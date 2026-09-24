from zipfile import ZipFile
import threading
import time

import pytest

from downloader_task import atomic_write_text, chapter_number_warning, fetch_parsed_chapter, merge_split_chapters, safe_filename, save_progress, unique_chapters, write_epub, write_txt_from_files
from sites.base import Chapter
from sites.base import BookInfo
from sites.base import SiteAdapter
from sites.generic import GenericAdapter
from fetcher import FetchError


def test_write_epub_creates_readable_book(tmp_path):
    path = tmp_path / "book.epub"
    write_epub(path, "測試書", "測試作者", "https://example.com", [("第一章", "第一段\n第二段")])

    with ZipFile(path) as book:
        assert book.read("mimetype") == b"application/epub+zip"
        assert "OEBPS/content.opf" in book.namelist()
        assert "OEBPS/nav.xhtml" in book.namelist()
        assert "第一章" in book.read("OEBPS/chapter1.xhtml").decode("utf-8")


def test_streaming_outputs_read_chapters_from_cache_files(tmp_path):
    chapter = tmp_path / "0001.txt"
    chapter.write_text("原始正文", encoding="utf-8")
    txt = tmp_path / "book.txt"
    epub = tmp_path / "book.epub"

    write_txt_from_files(txt, "書名", [("第一章", chapter)], lambda text: text.replace("原始", "清理"))
    write_epub(epub, "書名", "作者", "https://example.com", [("第一章", chapter)],
               transform=lambda text: text.replace("原始", "清理"))

    assert "清理正文" in txt.read_text(encoding="utf-8")
    with ZipFile(epub) as book:
        assert "清理正文" in book.read("OEBPS/chapter1.xhtml").decode("utf-8")


def test_incremental_progress_drops_legacy_growing_chapter_list(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "progress.json").write_text('{"completed_chapters":[1,2,3]}', encoding="utf-8")
    save_progress(cache, completed_count=4, last_completed_chapter=4)
    progress = (cache / "progress.json").read_text(encoding="utf-8")
    assert "completed_chapters" not in progress


def test_progress_save_failure_does_not_abort_download(monkeypatch, tmp_path):
    import downloader_task

    def blocked_write(*_args, **_kwargs):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(downloader_task, "write_json", blocked_write)
    downloader_task.save_progress(tmp_path / "cache", completed_count=1)


def test_single_book_downloads_chapters_in_parallel_but_writes_catalog_order(monkeypatch, tmp_path):
    import downloader_task

    active = 0
    max_active = 0
    lock = threading.Lock()

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            nonlocal active, max_active
            if url == "catalog":
                return "catalog"
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return url

        def polite_sleep(self):
            pass

    class FakeAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["example.test"]
        is_generic = False

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None
        def parse_catalog(self, _html):
            return BookInfo("並行測試", "", [Chapter(f"第{i}章", f"chapter-{i}") for i in range(1, 5)])
        def book_id(self, _url): return "parallel-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: FakeAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel("https://example.test/book", tmp_path, delay=0, chapter_workers=3)
    text = output.read_text(encoding="utf-8")

    assert max_active >= 2
    assert text.index("第1章") < text.index("第2章") < text.index("第3章") < text.index("第4章")


def test_single_worker_reuses_catalog_session_for_protected_chapters(monkeypatch, tmp_path):
    import downloader_task

    instances = []

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None
            self.catalog_seen = False
            instances.append(self)

        def get(self, url, **_kwargs):
            if url == "catalog":
                self.catalog_seen = True
                return "catalog"
            if not self.catalog_seen:
                raise FetchError("HTTP 403: catalog session was lost")
            return "chapter"

        def polite_sleep(self):
            pass

    class ProtectedAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["protected.test"]
        is_generic = False
        max_chapter_workers = 1

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None
        def parse_catalog(self, _html):
            return BookInfo("受保護測試", "", [Chapter("第1章", "chapter-1")])
        def book_id(self, _url): return "protected-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, _html, title=""): return "正文"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: ProtectedAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel(
        "https://protected.test/book", tmp_path, delay=0, chapter_workers=3, retries=1
    )

    assert output.exists()
    assert len(instances) == 1


