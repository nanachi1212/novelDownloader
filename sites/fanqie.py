"""番茄小說公開章節與獨立原始預覽資料 adapter。"""
import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from fanqie_decoder import decode_chapter
from fetcher import FetchError, Fetcher
from state_io import read_json, write_json
from .base import BookInfo, Chapter, SiteAdapter

BOOK_ID_RE = re.compile(r"(?:/page/|bookId=)(\d+)", re.I)
ITEM_ID_RE = re.compile(r"^\d+$")
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
BOOK_URL = "https://fanqienovel.com/page/{book_id}"
READER_URL = "https://fanqienovel.com/reader/{item_id}"
DIRECTORY_URL = "https://fanqienovel.com/api/reader/directory/detail?bookId={book_id}"
FULL_URL = "https://fanqienovel.com/api/reader/full?itemId={item_id}"
INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")
SCRIPT_END_RE = re.compile(r"</script\s*>", re.I)
MAX_READER_HTML = 8 * 1024 * 1024


class FanqieError(RuntimeError):
    """目錄／預覽資料格式錯誤或站方拒絕存取。"""


class AccessVerificationRequired(FanqieError):
    """站方要求登入、人機驗證或其他存取驗證。"""


@dataclass(frozen=True)
class FanqieChapter:
    item_id: str
    title: str
    volume_name: str
    order: int
    statuses: dict

    @property
    def access(self):
        """Fail closed: isChapterLock alone is ambiguous, so never fetch it."""
        paid_keys = ("needPay", "isPaidPublication", "isPaidStory", "isPay", "isVip")
        if any(_truthy(self.statuses.get(key)) for key in paid_keys):
            return "restricted"
        if _response_access_status(self.statuses):
            return "unknown_locked"
        for key, value in self.statuses.items():
            lowered = str(key).lower()
            if _truthy(value) and any(marker in lowered for marker in ("pay", "vip", "member", "purchase")):
                return "restricted"
            if _truthy(value) and any(marker in lowered for marker in ("lock", "restrict", "login", "auth", "access")):
                return "unknown_locked"
        access_keys = ("isChapterLock", "locked", "isLocked", "accessRestricted")
        if any(_truthy(self.statuses.get(key)) for key in access_keys):
            return "unknown_locked"
        required_flags = ("needPay", "isPaidPublication", "isPaidStory", "isChapterLock")
        if all(key in self.statuses and _explicit_false(self.statuses[key]) for key in required_flags):
            return "public_candidate"
        return "unknown"


def _truthy(value):
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"})


def _explicit_false(value):
    return value is False or (type(value) is int and value == 0) or (
        isinstance(value, str) and value.strip().lower() in {"0", "false", "no"}
    )


def _response_access_status(data):
    """Return an access block from explicit API metadata, never from HTTP 200 alone."""
    for source in (data, data.get("chapterData") if isinstance(data, dict) else None):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            lowered = str(key).lower()
            if _truthy(value) and any(marker in lowered for marker in ("pay", "vip", "member", "purchase")):
                return "付費／受限"
            gate_key = (
                any(marker in lowered for marker in ("lock", "restrict", "captcha"))
                or ("required" in lowered and any(marker in lowered for marker in ("login", "auth", "verify")))
            )
            if _truthy(value) and gate_key:
                return "存取狀態未明"
        for key in ("accessStatus", "access_status"):
            if key in source and str(source[key]).strip().lower() not in {"public", "free", "unlocked"}:
                return "存取狀態未明"
    return ""


def _verification_required(fetcher):
    headers = {str(k).lower(): str(v) for k, v in getattr(fetcher, "last_response_headers", {}).items()}
    return bool(
        headers.get("bdturing-verify")
        or headers.get("x-vc-bdturing-parameters")
        or getattr(fetcher, "last_status_code", None) in (401, 403)
    )


def parse_book_id(url):
    match = BOOK_ID_RE.search((url or "").strip())
    if not match:
        raise FanqieError("請輸入番茄小說書籍網址或包含 bookId 的網址。")
    return match.group(1)


