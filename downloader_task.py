"""下載任務核心:CLI 與 GUI 共用,支援章節範圍、快取斷點續傳、進度回報。"""
import re
import sys
import html as html_lib
import zipfile
from pathlib import Path

from fetcher import Fetcher
from sites import get_adapter
from sites.base import join_pages
from textfilter import apply_rules, drop_repeated, load_rules


class Cancelled(Exception):
    """使用者取消下載。"""


def cache_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "cache"
    return Path(__file__).parent / "cache"


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "novel"


def output_basename(title: str, author: str, site: str, pattern: str = "title") -> str:
    values = {"title": title, "author": author, "site": site}
    if pattern == "author_title":
        name = f"{author}_{title}" if author else title
    elif pattern == "site_title":
        name = f"{site}_{title}" if site else title
    else:
        name = title
    return safe_filename(name)


def write_epub(path: Path, title: str, author: str, source: str, chapters: list[tuple[str, str]]):
    """以標準 library 產生可被閱讀器開啟的最小 EPUB 3 檔案。"""
    import uuid

    book_id = f"urn:uuid:{uuid.uuid4()}"
    with zipfile.ZipFile(path, "w") as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
        items = []
        spine = []
        for index, (chapter_title, content) in enumerate(chapters, 1):
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


def download_novel(url, output_dir, title_override="", delay=2.0, callback=None,
                   start=None, end=None, cancel_check=None, retries=5,
                   output_format="txt", filename_format="title", request_headers=None):
    """下載小說並輸出 TXT,回傳輸出檔路徑。

    callback(stage, current, total, msg),stage: 'catalog'|'chapter'|'done'
    start/end:1-based 章節範圍(含端點),None 表示不限
    cancel_check:回傳 True 時在章節邊界中止,拋出 Cancelled
    """
    callback = callback or (lambda *a: None)
    adapter = get_adapter(url)
    fetcher = Fetcher(encoding=adapter.encoding, delay=delay, headers=request_headers)

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

    is_generic = getattr(adapter, "is_generic", False)
    if is_generic:
        if len(book.chapters) < 3:
            raise ValueError(
                f"[自動偵測] 只找到 {len(book.chapters)} 個章節連結,此網站可能用 JavaScript 載入目錄,"
                "通用模式吃不下,請回報網址讓我寫專屬 adapter")
        callback("catalog", 1, 1,
                 f"[自動偵測] 未註冊網站,使用通用解析|"
                 f"第一章: {book.chapters[0].title[:20]}|最後一章: {book.chapters[-1].title[:20]}")

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

    results = []  # (章節標題, 內文)
    fetched = 0
    for n, (idx, ch) in enumerate(jobs, 1):
        if cancel_check and cancel_check():
            raise Cancelled("使用者中止,已下載的章節保留在快取,重跑會續傳")
        cache_file = cache / f"{idx:04d}.txt"  # 用全書絕對章號命名,範圍下載也能共用快取
        if cache_file.exists():
            content = cache_file.read_text(encoding="utf-8")
        else:
            html = fetcher.get(ch.url, retries=retries)
            source_url = adapter.chapter_source_url(html, ch.url)
            if source_url:
                html = fetcher.get(source_url, referer=ch.url, retries=retries)
            parts = [adapter.parse_chapter(html, title=ch.title)]
            next_url = adapter.next_page_url(html, ch.url)
            seen = {ch.url}
            while next_url and next_url not in seen:
                seen.add(next_url)
                html = fetcher.get(next_url, retries=retries)
                parts.append(adapter.parse_chapter(html, title=ch.title))
                next_url = adapter.next_page_url(html, next_url)
            content = join_pages(parts)
            if is_generic and n == 1 and len(content) < 80:
                raise ValueError(
                    f"[自動偵測] 第一章只解析出 {len(content)} 字,通用模式可能抓錯正文區塊,"
                    "已中止下載;請回報網址讓我寫專屬 adapter")
            cache_file.write_text(content, encoding="utf-8")
            fetched += 1
            fetcher.polite_sleep()
        results.append((ch.title, content))
        callback("chapter", n, total, f"[{n}/{total}] {ch.title[:30]}")

    # 合併前後處理(只動輸出,不動快取):自訂規則 → 跨章重複樣板自動偵測
    # 提取網站識別符(優先用專用規則,找不到用全局)
    site_hint = adapter.domains[0] if hasattr(adapter, "domains") and adapter.domains else None
    rules = load_rules(site_hint)
    contents = [apply_rules(c, rules) for _, c in results]
    if rules:
        rule_file = f"filter_rules_{site_hint.replace('.', '_').lower()}.txt" if site_hint else "filter_rules.txt"
        callback("filter", 0, 1, f"[過濾] 已套用 {len(rules)} 條規則({rule_file})")
    contents, removed = drop_repeated(contents)
    if removed:
        preview = "|".join(p[:25] for p in removed[:3])
        callback("filter", 0, 1,
                 f"[自動去重] 移除 {len(removed)} 條跨章重複的宣傳/廣告樣板,如: {preview}")
    texts = [f"{t}\n\n{c}" for (t, _), c in zip(results, contents)]

    suffix = f"_第{lo}-{hi}章" if (start or end) else ""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    extension = ".epub" if output_format.lower() == "epub" else ".txt"
    out = output_path / f"{output_basename(book.title, book.author, site_hint or '', filename_format)}{suffix}{extension}"
    if extension == ".epub":
        write_epub(out, book.title, book.author, catalog_url,
                   [(t, c) for (t, _), c in zip(results, contents)])
    else:
        header = f"{book.title}\n作者: {book.author}\n來源: {catalog_url}\n"
        out.write_text(header + "\n\n" + "\n\n\n".join(texts) + "\n", encoding="utf-8")
    callback("done", total, total, f"完成!新抓 {fetched} 章、快取 {total - fetched} 章\n輸出: {out}")
    return out