def test_parallel_workers_copy_catalog_session_cookies(monkeypatch, tmp_path):
    import downloader_task

    instances = []

    class FakeSession:
        def __init__(self):
            self.cookies = {}

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None
            self.session = FakeSession()
            instances.append(self)

        def get(self, url, **_kwargs):
            if url == "catalog":
                self.session.cookies["clearance"] = "ok"
                return "catalog"
            if self.session.cookies.get("clearance") != "ok":
                raise FetchError("HTTP 403: catalog cookies were lost")
            return url

        def polite_sleep(self):
            pass

    class ProtectedAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["parallel-protected.test"]
        is_generic = False
        max_chapter_workers = 2

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None
        def parse_catalog(self, _html):
            return BookInfo("並行受保護測試", "", [
                Chapter("第1章", "chapter-1"), Chapter("第2章", "chapter-2")
            ])
        def book_id(self, _url): return "parallel-protected-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: ProtectedAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel(
        "https://parallel-protected.test/book", tmp_path, delay=0,
        chapter_workers=2, retries=1
    )

    assert output.exists()
    assert len(instances) >= 2
    assert all(instance.session.cookies.get("clearance") == "ok" for instance in instances)


def test_fetch_parsed_chapter_retries_when_parser_gets_wrong_page():
    class FakeFetcher:
        def __init__(self):
            self.calls = 0

        def get(self, url, referer=None, retries=1):
            self.calls += 1
            return "book page" if self.calls == 1 else "chapter page"

    class FakeAdapter(SiteAdapter):
        def chapter_source_url(self, html, url):
            return None

        def next_page_url(self, html, url):
            return None

        def parse_chapter(self, html, title=""):
            if html == "book page":
                raise ValueError("章節頁找不到內文")
            return "正文"

    fetcher = FakeFetcher()
    content = fetch_parsed_chapter(fetcher, FakeAdapter(), Chapter("第1章", "https://example/ch1"), 3)

    assert content == "正文"
    assert fetcher.calls == 2


def test_fetch_parsed_chapter_retries_transient_fetch_failure():
    class FakeFetcher:
        def __init__(self):
            self.calls = 0

        def get(self, url, referer=None, retries=1):
            self.calls += 1
            if self.calls == 1:
                raise FetchError("temporary timeout")
            return "chapter page"

    class FakeAdapter(SiteAdapter):
        def chapter_source_url(self, html, url):
            return None

        def next_page_url(self, html, url):
            return None

        def parse_chapter(self, html, title=""):
            return "正文"

    fetcher = FakeFetcher()
    content = fetch_parsed_chapter(fetcher, FakeAdapter(), Chapter("第1章", "https://example/ch1"), 3)

    assert content == "正文"
    assert fetcher.calls == 2


def test_unique_chapters_keeps_first_url_and_order():
    chapters = [
        Chapter("第一章", "https://example/ch1"),
        Chapter("第一章重複", "https://example/ch1"),
        Chapter("第二章", "https://example/ch2"),
    ]

    assert [chapter.title for chapter in unique_chapters(chapters)] == ["第一章", "第二章"]


def test_merge_split_chapters_combines_consecutive_numbered_parts():
    chapters = [
        Chapter("第一章", "https://example/ch1"),
        Chapter("第一章(2)", "https://example/ch1_2"),
        Chapter("第一章(3)", "https://example/ch1_3"),
        Chapter("第二章", "https://example/ch2"),
    ]

    merged, happened = merge_split_chapters(chapters)

    assert happened is True
    assert [c.title for c in merged] == ["第一章", "第二章"]
    assert merged[0].url == "https://example/ch1"
    assert merged[0].extra_urls == ["https://example/ch1_2", "https://example/ch1_3"]
    assert merged[1].extra_urls == []


def test_merge_split_chapters_handles_fullwidth_and_bracket_suffixes():
    chapters = [
        Chapter("第一章（1）", "https://example/ch1"),
        Chapter("第一章（2）", "https://example/ch1_2"),
        Chapter("第二章【1】", "https://example/ch2"),
        Chapter("第二章【2】", "https://example/ch2_2"),
    ]

    merged, happened = merge_split_chapters(chapters)

    assert happened is True
    assert [c.title for c in merged] == ["第一章", "第二章"]