def _json_object(raw, label):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise FanqieError(f"{label}回應不是有效 JSON。") from exc
    if not isinstance(value, dict):
        raise FanqieError(f"{label}回應格式錯誤。")
    if value.get("code") != 0:
        message = value.get("message") or value.get("msg") or "站方回應失敗"
        raise FanqieError(f"{label}失敗：{message}（code={value.get('code')!r}）")
    return value


def _replace_js_undefined(source):
    """Normalize a bare JS undefined without changing quoted story text."""
    result = []
    quoted = escaped = False
    previous = ""
    index = 0
    while index < len(source):
        char = source[index]
        if quoted:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            result.append(char)
        elif (source.startswith("undefined", index) and previous in {":", ",", "["}
              and (index + 9 == len(source) or source[index + 9] in ",}] \t\r\n")):
            result.append("null")
            previous = "l"
            index += 9
            continue
        else:
            result.append(char)
            if not char.isspace():
                previous = char
        index += 1
    return "".join(result)


def _initial_state(html):
    if not isinstance(html, str) or not html or len(html.encode("utf-8")) > MAX_READER_HTML:
        raise FanqieError("番茄頁面為空或過大，未接受正文。")
    match = INITIAL_STATE_RE.search(html)
    if not match:
        return None
    end = SCRIPT_END_RE.search(html, match.end())
    if not end:
        raise FanqieError("番茄頁面的 INITIAL_STATE 不完整。")
    try:
        state, _ = json.JSONDecoder().raw_decode(_replace_js_undefined(html[match.end():end.start()]))
    except (ValueError, TypeError) as exc:
        raise FanqieError("番茄頁面的 INITIAL_STATE 格式錯誤。") from exc
    if not isinstance(state, dict):
        raise FanqieError("番茄頁面的 INITIAL_STATE 不是資料物件。")
    return state


def _chapter_paragraphs(content):
    if not isinstance(content, str) or not content.strip():
        return []
    soup = BeautifulSoup(content, "html.parser")
    return [text for node in soup.find_all("p") if (text := node.get_text("", strip=False).strip())]


def parse_directory_response(raw):
    root = _json_object(raw, "番茄目錄")
    data = root.get("data")
    if not isinstance(data, dict):
        raise FanqieError("番茄目錄缺少 data 欄位。")
    groups = data.get("chapterListWithVolume")
    all_ids = data.get("allItemIds")
    if not isinstance(groups, list) or not groups:
        raise FanqieError("番茄目錄為空或格式不完整。")
    chapters = []
    seen = set()
    for group in groups:
        if not isinstance(group, list):
            raise FanqieError("番茄目錄卷資料格式錯誤。")
        for item in group:
            if not isinstance(item, dict):
                raise FanqieError("番茄目錄章節資料格式錯誤。")
            item_id = str(item.get("itemId", ""))
            title = item.get("title")
            if not ITEM_ID_RE.fullmatch(item_id) or not isinstance(title, str) or not title.strip():
                raise FanqieError("番茄目錄包含缺少 itemId 或標題的章節。")
            if item_id in seen:
                raise FanqieError(f"番茄目錄 itemId 重複：{item_id}")
            seen.add(item_id)
            statuses = {key: value for key, value in item.items() if key not in {"itemId", "title", "volume_name"}}
            chapters.append(FanqieChapter(
                item_id=item_id,
                title=title,
                volume_name=str(item.get("volume_name") or "未分卷"),
                order=len(chapters) + 1,
                statuses=statuses,
            ))
    if not isinstance(all_ids, list) or [str(value) for value in all_ids] != [ch.item_id for ch in chapters]:
        raise FanqieError("番茄目錄章節數或排序與 allItemIds 不一致，已停止避免使用不完整目錄。")
    return chapters


def create_fetcher(delay=2.0, timeout=20):
    return Fetcher(delay=delay, timeout=timeout, headers={
        "User-Agent": MOBILE_UA,
        "Accept": "application/json, text/plain, */*",
        "ismobile": "1",
    })


