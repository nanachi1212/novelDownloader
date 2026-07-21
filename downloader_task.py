"""下載任務核心:CLI 與 GUI 共用,支援章節範圍、快取斷點續傳、進度回報。"""
import re
import sys
from pathlib import Path

from fetcher import Fetcher
from sites import get_adapter
from sites.base import join_pages


class Cancelled(Exception):
    """使用者取消下載。"""


def cache_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "cache"
    return Path(__file__).parent / "cache"


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "novel"


def download_novel(url, output_dir, title_override="", delay=2.0, callback=None,
                   start=None, end=None, cancel_check=None):
    """下載小說並輸出 TXT,回傳輸出檔路徑。

    callback(stage, current, total, msg),stage: 'catalog'|'chapter'|'done'
    start/end:1-based 章節範圍(含端點),None 表示不限
    cancel_check:回傳 True 時在章節邊界中止,拋出 Cancelled
    """
    callback = callback or (lambda *a: None)
    adapter = get_adapter(url)
    fetcher = Fetcher(encoding=adapter.encoding, delay=delay)

    callback("catalog", 0, 1, "正在抓取目錄...")
    catalog_url = adapter.catalog_url(url)
    meta_url = adapter.meta_url(url)
    if meta_url:
        title, author = adapter.parse_meta(fetcher.get(meta_url))
    else:
        title = author = ""
    book = adapter.parse_catalog(fetcher.get(catalog_url))
    if title:
        book.title = title
    if author:
        book.author = author
    if title_override:
        book.title = title_override

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

    texts = []
    fetched = 0
    for n, (idx, ch) in enumerate(jobs, 1):
        if cancel_check and cancel_check():
            raise Cancelled("使用者中止,已下載的章節保留在快取,重跑會續傳")
        cache_file = cache / f"{idx:04d}.txt"  # 用全書絕對章號命名,範圍下載也能共用快取
        if cache_file.exists():
            content = cache_file.read_text(encoding="utf-8")
        else:
            html = fetcher.get(ch.url)
            parts = [adapter.parse_chapter(html, title=ch.title)]
            next_url = adapter.next_page_url(html, ch.url)
            seen = {ch.url}
            while next_url and next_url not in seen:
                seen.add(next_url)
                html = fetcher.get(next_url)
                parts.append(adapter.parse_chapter(html, title=ch.title))
                next_url = adapter.next_page_url(html, next_url)
            content = join_pages(parts)
            cache_file.write_text(content, encoding="utf-8")
            fetched += 1
            fetcher.polite_sleep()
        texts.append(f"{ch.title}\n\n{content}")
        callback("chapter", n, total, f"[{n}/{total}] {ch.title[:30]}")

    suffix = f"_第{lo}-{hi}章" if (start or end) else ""
    out = Path(output_dir) / f"{safe_filename(book.title)}{suffix}.txt"
    header = f"{book.title}\n作者: {book.author}\n來源: {catalog_url}\n"
    out.write_text(header + "\n\n" + "\n\n\n".join(texts) + "\n", encoding="utf-8")
    callback("done", total, total, f"完成!新抓 {fetched} 章、快取 {total - fetched} 章\n輸出: {out}")
    return out