def test_merge_split_chapters_does_not_merge_non_consecutive_parts():
    chapters = [
        Chapter("第一章(1)", "https://example/ch1"),
        Chapter("第一章(3)", "https://example/ch1_3"),
    ]
    merged, happened = merge_split_chapters(chapters)
    assert happened is False
    assert [c.title for c in merged] == ["第一章(1)", "第一章(3)"]


def test_merge_split_chapters_does_not_merge_when_part_one_is_missing():
    """第 1 段不在(目錄漏抓或解析失敗)時,不能從第 2 段開始合併,那樣會把
    缺頭的內容悄悄藏成一個看似完整的章節(Codex review 抓到的 bug)。
    """
    chapters = [
        Chapter("第一章(2)", "https://example/ch1_2"),
        Chapter("第一章(3)", "https://example/ch1_3"),
    ]
    merged, happened = merge_split_chapters(chapters)
    assert happened is False
    assert [c.title for c in merged] == ["第一章(2)", "第一章(3)"]


def test_merge_split_chapters_does_not_merge_different_base_titles():
    chapters = [
        Chapter("第一章(1)", "https://example/ch1"),
        Chapter("第二章(2)", "https://example/ch2"),
    ]
    merged, happened = merge_split_chapters(chapters)
    assert happened is False
    assert [c.title for c in merged] == ["第一章(1)", "第二章(2)"]


def test_merge_split_chapters_keeps_unrelated_paginated_reading_pages():
    """52shuku 這類「目錄就是連續閱讀頁」的站,標題是「第1頁/第2頁」,
    不應該被誤判成同一章的拆分編號。
    """
    chapters = [Chapter("第1頁", "https://example/p1"), Chapter("第2頁", "https://example/p2")]
    merged, happened = merge_split_chapters(chapters)
    assert happened is False
    assert [c.title for c in merged] == ["第1頁", "第2頁"]


def test_fetch_parsed_chapter_fetches_extra_urls_and_shares_visited_set():
    class FakeFetcher:
        def __init__(self):
            self.calls = []

        def get(self, url, referer=None, retries=1):
            self.calls.append(url)
            return {"https://example/ch1": "P1", "https://example/ch1_2": "P2"}[url]

    class FakeAdapter(SiteAdapter):
        def chapter_source_url(self, html, url):
            return None

        def next_page_url(self, html, url):
            return None

        def parse_chapter(self, html, title=""):
            return html

    fetcher = FakeFetcher()
    chapter = Chapter("第一章", "https://example/ch1", extra_urls=["https://example/ch1_2", "https://example/ch1_2"])
    content = fetch_parsed_chapter(fetcher, FakeAdapter(), chapter, 1)

    assert content == "P1\n\nP2"
    assert fetcher.calls == ["https://example/ch1", "https://example/ch1_2"]  # 重複的 extra_url 不重抓


def test_fetch_parsed_chapter_retries_when_an_extra_page_is_empty(monkeypatch):
    import downloader_task

    class FakeFetcher:
        def __init__(self):
            self.calls = []

        def get(self, url, referer=None, retries=1):
            self.calls.append(url)
            if url.endswith("ch1_2") and self.calls.count(url) == 1:
                return "empty page"
            return "page 2" if url.endswith("ch1_2") else "page 1"

    class FakeAdapter(SiteAdapter):
        def chapter_source_url(self, html, url): return None
        def next_page_url(self, html, url): return None

        def parse_chapter(self, html, title=""):
            return "" if html == "empty page" else ("P1" if html.endswith("1") else "P2")

    monkeypatch.setattr(downloader_task.time, "sleep", lambda _seconds: None)
    fetcher = FakeFetcher()
    chapter = Chapter("第一章", "https://example/ch1", extra_urls=["https://example/ch1_2"])
    assert fetch_parsed_chapter(fetcher, FakeAdapter(), chapter, 2) == "P1\n\nP2"
    assert fetcher.calls == [
        "https://example/ch1", "https://example/ch1_2",
        "https://example/ch1", "https://example/ch1_2",
    ]