class FanqieAdapter(SiteAdapter):
    domains = ["fanqienovel.com", "www.fanqienovel.com"]
    max_chapter_workers = 1
    max_request_retries = 1
    require_complete_chapters = True

    def book_id(self, url):
        return parse_book_id(url)

    def catalog_url(self, url):
        self._book_id = parse_book_id(url)
        self._book_title = ""
        self._book_author = ""
        self._chapter_access = {}
        self._decoder_mode = None
        self._expected_item_id = None
        return DIRECTORY_URL.format(book_id=self._book_id)

    def meta_url(self, url):
        self._book_id = parse_book_id(url)
        return BOOK_URL.format(book_id=self._book_id)

    def validate_response(self, fetcher):
        if _verification_required(fetcher):
            raise AccessVerificationRequired("番茄網站要求登入或人機驗證，已停止公開章節下載。")

    def parse_meta(self, html):
        state = _initial_state(html)
        page = state.get("page") if state else None
        if not isinstance(page, dict) or str(page.get("bookId")) != getattr(self, "_book_id", None):
            raise FanqieError("番茄書籍頁缺少相符的書籍資料；沒有使用不明頁面。")
        title, author = page.get("bookName"), page.get("author")
        if not isinstance(title, str) or not title.strip() or not isinstance(author, str):
            raise FanqieError("番茄書籍頁缺少書名或作者。")
        self._book_title, self._book_author = title.strip(), author.strip()
        return self._book_title, self._book_author

    def validate_download_chapter(self, chapter):
        for url in [chapter.url, *chapter.extra_urls]:
            match = re.fullmatch(r"https://fanqienovel\.com/reader/(\d+)", url)
            if not match or getattr(self, "_chapter_access", {}).get(match.group(1)) != "public_candidate":
                raise AccessVerificationRequired("所選番茄章節的目錄存取旗標不是明確公開，已停止下載。")

    def chapter_cache_filename(self, chapter, index):
        item_ids = []
        for url in [chapter.url, *chapter.extra_urls]:
            match = re.fullmatch(r"https://fanqienovel\.com/reader/(\d+)", url)
            if not match:
                raise FanqieError("番茄快取章節網址沒有有效 itemId。")
            item_ids.append(match.group(1))
        if len(item_ids) == 1:
            return f"{item_ids[0]}.txt"
        digest = hashlib.sha256(",".join(item_ids).encode("ascii")).hexdigest()[:16]
        return f"{item_ids[0]}-{digest}.txt"

    def restore_cache_state(self, cache):
        state = read_json(Path(cache) / "fanqie_decoder.json", {})
        if (isinstance(state, dict) and state.get("book_id") == getattr(self, "_book_id", None)
                and type(state.get("mode")) is int and state["mode"] in (0, 1)):
            self._decoder_mode = state["mode"]

    def save_cache_state(self, cache):
        if self._decoder_mode in (0, 1):
            write_json(Path(cache) / "fanqie_decoder.json",
                       {"book_id": self._book_id, "mode": self._decoder_mode})

    def chapter_source_url(self, html, url):
        match = re.fullmatch(r"https://fanqienovel\.com/reader/(\d+)", url)
        if not match:
            raise FanqieError("番茄章節網址不是已核對的 reader 頁。")
        self._expected_item_id = match.group(1)
        return None

    def fetch_directory(self, url, fetcher=None):
        book_id = parse_book_id(url)
        fetcher = fetcher or create_fetcher()
        try:
            raw = fetcher.get(
                DIRECTORY_URL.format(book_id=book_id),
                referer=BOOK_URL.format(book_id=book_id),
            )
        except FetchError as exc:
            if _verification_required(fetcher):
                raise AccessVerificationRequired("番茄目錄要求登入或人機驗證；沒有繞過驗證。") from exc
            raise FanqieError(str(exc)) from exc
        if _verification_required(fetcher):
            raise AccessVerificationRequired("番茄目錄要求登入或人機驗證；沒有繞過驗證。")
        return book_id, parse_directory_response(raw)

    def fetch_raw_chapter(self, book_id, chapter, fetcher=None):
        if chapter.access != "public_candidate":
            raise AccessVerificationRequired("此章存取狀態不是明確公開，已略過正文請求。")
        fetcher = fetcher or create_fetcher()
        url = FULL_URL.format(item_id=chapter.item_id)
        try:
            raw = fetcher.get(url, referer=READER_URL.format(item_id=chapter.item_id))
        except FetchError as exc:
            if _verification_required(fetcher):
                raise AccessVerificationRequired("番茄小說要求登入或人機驗證；此預覽不會繞過驗證，請改在原站閱讀。") from exc
            raise FanqieError(str(exc)) from exc
        headers = {str(k).lower(): str(v) for k, v in getattr(fetcher, "last_response_headers", {}).items()}
        if _verification_required(fetcher):
            raise AccessVerificationRequired("番茄小說要求人機驗證；此預覽不會繞過驗證，請改在原站閱讀。")
        if not raw or not raw.strip():
            raise FanqieError("番茄正文 API 回傳空內容；HTTP 狀態不能證明章節可讀。")
        response = _json_object(raw, "番茄章節")
        data = response.get("data")
        chapter_data = data.get("chapterData") if isinstance(data, dict) else None
        if not isinstance(chapter_data, dict):
            raise FanqieError("番茄章節沒有有效 chapterData，未保存空白或錯誤頁面。")
        access_status = _response_access_status(data)
        if access_status:
            raise AccessVerificationRequired(f"番茄章節回應標示{access_status}；未保存正文。")
        body = next((chapter_data.get(key) for key in ("content", "content_html", "contentHtml", "paragraphs")
                     if chapter_data.get(key) not in (None, "", [])), None)
        if not isinstance(body, (str, list)):
            raise FanqieError("番茄 chapterData 沒有可辨識的正文欄位，未保存非正文回應。")
        returned_id = str(chapter_data.get("itemId") or chapter_data.get("item_id") or chapter.item_id)
        if returned_id != chapter.item_id:
            raise FanqieError("番茄正文 itemId 與所選章節不符。")
        # Keep the exact response text plus its per-chapter font map header. This is
        # inert JSON data and is never executed as HTML or JavaScript.
        source_headers = {
            key: value for key, value in headers.items()
            if key in {"x-tt-zhal", "content-type"}
        }
        return json.dumps({
            "book_id": str(book_id), "item_id": chapter.item_id,
            "source_response_text": raw, "source_headers": source_headers,
        }, ensure_ascii=False)

    def parse_catalog(self, html):
        book_id = getattr(self, "_book_id", None)
        if not book_id:
            raise FanqieError("番茄目錄缺少來源 bookId。")
        source = parse_directory_response(html)
        self._chapter_access = {item.item_id: item.access for item in source}
        return BookInfo(
            getattr(self, "_book_title", "") or f"番茄小說_{book_id}",
            getattr(self, "_book_author", ""),
            [Chapter(item.title, READER_URL.format(item_id=item.item_id)) for item in source],
        )

    def parse_chapter(self, html, title=""):
        state = _initial_state(html)
        reader = state.get("reader") if state else None
        chapter_data = reader.get("chapterData") if isinstance(reader, dict) else None
        if not isinstance(chapter_data, dict):
            raise AccessVerificationRequired("番茄 reader 頁缺少章節資料，可能是驗證或錯誤頁；未保存正文。")
        item_id = str(chapter_data.get("itemId") or "")
        expected = getattr(self, "_expected_item_id", None)
        if not ITEM_ID_RE.fullmatch(item_id) or (expected and item_id != expected):
            raise FanqieError("番茄 reader 正文 itemId 與所選章節不符。")
        book_id = str(chapter_data.get("bookId") or "")
        if not ITEM_ID_RE.fullmatch(book_id) or (getattr(self, "_book_id", None) and book_id != self._book_id):
            raise FanqieError("番茄 reader 正文 bookId 與所選書籍不符。")
        access = FanqieChapter(item_id, title, "", 0, chapter_data).access
        if access != "public_candidate" or _response_access_status(chapter_data):
            raise AccessVerificationRequired("番茄 reader 章節未標示明確公開，未保存正文。")
        if getattr(self, "_chapter_access", {}).get(item_id, "public_candidate") != "public_candidate":
            raise AccessVerificationRequired("番茄目錄與 reader 的公開狀態不一致，未保存正文。")
        paragraphs = _chapter_paragraphs(chapter_data.get("content"))
        if not paragraphs:
            soup = BeautifulSoup(html, "html.parser")
            node = soup.select_one(".muye-reader-content")
            paragraphs = _chapter_paragraphs(str(node)) if node else []
        if not paragraphs:
            raise FanqieError("番茄 reader 頁沒有完整可辨識的段落正文，未保存錯誤頁。")
        if title and paragraphs[0].strip() == title.strip():
            paragraphs.pop(0)
        body = "\n\n".join(paragraphs)
        expected_words = chapter_data.get("chapterWordNumber")
        if (type(expected_words) is int and expected_words > 0
                and len(body.replace("\n", "")) < expected_words * 0.7):
            raise FanqieError("番茄 reader 正文短於章節字數，可能是截斷內容；未保存。")
        decoded = decode_chapter(body, getattr(self, "_decoder_mode", None))
        if decoded.mode is not None:
            self._decoder_mode = decoded.mode
        return decoded.text


