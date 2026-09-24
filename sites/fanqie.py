"""番茄小說目錄與原始預覽資料 adapter。"""
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from fetcher import FetchError, Fetcher
from .base import BookInfo, SiteAdapter

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
        for key, value in self.statuses.items():
            lowered = str(key).lower()
            if _truthy(value) and any(marker in lowered for marker in ("pay", "vip", "member", "purchase")):
                return "restricted"
            if _truthy(value) and any(marker in lowered for marker in ("lock", "restrict", "login", "auth", "access")):
                return "unknown_locked"
        access_keys = ("isChapterLock", "locked", "isLocked", "accessRestricted")
        if any(_truthy(self.statuses.get(key)) for key in access_keys):
            return "unknown_locked"
        if all(key in self.statuses for key in ("needPay", "isPaidPublication", "isPaidStory", "isChapterLock")):
            return "public_candidate"
        return "unknown"


def _truthy(value):
    return value is True or value == 1 or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes"})


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
        status = str(source.get("accessStatus") or source.get("access_status") or "").lower()
        if any(word in status for word in ("pay", "lock", "restrict", "login", "verify", "auth", "denied", "forbidden")):
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
    preview_only = True

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
        raise FanqieError("番茄小說目前只支援目錄／原始資料 Preview，不能加入 TXT／EPUB 下載隊列。")

    def parse_chapter(self, html, title=""):
        raise FanqieError("番茄原始字元尚未還原，禁止經由 TXT／EPUB 匯出流程輸出。")


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