def test_fetch_parsed_chapter_treats_empty_content_as_failure_and_retries():
    class FakeFetcher:
        def __init__(self):
            self.calls = 0

        def get(self, url, referer=None, retries=1):
            self.calls += 1
            return "empty page" if self.calls == 1 else "real page"

    class FakeAdapter(SiteAdapter):
        def chapter_source_url(self, html, url):
            return None

        def next_page_url(self, html, url):
            return None

        def parse_chapter(self, html, title=""):
            return "" if html == "empty page" else "正文"

    fetcher = FakeFetcher()
    content = fetch_parsed_chapter(fetcher, FakeAdapter(), Chapter("第1章", "https://example/ch1"), 3)
    assert content == "正文"
    assert fetcher.calls == 2


def test_fetch_parsed_chapter_raises_after_exhausting_retries_on_empty_content():
    class FakeFetcher:
        def get(self, url, referer=None, retries=1):
            return "empty page"

    class FakeAdapter(SiteAdapter):
        def chapter_source_url(self, html, url):
            return None

        def next_page_url(self, html, url):
            return None

        def parse_chapter(self, html, title=""):
            return ""

    try:
        fetch_parsed_chapter(FakeFetcher(), FakeAdapter(), Chapter("第1章", "https://example/ch1"), 2)
    except ValueError as exc:
        assert "空" in str(exc)
    else:
        raise AssertionError("empty content should raise after exhausting retries")


def test_atomic_write_replaces_part_file(tmp_path):
    path = tmp_path / "chapter.txt"
    atomic_write_text(path, "完整正文")

    assert path.read_text(encoding="utf-8") == "完整正文"
    assert not (tmp_path / "chapter.txt.part").exists()


def test_safe_filename_handles_windows_reserved_and_trailing_chars():
    assert safe_filename("CON") == "_CON"
    assert safe_filename("CON.txt") == "_CON.txt"
    assert safe_filename('書名:*?. ') == "書名___"


def test_chapter_number_warning_reports_real_gap():
    chapters = [
        Chapter("第1章", "https://example/ch1"),
        Chapter("第3章", "https://example/ch3"),
    ]

    warning = chapter_number_warning(chapters)
    assert "缺少章號 2" in warning


def test_chapter_number_warning_accepts_contiguous_non_one_start():
    chapters = [
        Chapter("第100章", "https://example/ch100"),
        Chapter("第101章", "https://example/ch101"),
    ]

    assert chapter_number_warning(chapters) == ""


def _tolerance_setup(monkeypatch, tmp_path, failing_urls, chapters=6, fail_times=None):
    """建立一本 `chapters` 章的假書；failing_urls 的章節會丟 FetchError。

    fail_times=None 表示永遠失敗；給數字則只失敗前 N 次（模擬網站暫時性 404）。
    """
    import downloader_task

    attempts = {}

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            if url == "catalog":
                return "catalog"
            if url in failing_urls:
                attempts[url] = attempts.get(url, 0) + 1
                if fail_times is None or attempts[url] <= fail_times:
                    raise FetchError(f"抓取失敗 {url}: HTTP 404")
            return url

        def polite_sleep(self):
            pass

    class FakeAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["example.test"]
        is_generic = False

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None
        def parse_catalog(self, _html):
            return BookInfo("容錯測試", "", [
                Chapter(f"第{i}章", f"chapter-{i}") for i in range(1, chapters + 1)])
        def book_id(self, _url): return "tolerance-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: FakeAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])
    monkeypatch.setattr(downloader_task, "RETRY_COOLDOWN", 0)
    monkeypatch.setattr(downloader_task.time, "sleep", lambda _seconds: None)
    return downloader_task


def test_a_few_failed_chapters_do_not_kill_the_whole_book(monkeypatch, tmp_path):
    messages = []
    downloader_task = _tolerance_setup(monkeypatch, tmp_path, {"chapter-3"})

    output = downloader_task.download_novel(
        "https://example.test/book", tmp_path, delay=0, chapter_workers=2,
        callback=lambda stage, current, total, msg: messages.append(msg))
    text = output.read_text(encoding="utf-8")

    assert "第2章" in text and "第4章" in text
    assert "正文 chapter-3" not in text          # 失敗章節不會寫進輸出
    assert any("第3章" in m and "抓取失敗" in m for m in messages)
    assert any(m.startswith("[重試] 1 章第一輪失敗") for m in messages)
    assert any(m.startswith("[略過] 1 章抓取失敗") for m in messages)
    assert not (tmp_path / "cache" / "tolerance-test" / "0003.txt").exists()  # 不留壞快取


