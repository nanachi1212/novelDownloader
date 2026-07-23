from zipfile import ZipFile

from downloader_task import write_epub


def test_write_epub_creates_readable_book(tmp_path):
    path = tmp_path / "book.epub"
    write_epub(path, "測試書", "測試作者", "https://example.com", [("第一章", "第一段\n第二段")])

    with ZipFile(path) as book:
        assert book.read("mimetype") == b"application/epub+zip"
        assert "OEBPS/content.opf" in book.namelist()
        assert "OEBPS/nav.xhtml" in book.namelist()
        assert "第一章" in book.read("OEBPS/chapter1.xhtml").decode("utf-8")
