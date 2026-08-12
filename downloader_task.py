"""下載任務核心:CLI 與 GUI 共用,支援章節範圍、快取斷點續傳、進度回報。"""
import re
import sys
import html as html_lib
import json
import os
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fetcher import FetchError, Fetcher
from sites import get_adapter
from sites.base import join_pages
from textfilter import apply_rules, drop_repeated, load_rules
from state_io import read_json, write_json

MAX_IN_MEMORY_CONTENT_BYTES = 64 * 1024 * 1024


class Cancelled(Exception):
    """使用者取消下載。"""


def cache_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "cache"
    return Path(__file__).parent / "cache"


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
    write_json(path, current)


def unique_chapters(chapters):
    seen = set()
    result = []
    for chapter in chapters:
        key = chapter.url.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(chapter)
    return result


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


def fetch_parsed_chapter(fetcher, adapter, chapter, retries: int) -> str:
    last_err = None
    for _ in range(max(retries, 1)):
        try:
            html = fetcher.get(chapter.url, retries=1)
            source_url = adapter.chapter_source_url(html, chapter.url)
            if source_url:
                html = fetcher.get(source_url, referer=chapter.url, retries=1)
            parts = [adapter.parse_chapter(html, title=chapter.title)]
            next_url = adapter.next_page_url(html, chapter.url)
            seen = {chapter.url}
            while next_url and next_url not in seen:
                seen.add(next_url)
                html = fetcher.get(next_url, retries=1)
                parts.append(adapter.parse_chapter(html, title=chapter.title))
                next_url = adapter.next_page_url(html, next_url)
            return join_pages(parts)
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
    book = adapter.parse_catalog(fetcher.get(catalog_url, retries=retries))
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

    cache = cache_root() / adapter.book_id(url)
    cache.mkdir(parents=True, exist_ok=True)
    save_progress(cache, url=url, title=book.title, total_chapters=total_all, range=[lo, hi], status="downloading")

    results = [None] * total  # (章節標題, 快取檔案)，固定索引保證輸出順序
    fetched = 0
    worker_local = threading.local()

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
            if not hasattr(worker_local, "fetcher"):
                worker_local.fetcher = Fetcher(
                    encoding=adapter.encoding, delay=delay, headers=request_headers, timeout=timeout)
            content = fetch_parsed_chapter(worker_local.fetcher, adapter, ch, retries)
            if is_generic and n == 1 and len(content) < 80:
                raise ValueError(
                    f"[自動偵測] 第一章只解析出 {len(content)} 字,通用模式可能抓錯正文區塊,"
                    "已中止下載;請回報網址讓我寫專屬 adapter")
            atomic_write_text(cache_file, content)
            worker_local.fetcher.polite_sleep()
            downloaded = True
        else:
            downloaded = False
        return n, idx, ch.title, cache_file, downloaded

    workers = max(1, min(int(chapter_workers), 8, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_job, n, idx, ch) for n, (idx, ch) in enumerate(jobs, 1)]
        completed = 0
        try:
            for future in as_completed(futures):
                n, idx, title, cache_file, downloaded = future.result()
                results[n - 1] = (title, cache_file)
                fetched += int(downloaded)
                completed += 1
                save_progress(cache, completed_count=completed, last_completed_chapter=idx, status="downloading")
                callback("chapter", completed, total, f"[{completed}/{total}] {title[:30]}")
        except Exception as exc:
            for future in futures:
                future.cancel()
            save_progress(cache, status="error", last_error=str(exc))
            raise

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
    callback("done", total, total, f"完成!新抓 {fetched} 章、快取 {total - fetched} 章\n輸出: {out}")
    return out