def test_too_many_failed_chapters_still_abort(monkeypatch, tmp_path):
    downloader_task = _tolerance_setup(
        monkeypatch, tmp_path, {"chapter-2", "chapter-3", "chapter-4", "chapter-5"})

    try:
        downloader_task.download_novel(
            "https://example.test/book", tmp_path, delay=0, chapter_workers=2)
    except FetchError as error:
        assert "重試後仍有" in str(error) and "中止下載" in str(error)
    else:
        raise AssertionError("too many failures should abort")


def test_temporary_404_recovers_on_the_slow_retry_pass(monkeypatch, tmp_path):
    """網站忙碌時整批回 404：第一輪失敗的章節在冷卻後單執行緒重抓回來。"""
    messages = []
    downloader_task = _tolerance_setup(
        monkeypatch, tmp_path, {"chapter-2", "chapter-3", "chapter-4", "chapter-5"},
        fail_times=5)  # 第一輪的 5 次內部重試全失敗，冷卻後的重試輪才成功

    output = downloader_task.download_novel(
        "https://example.test/book", tmp_path, delay=0, chapter_workers=3,
        callback=lambda stage, current, total, msg: messages.append(msg))
    text = output.read_text(encoding="utf-8")

    for n in range(1, 7):
        assert f"正文 chapter-{n}" in text          # 六章全部都在,順序照目錄
    assert text.index("第2章") < text.index("第3章") < text.index("第4章")
    assert any(m.startswith("[重試] 4 章第一輪失敗") for m in messages)
    assert sum(m.startswith("[重試成功]") for m in messages) == 4
    assert not any(m.startswith("[略過]") for m in messages)


def test_catalog_expansion_and_pagination_are_followed(monkeypatch, tmp_path):
    """目錄摺疊需要展開,展開後的完整目錄還分頁:兩者都要跟著抓。"""
    import downloader_task

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            return url

        def polite_sleep(self):
            pass

    class PagedAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["paged.test"]
        is_generic = False

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None

        def full_catalog_url(self, html, _url):
            return "full-catalog" if html == "catalog" else None

        def parse_catalog(self, html):
            assert html == "full-catalog"
            return BookInfo("分頁測試", "", [Chapter("第1章", "chapter-1"), Chapter("第2章", "chapter-2")])

        def catalog_page_urls(self, html, url):
            return ["page2"] if url == "full-catalog" else []

        def parse_catalog_page(self, html, url):
            if url == "full-catalog":
                return self.parse_catalog(html)  # download_novel 現在也用這個 hook 解析首頁
            assert url == "page2"
            return BookInfo("", "", [Chapter("第3章", "chapter-3"), Chapter("第4章", "chapter-4")])

        def book_id(self, _url): return "paged-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: PagedAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    messages = []
    output = downloader_task.download_novel(
        "https://paged.test/book", tmp_path, delay=0, chapter_workers=1,
        callback=lambda stage, current, total, msg: messages.append(msg))
    text = output.read_text(encoding="utf-8")

    assert all(f"第{i}章" in text for i in range(1, 5))
    assert any("已展開完整目錄" in m for m in messages)
    assert any(m.startswith("[目錄分頁] 共抓取 1 頁") for m in messages)


