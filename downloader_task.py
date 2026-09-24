"""下載任務核心:CLI 與 GUI 共用,支援章節範圍、快取斷點續傳、進度回報。"""
import re
import html as html_lib
import json
import os
import logging
import time
import unicodedata
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from fetcher import FetchError, Fetcher
from sites import get_adapter
from sites.base import Chapter, join_pages
from textfilter import apply_rules, drop_repeated, load_rules
from state_io import read_json, write_json
from app_paths import prepare_app_data

logger = logging.getLogger(__name__)

MAX_IN_MEMORY_CONTENT_BYTES = 64 * 1024 * 1024
CHAPTER_RETRY_MAX_WAIT = 8      # 單章重試之間的退避上限(秒)
RETRY_COOLDOWN = 20             # 第一輪失敗後的冷卻秒數,等網站的暫時性限速解除
ABORT_AFTER_CONSECUTIVE = 20    # 開頭連續這麼多章全失敗就直接中止
MAX_CATALOG_PAGES = 200         # 目錄分頁上限,避免分頁連結壞掉時無窮抓取

# 目錄把同一章拆成多個項目時的編號後綴,例如「第一章(2)」「第一章（3/5）」「第一章【2】」
SPLIT_SUFFIX_RE = re.compile(
    r"^(?P<base>.*?)\(\s*(?P<part>\d+)(?:\s*/\s*\d+)?\s*\)$"
    r"|^(?P<base2>.*?)[【\[]\s*(?P<part2>\d+)\s*[】\]]$"
)


class Cancelled(Exception):
    """使用者取消下載。"""


def cache_root() -> Path:
    return prepare_app_data() / "cache"


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().rstrip(". ")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if cleaned.split(".", 1)[0].upper() in reserved:
        cleaned = "_" + cleaned
    return cleaned[:180] or "novel"


