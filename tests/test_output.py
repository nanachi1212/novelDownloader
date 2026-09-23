from zipfile import ZipFile
import threading
import time

from downloader_task import atomic_write_text, chapter_number_warning, fetch_parsed_chapter, safe_filename, save_progress, unique_chapters, write_epub, write_txt_from_files
from sites.base import Chapter
from sites.base import BookInfo
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

    class FakeAdapter:
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

    class ProtectedAdapter:
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

    class ProtectedAdapter:
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

    class FakeAdapter:
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

    class FakeAdapter:
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

    class FakeAdapter:
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
