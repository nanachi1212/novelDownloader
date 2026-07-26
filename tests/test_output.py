from zipfile import ZipFile

from downloader_task import fetch_parsed_chapter, write_epub
from sites.base import Chapter


def test_write_epub_creates_readable_book(tmp_path):
    path = tmp_path / "book.epub"
    write_epub(path, "測試書", "測試作者", "https://example.com", [("第一章", "第一段\n第二段")])

    with ZipFile(path) as book:
        assert book.read("mimetype") == b"application/epub+zip"
        assert "OEBPS/content.opf" in book.namelist()
        assert "OEBPS/nav.xhtml" in book.namelist()
        assert "第一章" in book.read("OEBPS/chapter1.xhtml").decode("utf-8")


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
