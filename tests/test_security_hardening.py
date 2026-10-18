import pytest

import downloader_task
import main_window
from fetcher import Fetcher, cookie_allowed
from sites import get_adapter
from test_fetcher import FakeResponse, FakeSession
from test_gui_behavior import FakeCookie


def test_browser_cookie_import_drops_cookies_from_other_domains():
    class FakeModule:
        @staticmethod
        def firefox(domain_name):
            # browser_cookie3 以 LIKE %acebook.co% 篩選,會連 .facebook.com 一起回傳
            return [FakeCookie("sid", "victim", ".facebook.com"),
                    FakeCookie("cf_clearance", "ok", ".acebook.co")]

    _label, header, _problems = main_window.read_browser_cookies("acebook.co", FakeModule)
    assert header == "cf_clearance=ok"


def test_imported_queue_row_cannot_choose_site_key():
    job = main_window.queue_job_from_row(
        {"url": "https://attacker.example/book/1/", "site_key": "xbanxia.cc"})
    assert job["site_key"] == "attacker.example"


def test_cookie_only_sent_over_https_to_matching_host():
    assert cookie_allowed("https://www.xbanxia.cc/books/1.html", "xbanxia.cc")
    assert not cookie_allowed("http://www.xbanxia.cc/books/1.html", "xbanxia.cc")
    assert not cookie_allowed("https://evilxbanxia.cc/", "xbanxia.cc")
    assert not cookie_allowed("https://attacker.example/", "xbanxia.cc")


def test_fetcher_strips_cookie_on_redirect_to_other_host():
    fetcher = Fetcher(headers={"Cookie": "sid=1"}, cookie_domain="site.example")
    fetcher.session = FakeSession([
        FakeResponse('<script>window.location.href = "https://other.example/c";</script>'),
        FakeResponse("<html><body>正文</body></html>"),
    ])
    fetcher.get("https://www.site.example/book")
    assert fetcher.session.calls[0][1]["Cookie"] == "sid=1"
    assert "Cookie" not in fetcher.session.calls[1][1]


@pytest.mark.parametrize("book_id", ["..", ".", r"..\..\evil", "../evil", "a/b"])
def test_book_cache_dir_rejects_escape(monkeypatch, tmp_path, book_id):
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    with pytest.raises(ValueError):
        downloader_task.book_cache_dir(book_id)
    assert downloader_task.book_cache_dir("xbanxia-1") == (tmp_path / "cache" / "xbanxia-1").resolve()


def test_adapters_reject_traversal_book_ids():
    evil = r"https://www.sunzhinan.com/books/..\..\x/books/1/"
    with pytest.raises(ValueError):
        get_adapter(evil).book_id(evil)
    oop = get_adapter("https://www.oop.tw/abooka/a31674962a/")
    with pytest.raises(ValueError):
        oop.book_id(r"https://www.oop.tw/abooka/a1#..\..\x/")
    assert oop.book_id("https://www.oop.tw/abooka/a31674962a/") == "oop-tw-a31674962a"
    assert get_adapter("https://www.xbanxia.cc/books/1.html").catalog_url(
        "http://www.xbanxia.cc/books/1.html").startswith("https://")


def test_fetcher_rechecks_cookie_on_each_http_redirect():
    fetcher = Fetcher(headers={"Cookie": "sid=1"}, cookie_domain="site.example")
    fetcher.session = FakeSession([
        FakeResponse("", 301, {"Location": "https://www.site.example/book"}),
        FakeResponse("", 302, {"Location": "https://other.example/book"}),
        FakeResponse("<html><body>正文</body></html>"),
    ])
    fetcher.get("https://site.example/book")
    sent = [call[1].get("Cookie") for call in fetcher.session.calls]
    assert sent == ["sid=1", "sid=1", None]


def test_fetcher_treats_cookie_header_name_case_insensitively():
    fetcher = Fetcher(headers={"cookie": "sid=1"}, cookie_domain="site.example")
    fetcher.session = FakeSession([
        FakeResponse('<script>window.location.href = "https://other.example/c";</script>'),
        FakeResponse("<html><body>正文</body></html>"),
    ])
    fetcher.get("https://site.example/book")
    first, second = (call[1] for call in fetcher.session.calls)
    assert first["Cookie"] == "sid=1"
    assert not any(key.lower() == "cookie" for key in second)