def test_catalog_pagination_stops_on_a_to_b_to_a_loop_with_no_new_chapters(monkeypatch, tmp_path):
    """A→B→A 這種分頁循環,且 B 頁沒有任何新章節網址,必須立即停止,不能無窮抓取。"""
    import downloader_task

    fetch_log = []

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            fetch_log.append(url)
            return url

        def polite_sleep(self):
            pass

    class LoopyAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["loopy.test"]
        is_generic = False

        def catalog_url(self, _url): return "page-a"
        def meta_url(self, _url): return None

        def parse_catalog(self, _html):
            return BookInfo("循環測試", "", [Chapter("第1章", "chapter-1")])

        def catalog_page_urls(self, _html, url):
            return ["page-b"] if url == "page-a" else ["page-a"]

        def parse_catalog_page(self, _html, _url):
            # page-b 沒有任何新章節(和 page-a 的章節完全重複)
            return BookInfo("", "", [Chapter("第1章", "chapter-1")])

        def book_id(self, _url): return "loopy-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: LoopyAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel("https://loopy.test/book", tmp_path, delay=0, chapter_workers=1)

    assert output.exists()
    assert fetch_log.count("page-a") == 1  # 只在一開始抓一次目錄,不會被 B 的分頁連結帶回頭重抓
    assert fetch_log.count("page-b") == 1


def test_catalog_pagination_continues_past_a_duplicate_alias_page(monkeypatch, tmp_path):
    """佇列裡先出現一個沒有新章節的重複/別名頁,不能因此整批放棄後面排隊、
    真正有新章節的分頁(Codex review 抓到的 bug:原本遇到 0 新章節就整個 break)。
    """
    import downloader_task

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            return url

        def polite_sleep(self):
            pass

    class DupeAliasAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["dupe.test"]
        is_generic = False

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None

        def parse_catalog(self, _html):
            return BookInfo("重複頁測試", "", [Chapter("第1章", "chapter-1")])

        def catalog_page_urls(self, _html, url):
            # 第一頁同時連到一個內容重複的別名頁,和真正有新章節的第二頁
            return ["dup-alias", "page2"] if url == "catalog" else []

        def parse_catalog_page(self, _html, url):
            if url == "dup-alias":
                return BookInfo("", "", [Chapter("第1章", "chapter-1")])  # 沒有新章節
            return BookInfo("", "", [Chapter("第2章", "chapter-2")])

        def book_id(self, _url): return "dupe-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: DupeAliasAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel("https://dupe.test/book", tmp_path, delay=0, chapter_workers=1)
    text = output.read_text(encoding="utf-8")

    assert "第1章" in text and "第2章" in text  # page2 沒有被 dup-alias 連累而漏抓


def test_catalog_pagination_fails_instead_of_silently_truncating_at_page_cap(monkeypatch, tmp_path):
    import downloader_task

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            return url

        def polite_sleep(self):
            pass

    class LongCatalogAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["long-catalog.test"]
        is_generic = False

        def catalog_url(self, _url): return "https://long-catalog.test/page/0"
        def meta_url(self, _url): return None
        def full_catalog_url(self, _html, _url): return None

        def catalog_page_urls(self, _html, url):
            page = int(url.rsplit("/", 1)[-1])
            return [f"https://long-catalog.test/page/{page + 1}"]

        def parse_catalog_page(self, _html, url):
            page = int(url.rsplit("/", 1)[-1])
            return BookInfo("長目錄", "", [Chapter(f"第{page + 1}章", f"chapter-{page + 1}")])

        def parse_catalog(self, html): return self.parse_catalog_page(html, html)
        def book_id(self, _url): return "long-catalog"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: LongCatalogAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])
    monkeypatch.setattr(downloader_task, "MAX_CATALOG_PAGES", 2)

    with pytest.raises(ValueError, match="目錄分頁超過安全上限"):
        downloader_task.download_novel(
            "https://long-catalog.test/book", tmp_path, delay=0, chapter_workers=1)


def _merge_test_setup(monkeypatch, tmp_path, titles):
    import downloader_task

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            return url

        def polite_sleep(self):
            pass

    class SplitAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["split.test"]
        is_generic = False

        def catalog_url(self, _url): return "catalog"
        def meta_url(self, _url): return None

        def parse_catalog(self, _html):
            return BookInfo("拆頁測試", "", [Chapter(t, f"ch-{i}") for i, t in enumerate(titles, 1)])

        def book_id(self, _url): return "split-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: SplitAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])
    return downloader_task