def atomic_write_text(path: Path, text: str):
    """同資料夾暫存後替換，避免程序中止留下半截正式檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def save_progress(cache: Path, **values):
    path = cache / "progress.json"
    current = read_json(path, {})
    if not isinstance(current, dict):
        current = {}
    if "completed_count" in values:
        current.pop("completed_chapters", None)
    current.update(values, updated_at=int(time.time()))
    try:
        write_json(path, current)
    except OSError as exc:
        # Progress is auxiliary state.  A transient OneDrive lock must not
        # turn an otherwise successful chapter download into a failed book.
        logger.warning("Cannot save download progress %s: %s", path, exc)


def unique_chapters(chapters):
    seen = set()
    result = []
    for chapter in chapters:
        key = chapter.url.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(chapter)
    return result


def _split_chapter_suffix(title: str):
    """回傳 (去掉編號後綴的標題, 編號);沒有後綴回傳 (原標題, None)。"""
    normalized = unicodedata.normalize("NFKC", title.strip())
    m = SPLIT_SUFFIX_RE.match(normalized)
    if not m:
        return normalized, None
    base = m.group("base") if m.group("base") is not None else m.group("base2")
    part = m.group("part") if m.group("part") is not None else m.group("part2")
    return base.strip(), int(part)


def order_catalog_pages(pages):
    """依網址裡的頁碼還原目錄分頁順序。

    使用者可能貼的是第 2 頁,分頁控制項卻同時連到第 1、3 頁,單純依抓取順序
    會得到 2、1、3。只有在所有分頁網址除了數字之外完全同樣式時才重排
    (index_2.html / ?page=2 這類);否則保持抓取順序,不亂猜。
    pages: [(url, chapters)];回傳同樣結構、排好序的 list。
    """
    def template(url):
        return re.sub(r"\d+", "#", url)

    if len(pages) < 2 or len({template(url) for url, _ in pages}) != 1:
        return pages
    return sorted(pages, key=lambda page: [int(n) for n in re.findall(r"\d+", page[0])])


def merge_split_chapters(chapters):
    """目錄把同一章拆成多個項目(第一章(1)、第一章(2)…)時合併成一章。

    只在「相鄰、標題基底相同、編號連續」時合併;第一項可省略編號(視為第 1 段)。
    回傳 (合併後章節, 是否有發生合併)。
    """
    if len(chapters) < 2:
        return chapters, False

    merged, happened = [], False
    i, n = 0, len(chapters)
    while i < n:
        base, part = _split_chapter_suffix(chapters[i].title)
        if part not in (None, 1):
            # 第 1 段不在(目錄漏抓或解析失敗),不能從第 2/3 段開始假裝合併出
            # 一個「完整」章節,那樣會把缺頭的內容悄悄藏起來。
            merged.append(chapters[i])
            i += 1
            continue
        group = [chapters[i]]
        expected = (part or 1) + 1
        j = i + 1
        while j < n:
            nbase, npart = _split_chapter_suffix(chapters[j].title)
            if nbase != base or npart != expected:
                break
            group.append(chapters[j])
            expected += 1
            j += 1
        if len(group) > 1:
            happened = True
            head = group[0]
            extra_urls = list(head.extra_urls)
            for chapter in group[1:]:
                extra_urls.append(chapter.url)
                extra_urls.extend(chapter.extra_urls)
            merged.append(Chapter(title=base, url=head.url, extra_urls=extra_urls))
        else:
            merged.append(chapters[i])
        i = j if len(group) > 1 else i + 1
    return merged, happened


def output_basename(title: str, author: str, site: str, pattern: str = "title") -> str:
    values = {"title": title, "author": author, "site": site}
    if pattern == "author_title":
        name = f"{author}_{title}" if author else title
    elif pattern == "site_title":
        name = f"{site}_{title}" if site else title
    else:
        name = title
    return safe_filename(name)


def chapter_number_warning(chapters) -> str:
    nums = []
    for ch in chapters:
        match = re.search(r"第\s*(\d+)\s*章", ch.title)
        if match:
            nums.append(int(match.group(1)))
    if not nums:
        return ""
    unique_nums = sorted(set(nums))
    min_num, max_num = unique_nums[0], unique_nums[-1]
    repeated = len(nums) - len(set(nums))
    missing_count = 0
    missing_preview = []
    for left, right in zip(unique_nums, unique_nums[1:]):
        gap = max(0, right - left - 1)
        missing_count += gap
        if gap and len(missing_preview) < 10:
            missing_preview.extend(range(left + 1, min(right, left + 1 + 10 - len(missing_preview))))
    if not missing_count and not repeated:
        return ""
    parts = [
        f"[章號提示] 目錄有 {len(chapters)} 個章節連結",
        f"標題最大章號為 {max_num}",
    ]
    if repeated:
        parts.append(f"偵測到重複章號 {repeated} 個")
    if missing_count:
        preview = "、".join(str(number) for number in missing_preview)
        suffix = "…" if missing_count > len(missing_preview) else ""
        parts.append(f"疑似缺少章號 {preview}{suffix}（共 {missing_count} 個）")
    parts.append("網站章號可能不可靠,仍按唯一章節網址下載。")
    return ",".join(parts)


def fetch_parsed_chapter(fetcher, adapter, chapter, retries: int, on_retry=None) -> str:
    last_err = None
    attempts = max(retries, 1)
    for attempt in range(attempts):
        if attempt:
            # 網站限速時常回 404/403,緊接著重打只會被擋更久,改成指數退避
            wait = min(2 ** attempt, CHAPTER_RETRY_MAX_WAIT)
            if on_retry:
                on_retry(attempt + 1, attempts, wait, last_err)
            time.sleep(wait)
        try:
            parts = []
            seen = {chapter.url}

            def fetch_one(page_url, referer=None):
                html = fetcher.get(page_url, referer=referer, retries=1)
                source_url = adapter.chapter_source_url(html, page_url)
                if source_url:
                    html = fetcher.get(source_url, referer=page_url, retries=1)
                parts.append(adapter.parse_chapter(html, title=chapter.title))
                return html

            def follow_same_chapter_pages(html, page_url):
                next_url = adapter.next_page_url(html, page_url)
                while next_url and next_url not in seen:
                    seen.add(next_url)
                    html = fetch_one(next_url)
                    next_url = adapter.next_page_url(html, next_url)

            html = fetch_one(chapter.url)
            follow_same_chapter_pages(html, chapter.url)

            # 目錄把同一章拆成多個項目時(merge_split_chapters 合併後),其餘網址
            # 都在這裡依序抓;next_page_url 與 extra_urls 共用 seen,同一 URL 不重抓。
            for extra_url in chapter.extra_urls:
                if extra_url in seen:
                    continue
                seen.add(extra_url)
                html = fetch_one(extra_url)
                follow_same_chapter_pages(html, extra_url)

            content = join_pages(parts)
            if not content.strip():
                # 空正文(選錯區塊、被拆頁但正文其實是限速/錯誤頁)一律當失敗重試,
                # 絕不能讓呼叫端把空字串當成功寫入快取。
                raise ValueError(f"章節正文為空: {chapter.url}")
            return content
        except (FetchError, ValueError) as e:
            last_err = e
    raise last_err


def write_epub(path: Path, title: str, author: str, source: str, chapters, transform=None):
    """以標準 library 產生可被閱讀器開啟的最小 EPUB 3 檔案。"""
    import uuid

    book_id = f"urn:uuid:{uuid.uuid4()}"
    part = path.with_name(path.name + ".part")
    with zipfile.ZipFile(part, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
        items = []
        spine = []
        for index, (chapter_title, raw_content) in enumerate(chapters, 1):
            content = raw_content.read_text(encoding="utf-8") if isinstance(raw_content, Path) else raw_content
            if transform:
                content = transform(content)
            item_id = f"chapter{index}"
            filename = f"chapter{index}.xhtml"
            paragraphs = "".join(
                f"<p>{html_lib.escape(line)}</p>" for line in content.splitlines() if line.strip()
            )
            xhtml = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{html_lib.escape(chapter_title)}</title></head>
<body><h1>{html_lib.escape(chapter_title)}</h1>{paragraphs}</body></html>"""
            book.writestr(f"OEBPS/{filename}", xhtml)
            items.append(f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{item_id}"/>')
        nav_links = "".join(
            f'<li><a href="chapter{index}.xhtml">{html_lib.escape(chapter_title)}</a></li>'
            for index, (chapter_title, _) in enumerate(chapters, 1)
        )
        book.writestr("OEBPS/nav.xhtml", f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>{html_lib.escape(title)}</title></head><body><nav epub:type="toc"><ol>{nav_links}</ol></nav></body></html>""")
        items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        book.writestr("OEBPS/content.opf", f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{html_lib.escape(title)}</dc:title><dc:creator>{html_lib.escape(author)}</dc:creator>
<dc:language>zh-Hant</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
<manifest>{''.join(items)}</manifest><spine>{''.join(spine)}</spine></package>""")
        book.writestr("OEBPS/toc.ncx", "")
    os.replace(part, path)


def write_txt_from_files(path, header, chapters, transform=None):
    part = path.with_name(path.name + ".part")
    with part.open("w", encoding="utf-8", newline="\n") as output:
        output.write(header + "\n\n")
        for index, (title, source) in enumerate(chapters):
            content = source.read_text(encoding="utf-8")
            if transform:
                content = transform(content)
            if index:
                output.write("\n\n\n")
            output.write(f"{title}\n\n{content}")
        output.write("\n")
    os.replace(part, path)


def download_novel(url, output_dir, title_override="", delay=2.0, callback=None,
                   start=None, end=None, cancel_check=None, retries=5,
                   output_format="txt", filename_format="title", request_headers=None,
                   timeout=20, chapter_workers=3):
    """下載小說並輸出 TXT,回傳輸出檔路徑。

    callback(stage, current, total, msg),stage: 'catalog'|'chapter'|'done'
    start/end:1-based 章節範圍(含端點),None 表示不限
    cancel_check:回傳 True 時在章節邊界中止,拋出 Cancelled
    """
    callback = callback or (lambda *a: None)
    adapter = get_adapter(url)
    fetcher = Fetcher(encoding=adapter.encoding, delay=delay, headers=request_headers, timeout=timeout)

    callback("catalog", 0, 1, "正在抓取目錄...")
    catalog_url = adapter.catalog_url(url)
    meta_url = adapter.meta_url(url)
    if meta_url:
        title, author = adapter.parse_meta(fetcher.get(meta_url, retries=retries))
    else:
        title = author = ""

    catalog_html = fetcher.get(catalog_url, retries=retries)
    full_url = adapter.full_catalog_url(catalog_html, catalog_url)
    if full_url and full_url != catalog_url:
        callback("catalog", 0, 1, "[目錄] 已展開完整目錄")
        catalog_url = full_url
        fetcher.polite_sleep()
        catalog_html = fetcher.get(catalog_url, retries=retries)

    # 用 parse_catalog_page 而非 parse_catalog:展開完整目錄後,GenericAdapter
    # 需要知道實際抓到的是哪個網址,才能正確解析頁面上的相對連結
    # (catalog_url() 當初記下的是展開前的舊網址)。
    book = adapter.parse_catalog_page(catalog_html, catalog_url)
    template_name = getattr(adapter, "template_name", None)
    if template_name:
        callback("catalog", 0, 1, f"[自動偵測] 目錄套用內建模板: {template_name}")

    # 目錄分頁:反覆問 adapter「還有哪些分頁」,直到沒有新分頁為止。
    # 迴圈安全由 visited_pages(每個網址只抓一次)+ MAX_CATALOG_PAGES(硬上限)
    # 保證,不會因為 A→B→A 這種循環而無窮抓取。用「新增的章節網址」而非單純的
    # 章節數變化來判斷有沒有進展:某一頁剛好是重複頁/別名(0 個新章節)只代表
    # 那一頁沒帶來新東西,不代表後面排隊的其他頁也一樣,所以不整批中止,
    # 只是這頁不計入章節、繼續處理佇列裡剩下的頁面。
    visited_pages = {catalog_url}
    seen_chapter_urls = {c.url for c in book.chapters}
    catalog_pages = [(catalog_url, list(book.chapters))]  # 依抓取順序記下每一頁,最後依頁碼還原順序
    queue = list(adapter.catalog_page_urls(catalog_html, catalog_url))
    pages_fetched = 0
    host = urlparse(catalog_url).netloc
    while queue and pages_fetched < MAX_CATALOG_PAGES:
        next_url = queue.pop(0)
        if next_url in visited_pages or urlparse(next_url).netloc != host:
            continue
        visited_pages.add(next_url)
        pages_fetched += 1
        fetcher.polite_sleep()
        next_html = fetcher.get(next_url, retries=retries)
        extra = adapter.parse_catalog_page(next_html, next_url)
        new_chapters = [c for c in extra.chapters if c.url not in seen_chapter_urls]
        if new_chapters:
            seen_chapter_urls.update(c.url for c in new_chapters)
            catalog_pages.append((next_url, new_chapters))
        for more in adapter.catalog_page_urls(next_html, next_url):
            if more not in visited_pages and more not in queue:
                queue.append(more)
    if pages_fetched:
        book.chapters = [c for _url, chapters in order_catalog_pages(catalog_pages) for c in chapters]
        callback("catalog", 0, 1,
                 f"[目錄分頁] 共抓取 {pages_fetched} 頁,合計 {len(book.chapters)} 個章節連結")

    if title:
        book.title = title
    if author:
        book.author = author
    if title_override:
        book.title = title_override

    numbering_warning = chapter_number_warning(book.chapters)
    if numbering_warning:
        callback("catalog", 0, 1, numbering_warning)

    is_generic = getattr(adapter, "is_generic", False)
    if is_generic:
        if len(book.chapters) < 3:
            raise ValueError(
                f"[自動偵測] 只找到 {len(book.chapters)} 個章節連結,此網站可能用 JavaScript 載入目錄,"
                "通用模式吃不下,請回報網址讓我寫專屬 adapter")
        callback("catalog", 1, 1,
                 f"[自動偵測] 未註冊網站,使用通用解析|"
                 f"第一章: {book.chapters[0].title[:20]}|最後一章: {book.chapters[-1].title[:20]}")

    original_count = len(book.chapters)
    book.chapters = unique_chapters(book.chapters)
    if len(book.chapters) != original_count:
        callback("catalog", 0, 1, f"[目錄去重] 移除 {original_count - len(book.chapters)} 個重複章節連結")

    pre_merge_count = len(book.chapters)
    book.chapters, merged = merge_split_chapters(book.chapters)
    if merged:
        callback("catalog", 0, 1,
                 f"[合併分頁章節] 由 {pre_merge_count} 個目錄項目合併為 {len(book.chapters)} 章")

    total_all = len(book.chapters)
    lo = max(1, start or 1)
    hi = min(total_all, end or total_all)
    if lo > hi:
        raise ValueError(f"章節範圍無效:{lo} > {hi}(全書共 {total_all} 章)")
    jobs = [(i, book.chapters[i - 1]) for i in range(lo, hi + 1)]
    total = len(jobs)
    range_note = f",本次範圍第 {lo}~{hi} 章" if (start or end) else ""
    callback("catalog", 1, 1,
             f"《{book.title}》作者: {book.author},全書 {total_all} 章{range_note},共下載 {total} 章")

    # 有合併發生時快取換一個命名空間,避免舊快取(依合併前的章節位置編號)
    # 對不上合併後的新章節順序。
    cache = cache_root() / (adapter.book_id(url) + ("-merged" if merged else ""))
    cache.mkdir(parents=True, exist_ok=True)
    prior_progress = read_json(cache / "progress.json", {})
    if (isinstance(prior_progress, dict) and prior_progress.get("total_chapters")
            and prior_progress["total_chapters"] != total_all and any(cache.glob("*.txt"))):
        callback("catalog", 0, 1,
                 f"[快取提醒] 目錄章數由 {prior_progress['total_chapters']} 變成 {total_all},"
                 "網站章節可能有增刪或改版,既有快取的章號可能對不上;不確定時建議清空快取後重新下載")
    save_progress(cache, url=url, title=book.title, total_chapters=total_all, range=[lo, hi], status="downloading")

    results = [None] * total  # (章節標題, 快取檔案)，固定索引保證輸出順序
    fetched = 0
    worker_local = threading.local()
    chapter_limit = getattr(adapter, "max_chapter_workers", 8)
    workers = max(1, min(int(chapter_workers), int(chapter_limit), 8, total))

    def worker_fetcher():
        if hasattr(worker_local, "fetcher"):
            return worker_local.fetcher
        if workers == 1:
            # Reuse the catalog session exactly as v1.5.6 did.  Some sites set
            # anti-bot/session state while serving the catalog and require it
            # for every chapter request.
            worker_local.fetcher = fetcher
            return worker_local.fetcher
        child = Fetcher(
            encoding=adapter.encoding, delay=delay, headers=request_headers, timeout=timeout,
            throttle=fetcher.throttle)  # 共用退避狀態:一個 worker 被 429,全部一起放慢
        try:
            child.session.cookies.update(fetcher.session.cookies)
        except (AttributeError, TypeError):
            pass
        worker_local.fetcher = child
        return child

    def fetch_job(n, idx, ch):
        if cancel_check and cancel_check():
            raise Cancelled("使用者中止,已下載的章節保留在快取,重跑會續傳")
        cache_file = cache / f"{idx:04d}.txt"  # 用全書絕對章號命名,範圍下載也能共用快取
        content = ""
        if cache_file.exists():
            try:
                content = cache_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                content = ""
        if not content:
            active_fetcher = worker_fetcher()
            def on_retry(attempt, attempts, wait, error):
                callback("retry", 0, 1,
                         f"[重試] 第{idx}章 {ch.title[:16]} 第 {attempt}/{attempts} 次,"
                         f"等 {wait} 秒（{str(error)[-40:]}）")

            try:
                content = fetch_parsed_chapter(active_fetcher, adapter, ch, retries, on_retry)
            except (FetchError, ValueError) as exc:
                # 單章失敗(網站偶發 404/逾時,或該章解析結果是空正文/選錯區塊)不寫快取,
                # 交由呼叫端決定是否中止整本;絕不能讓單一章節的 ValueError 弄垮整批下載。
                return n, idx, ch.title, None, False, str(exc)
            if is_generic and n == 1 and len(content) < 80:
                raise ValueError(
                    f"[自動偵測] 第一章只解析出 {len(content)} 字,通用模式可能抓錯正文區塊,"
                    "已中止下載;請回報網址讓我寫專屬 adapter")
            atomic_write_text(cache_file, content)
            active_fetcher.polite_sleep()
            downloaded = True
        else:
            downloaded = False
        return n, idx, ch.title, cache_file, downloaded, ""

    # 網站忙碌時常整批回 404/限速,第一輪失敗不代表章節不存在:先跑完,再放慢重試一輪。
    max_failures = max(3, total // 100)
    failures = []          # [(n, idx, 章節)]
    last_error = ""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_job, n, idx, ch) for n, (idx, ch) in enumerate(jobs, 1)]
        completed = 0
        try:
            for future in as_completed(futures):
                n, idx, title, cache_file, downloaded, error = future.result()
                completed += 1
                if error:
                    failures.append((n, idx, jobs[n - 1][1]))
                    last_error = error
                    if len(failures) >= ABORT_AFTER_CONSECUTIVE and len(failures) == completed:
                        # 從頭到尾每一章都失敗:網站整個擋住,再抓下去只是浪費時間
                        raise FetchError(f"連續 {completed} 章都抓取失敗,中止下載。最後錯誤:{error}")
                    callback("chapter", completed, total,
                             f"[{completed}/{total}] ✗ 第{idx}章 {title[:20]} 抓取失敗,稍後重試")
                    continue
                results[n - 1] = (title, cache_file)
                fetched += int(downloaded)
                save_progress(cache, completed_count=completed, last_completed_chapter=idx, status="downloading")
                callback("chapter", completed, total, f"[{completed}/{total}] {title[:30]}")
        except Exception as exc:
            for future in futures:
                future.cancel()
            save_progress(cache, status="error", last_error=str(exc))
            raise

    if failures:
        callback("chapter", completed, total,
                 f"[重試] {len(failures)} 章第一輪失敗,等 {RETRY_COOLDOWN} 秒後單執行緒重抓")
        time.sleep(RETRY_COOLDOWN)  # 給網站的暫時性限速一點時間解除
        remaining = []
        for n, idx, ch in failures:
            if cancel_check and cancel_check():
                raise Cancelled("使用者中止,已下載的章節保留在快取,重跑會續傳")
            cache_file = cache / f"{idx:04d}.txt"
            try:
                content = fetch_parsed_chapter(fetcher, adapter, ch, retries)
            except (FetchError, ValueError) as exc:
                last_error = str(exc)
                remaining.append((idx, ch.title))
                continue
            atomic_write_text(cache_file, content)
            fetcher.polite_sleep()
            results[n - 1] = (ch.title, cache_file)
            fetched += 1
            callback("chapter", completed, total, f"[重試成功] 第{idx}章 {ch.title[:20]}")
        failures = remaining
        if len(failures) > max_failures:
            save_progress(cache, status="error", last_error=last_error)
            raise FetchError(
                f"重試後仍有 {len(failures)} 章失敗(上限 {max_failures}),中止下載;"
                f"稍後重跑會從快取續傳。最後錯誤:{last_error}")
    if failures:
        preview = "、".join(f"第{idx}章" for idx, _ in failures[:5])
        callback("chapter", completed, total,
                 f"[略過] {len(failures)} 章抓取失敗({preview}),輸出不含這些章節;重跑會補抓")
    results = [item for item in results if item]

    # 合併前後處理(只動輸出,不動快取):自訂規則 → 跨章重複樣板自動偵測
    # 提取網站識別符(優先用專用規則,找不到用全局)
    site_hint = adapter.domains[0] if hasattr(adapter, "domains") and adapter.domains else None
    rules = load_rules(site_hint)
    content_bytes = sum(path.stat().st_size for _, path in results)
    stream_output = content_bytes > MAX_IN_MEMORY_CONTENT_BYTES
    contents = [] if stream_output else [apply_rules(path.read_text(encoding="utf-8"), rules) for _, path in results]
    if rules:
        rule_file = f"filter_rules_{site_hint.replace('.', '_').lower()}.txt" if site_hint else "filter_rules.txt"
        callback("filter", 0, 1, f"[過濾] 已套用 {len(rules)} 條規則({rule_file})")
    contents, removed = drop_repeated(contents)
    if removed:
        preview = "|".join(p[:25] for p in removed[:3])
        callback("filter", 0, 1,
                 f"[自動去重] 移除 {len(removed)} 條跨章重複的宣傳/廣告樣板,如: {preview}")
    if stream_output:
        callback("filter", 0, 1, "[記憶體保護] 正文超過 64 MB，改用逐章輸出並略過跨章樣板偵測")
    texts = [f"{t}\n\n{c}" for (t, _), c in zip(results, contents)]

    suffix = f"_第{lo}-{hi}章" if (start or end) else ""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    extension = ".epub" if output_format.lower() == "epub" else ".txt"
    out = output_path / f"{output_basename(book.title, book.author, site_hint or '', filename_format)}{suffix}{extension}"
    if extension == ".epub":
        chapters = results if stream_output else [(t, c) for (t, _), c in zip(results, contents)]
        write_epub(out, book.title, book.author, catalog_url, chapters,
                   transform=(lambda text: apply_rules(text, rules)) if stream_output else None)
    else:
        header = f"{book.title}\n作者: {book.author}\n來源: {catalog_url}\n"
        if stream_output:
            write_txt_from_files(out, header, results, lambda text: apply_rules(text, rules))
        else:
            atomic_write_text(out, header + "\n\n" + "\n\n\n".join(texts) + "\n")
    save_progress(cache, completed_chapters=[idx for idx, _ in jobs], status="done", output=str(out))
    skipped_note = f"、失敗略過 {len(failures)} 章" if failures else ""
    callback("done", total, total,
             f"完成!新抓 {fetched} 章、快取 {len(results) - fetched} 章{skipped_note}\n輸出: {out}")
    return out