def chapter_cache_path(root, book_id, item_id):
    if not re.fullmatch(r"\d+", str(book_id)) or not ITEM_ID_RE.fullmatch(str(item_id)):
        raise ValueError("book_id/item_id 格式錯誤")
    return Path(root) / str(book_id) / f"{item_id}.json"


def validate_raw_cache(path, book_id, item_id):
    """Only resume from a complete, matching source response; never bless empty files."""
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        if (envelope.get("book_id") != str(book_id) or envelope.get("item_id") != str(item_id)
                or not isinstance(envelope.get("source_headers"), dict)):
            return False
        source = _json_object(envelope.get("source_response_text"), "已保存章節")
        source_data = source.get("data", {})
        if _response_access_status(source_data):
            return False
        chapter_data = source_data.get("chapterData")
        if not isinstance(chapter_data, dict):
            return False
        returned_id = str(chapter_data.get("itemId") or chapter_data.get("item_id") or item_id)
        body = next((chapter_data.get(key) for key in ("content", "content_html", "contentHtml", "paragraphs")
                     if chapter_data.get(key) not in (None, "", [])), None)
        return returned_id == str(item_id) and isinstance(body, (str, list))
    except (OSError, TypeError, ValueError, AttributeError, FanqieError):
        return False


def open_reader_url(item_id):
    if not ITEM_ID_RE.fullmatch(str(item_id)):
        raise ValueError("item_id 格式錯誤")
    return READER_URL.format(item_id=item_id)


def save_raw_chapters(adapter, book_id, chapters, root, fetcher=None, cancel_event=None, progress=None):
    """Atomically save raw responses by stable itemId; existing files resume safely."""
    cancel_event = cancel_event or threading.Event()
    fetcher = fetcher or create_fetcher()
    saved = []
    for chapter in chapters:
        if cancel_event.is_set():
            break
        target = chapter_cache_path(root, book_id, chapter.item_id)
        if target.is_file():
            if not validate_raw_cache(target, book_id, chapter.item_id):
                raise FanqieError(f"既有 Preview 快取無效，為保留檔案未覆寫：{target}")
            saved.append((chapter.item_id, target, "cached"))
            continue
        if progress:
            progress(chapter)
        if cancel_event.is_set():
            break
        raw = adapter.fetch_raw_chapter(book_id, chapter, fetcher)
        if cancel_event.is_set():
            break
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        saved.append((chapter.item_id, target, "saved"))
    return saved