def test_catalog_expansion_resolves_relative_links_against_the_expanded_url(monkeypatch, tmp_path):
    """展開完整目錄後,頁面上的相對連結要對著展開後的網址解析,不是展開前的舊網址
    (Codex review 抓到的 bug:GenericAdapter._base_url 沒有跟著 full_catalog_url 更新)。
    """
    import downloader_task

    class ExpandingAdapter(GenericAdapter):
        domains = ["expand.test"]

        def catalog_url(self, url):
            self._base_url = url
            return url

    pages = {
        "https://expand.test/n/1": (
            '<meta property="og:novel:book_name" content="Expanded Metadata Fallback">'
            '<meta property="og:novel:author" content="Preview Author">'
            '<a href="/n/1/full.html">查看全部章節</a>'
        ),
        "https://expand.test/n/1/full.html": "".join(
            f'<a href="{i}.html">第{i}章</a>' for i in range(1, 6)),
    }
    fetched_urls = []

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            fetched_urls.append(url)
            return pages.get(url, f"<article>{'正文內容' * 20}</article>")  # 湊滿 GenericAdapter 首章長度檢查

        def polite_sleep(self):
            pass

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: ExpandingAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel(
        "https://expand.test/n/1", tmp_path, delay=0, chapter_workers=1)

    expected = {f"https://expand.test/n/1/{i}.html" for i in range(1, 6)}
    assert expected.issubset(set(fetched_urls))
    assert output.name == "Expanded Metadata Fallback.txt"
    wrong = {f"https://expand.test/n/{i}.html" for i in range(1, 6)}  # 用舊網址解析會得到這種錯誤路徑
    assert not (wrong & set(fetched_urls))


def test_merged_split_chapters_use_a_separate_cache_namespace(monkeypatch, tmp_path):
    """目錄把章節拆成多個項目、合併下載後,快取資料夾要換一個命名空間,
    避免舊的(依合併前位置編號的)快取對不上合併後的新章節順序。
    """
    downloader_task = _merge_test_setup(
        monkeypatch, tmp_path, ["第一章", "第一章(2)", "第二章"])

    messages = []
    output = downloader_task.download_novel(
        "https://split.test/book", tmp_path, delay=0, chapter_workers=1,
        callback=lambda stage, current, total, msg: messages.append(msg))

    assert (tmp_path / "cache" / "split-test-merged").exists()
    assert not (tmp_path / "cache" / "split-test").exists()
    assert any(m.startswith("[合併分頁章節] 由 3 個目錄項目合併為 2 章") for m in messages)
    text = output.read_text(encoding="utf-8")
    assert "正文 ch-1" in text and "正文 ch-2" in text  # 合併後第一章的正文含 extra_urls(ch-2)


def test_catalog_size_change_warns_about_possibly_stale_cache(monkeypatch, tmp_path):
    """重跑時目錄章數變了,且已有快取檔案:必須提醒使用者章號可能對不上,不能默默沿用。"""
    downloader_task = _merge_test_setup(monkeypatch, tmp_path, ["第一章", "第二章", "第三章"])
    cache_dir = tmp_path / "cache" / "split-test"
    cache_dir.mkdir(parents=True)
    (cache_dir / "0001.txt").write_text("舊快取", encoding="utf-8")
    downloader_task.save_progress(cache_dir, total_chapters=5)

    messages = []
    downloader_task.download_novel(
        "https://split.test/book", tmp_path, delay=0, chapter_workers=1,
        callback=lambda stage, current, total, msg: messages.append(msg))

    assert any("[快取提醒]" in m and "5" in m and "3" in m for m in messages)


def test_order_catalog_pages_restores_page_number_order():
    from downloader_task import order_catalog_pages

    pages = [("https://x.test/list_2.html", ["b"]), ("https://x.test/list_1.html", ["a"]),
             ("https://x.test/list_3.html", ["c"])]
    assert [chapters for _url, chapters in order_catalog_pages(pages)] == [["a"], ["b"], ["c"]]


def test_order_catalog_pages_places_unnumbered_page_one_before_numbered_pages():
    from downloader_task import order_catalog_pages

    pages = [
        ("https://x.test/book/index_2.html", ["b"]),
        ("https://x.test/book/index.html", ["a"]),
        ("https://x.test/book/index_3.html", ["c"]),
    ]
    assert [chapters for _url, chapters in order_catalog_pages(pages)] == [["a"], ["b"], ["c"]]


def test_order_catalog_pages_places_unnumbered_query_page_one_first():
    from downloader_task import order_catalog_pages

    pages = [
        ("https://x.test/catalog?page=2", ["b"]),
        ("https://x.test/catalog", ["a"]),
        ("https://x.test/catalog?page=3", ["c"]),
    ]
    assert [chapters for _url, chapters in order_catalog_pages(pages)] == [["a"], ["b"], ["c"]]


def test_order_catalog_pages_keeps_fetch_order_when_url_patterns_differ():
    from downloader_task import order_catalog_pages

    pages = [("https://x.test/list_2.html", ["b"]), ("https://x.test/other/a.html", ["a"])]
    assert order_catalog_pages(pages) == pages


def test_catalog_starting_on_a_later_page_still_outputs_chapters_in_page_order(monkeypatch, tmp_path):
    """使用者貼第 2 頁,分頁控制項同時連到第 1、3 頁:章節必須是 1、2、3 頁的順序
    (Codex review 抓到的 bug:原本依抓取順序變成 2、1、3)。
    """
    import downloader_task

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.throttle = None

        def get(self, url, **_kwargs):
            return url

        def polite_sleep(self):
            pass

    class MiddlePageAdapter(SiteAdapter):
        encoding = "utf-8"
        domains = ["order.test"]
        is_generic = False

        def catalog_url(self, _url): return "https://order.test/list_2.html"
        def meta_url(self, _url): return None

        def catalog_page_urls(self, _html, url):
            if url == "https://order.test/list_2.html":
                return ["https://order.test/list_1.html", "https://order.test/list_3.html"]
            return []

        def parse_catalog_page(self, _html, url):
            page = url.rsplit("_", 1)[1].split(".")[0]
            return BookInfo("順序測試", "", [Chapter(f"第{page}章", f"chapter-{page}")])

        def book_id(self, _url): return "order-test"
        def chapter_source_url(self, _html, _url): return None
        def next_page_url(self, _html, _url): return None
        def parse_chapter(self, html, title=""): return f"正文 {html}"

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "get_adapter", lambda _url: MiddlePageAdapter())
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    output = downloader_task.download_novel("https://order.test/list_2.html", tmp_path, delay=0, chapter_workers=1)
    text = output.read_text(encoding="utf-8")

    assert text.index("第1章") < text.index("第2章") < text.index("第3章")


def test_merge_split_chapters_honors_declared_total_and_refuses_incomplete_groups():
    """標題寫明「(1/5)、(2/5)」卻只有前兩段時,不能合併成看似完整的章節
    (Codex review 抓到的 bug:總段數被丟掉,只檢查編號連續)。
    """
    partial = [Chapter("第一章(1/5)", "https://example/1"), Chapter("第一章(2/5)", "https://example/2")]
    merged, happened = merge_split_chapters(partial)
    assert happened is False
    assert [c.title for c in merged] == ["第一章(1/5)", "第一章(2/5)"]


def test_merge_split_chapters_merges_when_all_declared_parts_are_present():
    # 全形數字、斜線與括號會先經 NFKC 正規化。
    full = [Chapter(f"第一章（{i}／３）", f"https://example/{i}") for i in (1, 2, 3)]
    merged, happened = merge_split_chapters(full)
    assert happened is True
    assert [c.title for c in merged] == ["第一章"]
    assert merged[0].extra_urls == ["https://example/2", "https://example/3"]


def test_merge_split_chapters_refuses_missing_declared_middle_part():
    missing_middle = [Chapter("第一章(1/3)", "https://example/1"), Chapter("第一章(3/3)", "https://example/3")]
    merged, happened = merge_split_chapters(missing_middle)
    assert happened is False
    assert [c.title for c in merged] == ["第一章(1/3)", "第一章(3/3)"]


def test_merge_split_chapters_refuses_inconsistent_declared_totals():
    mixed = [
        Chapter("第一章(1/3)", "https://example/1"),
        Chapter("第一章(2/4)", "https://example/2"),
        Chapter("第一章(3/4)", "https://example/3"),
        Chapter("第一章(4/4)", "https://example/4"),
    ]
    merged, happened = merge_split_chapters(mixed)
    assert happened is False
    assert [c.title for c in merged] == [chapter.title for chapter in mixed]
