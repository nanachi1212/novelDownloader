"""Synthetic Fanqie download tests; no live-site requests or novel text."""
import json
import zipfile

import pytest

import downloader_task
from fanqie_decoder import CHARSETS, DecodeFailed, decode_chapter, decode_pua
from fetcher import FetchError
from sites.fanqie import AccessVerificationRequired, FanqieAdapter, FanqieError


BOOK_ID = "999"
ITEM_ID = "101"
BOOK_URL = f"https://fanqienovel.com/page/{BOOK_ID}"
READER_URL = f"https://fanqienovel.com/reader/{ITEM_ID}"
PUBLIC = {"needPay": 0, "isPaidPublication": False,
          "isPaidStory": False, "isChapterLock": False}
RAW = "".join(chr(0xE3E8 + CHARSETS[0].index(char)) for char in "人在这里") * 12


def _state(section, data, *, js_undefined=False):
    encoded = json.dumps({section: data, "note": ":undefined,"}, ensure_ascii=False)
    if js_undefined:
        encoded = encoded[:-1] + ',"optional":undefined}'
    return f"<html><script>window.__INITIAL_STATE__ = {encoded};</script></html>"


def _meta(book_id=BOOK_ID):
    return _state("page", {"bookId": book_id, "bookName": "合成測試書", "author": "測試作者"})


def _directory(*items):
    return json.dumps({"code": 0, "data": {
        "chapterListWithVolume": [list(items)],
        "allItemIds": [item["itemId"] for item in items],
        "volumeNameList": ["第一卷"],
    }}, ensure_ascii=False)


def _item(item_id=ITEM_ID, **flags):
    return {"itemId": item_id, "title": "第1章", "volume_name": "第一卷",
            **(flags or PUBLIC)}


def _reader(item_id=ITEM_ID, book_id=BOOK_ID, content=None, **flags):
    return _state("reader", {"chapterData": {
        "itemId": item_id, "bookId": book_id, "title": "第1章",
        "content": f"<p>{RAW}</p>" if content is None else content,
        **(flags or PUBLIC),
    }}, js_undefined=True)


def _adapter():
    adapter = FanqieAdapter()
    adapter.catalog_url(BOOK_URL)
    adapter.parse_meta(_meta())
    adapter.parse_catalog(_directory(_item()))
    return adapter


def test_fixed_tables_decode_only_known_private_use_positions():
    assert [len(table) for table in CHARSETS] == [372, 372]
    assert decode_pua("A\ue3e8\ue3e9\uf000", 0) == "AD在\uf000"
    assert decode_pua("A\ue3e9\ue55c\uf000", 1) == "As\ue55c\uf000"
    assert decode_chapter(RAW).mode == 0
    mode_one = decode_chapter("\ue3f6" * 30)
    assert (mode_one.mode, mode_one.text) == (1, "才" * 30)
    with pytest.raises(DecodeFailed, match="DECODE_FAILED"):
        decode_chapter("\uf000" * 30)


def test_directory_becomes_ordered_bookinfo_and_fails_closed():
    adapter = FanqieAdapter()
    assert adapter.catalog_url(BOOK_URL).endswith("bookId=999")
    assert adapter.meta_url(BOOK_URL) == BOOK_URL
    assert adapter.parse_meta(_meta()) == ("合成測試書", "測試作者")
    second = _item("202", isChapterLock=True)
    book = adapter.parse_catalog(_directory(_item(), second))
    assert (book.title, book.author) == ("合成測試書", "測試作者")
    assert [(chapter.title, chapter.url) for chapter in book.chapters] == [
        ("第1章", READER_URL), ("第1章", "https://fanqienovel.com/reader/202")]
    adapter.validate_download_chapter(book.chapters[0])
    with pytest.raises(AccessVerificationRequired):
        adapter.validate_download_chapter(book.chapters[1])
    book.chapters[0].extra_urls.append(book.chapters[1].url)
    with pytest.raises(AccessVerificationRequired):
        adapter.validate_download_chapter(book.chapters[0])
    with pytest.raises(FanqieError):
        adapter.parse_meta(_meta("wrong"))
    paid_directory = adapter.parse_catalog(_directory(_item(accessStatus="paid", **PUBLIC)))
    with pytest.raises(AccessVerificationRequired):
        adapter.validate_download_chapter(paid_directory.chapters[0])


def test_initial_state_content_identity_access_and_challenge():
    adapter = _adapter()
    adapter.chapter_source_url(_reader(), READER_URL)
    assert adapter.parse_chapter(_reader(), "第1章") == "人在这里" * 12
    assert ":undefined," in _state("note", {"value": ":undefined,"})
    with pytest.raises(FanqieError, match="itemId"):
        adapter.parse_chapter(_reader(item_id="202"))
    with pytest.raises(FanqieError, match="bookId"):
        adapter.parse_chapter(_reader(book_id="888"))
    with pytest.raises(AccessVerificationRequired):
        adapter.parse_chapter(_reader(isChapterLock=True))
    with pytest.raises(AccessVerificationRequired):
        adapter.parse_chapter("<html>bdturing challenge</html>")


def test_reader_dom_fallback_requires_matching_state():
    adapter = _adapter()
    adapter.chapter_source_url("", READER_URL)
    html = _reader(content="")
    html = html.replace("</html>", f'<div class="muye-reader-content"><p>{RAW}</p></div></html>')
    assert adapter.parse_chapter(html) == "人在这里" * 12


def test_shortened_reader_body_is_rejected():
    adapter = _adapter()
    adapter.chapter_source_url("", READER_URL)
    html = _reader().replace('"content":', '"chapterWordNumber": 2000, "content":')
    with pytest.raises(FanqieError, match="截斷"):
        adapter.parse_chapter(html)


@pytest.mark.parametrize("status", ["paid", "vip", "unknown-state", 0])
def test_non_public_access_status_never_exports_chapter(status):
    adapter = _adapter()
    adapter.chapter_source_url("", READER_URL)
    html = _reader().replace('"content":', f'"accessStatus":{json.dumps(status)},"content":')
    with pytest.raises(AccessVerificationRequired):
        adapter.parse_chapter(html)


def test_fanqie_txt_epub_resume_and_challenge_never_cached(tmp_path, monkeypatch):
    calls = []
    supplied_headers = []
    response = [_reader()]
    reader_headers = [{}]
    reader_forbidden = [False]

    class FakeFetcher:
        def __init__(self, **kwargs):
            self.last_response_headers = {}
            self.last_status_code = 200
            supplied_headers.append(kwargs["headers"])

        def get(self, url, **_kwargs):
            calls.append(url)
            self.last_response_headers = reader_headers[0] if url == READER_URL else {}
            self.last_status_code = 200
            if url == READER_URL and reader_forbidden[0]:
                self.last_status_code = 403
                raise FetchError("HTTP 403")
            if "/api/reader/directory/detail" in url:
                return _directory(_item())
            if "/page/" in url:
                return _meta()
            return response[0]

        def polite_sleep(self):
            pass

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])

    response[0] = "<html>bdturing challenge</html>"
    with pytest.raises(AccessVerificationRequired):
        downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1, end=1)
    chapter_cache = tmp_path / "cache" / BOOK_ID / f"{ITEM_ID}.txt"
    assert not chapter_cache.exists()

    response[0] = _reader()
    reader_headers[0] = {"bdturing-verify": "challenge"}
    with pytest.raises(AccessVerificationRequired):
        downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1, end=1)
    assert not chapter_cache.exists()
    reader_headers[0] = {}
    reader_forbidden[0] = True
    before_forbidden = calls.count(READER_URL)
    with pytest.raises(AccessVerificationRequired):
        downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=3, end=1)
    assert calls.count(READER_URL) == before_forbidden + 1
    assert not chapter_cache.exists()
    reader_forbidden[0] = False
    response[0] = _reader(content="<p>" + "\uf000" * 30 + "</p>")
    with pytest.raises(DecodeFailed, match="DECODE_FAILED"):
        downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1, end=1)
    assert not chapter_cache.exists()
    response[0] = _reader()
    txt = downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0,
                                         retries=1, end=1, output_format="txt")
    assert txt.suffix == ".txt"
    assert "人在这里" in txt.read_text(encoding="utf-8")
    chapter_calls = calls.count(READER_URL)
    epub = downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0,
                                          retries=1, end=1, output_format="epub")
    assert epub.suffix == ".epub"
    with zipfile.ZipFile(epub) as archive:
        assert "人在这里" in archive.read("OEBPS/chapter1.xhtml").decode("utf-8")
    assert calls.count(READER_URL) == chapter_calls
    assert "\ue3e8" not in chapter_cache.read_text(encoding="utf-8")
    assert supplied_headers[0]["ismobile"] == "1"
    assert "Mobile" in supplied_headers[0]["User-Agent"]
    downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1,
                                   end=1, request_headers={"Accept": "custom/type"})
    assert supplied_headers[-1]["Accept"] == "custom/type"
    assert supplied_headers[-1]["ismobile"] == "1"


def test_same_count_directory_reorder_reuses_only_matching_item_ids(tmp_path, monkeypatch):
    item_ids = ["101", "202"]
    chapter_calls = []
    other_raw = "".join(chr(0xE3E8 + CHARSETS[0].index(char))
                        for char in "这里有人" + "人在这里") * 12

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.last_response_headers = {}
            self.last_status_code = 200

        def get(self, url, **_kwargs):
            if "/page/" in url:
                return _meta()
            if "/directory/detail" in url:
                return _directory(*(_item(item_id) for item_id in item_ids))
            item_id = url.rsplit("/", 1)[-1]
            chapter_calls.append(item_id)
            raw = RAW if item_id == "101" else other_raw
            return _reader(item_id=item_id, content=f"<p>{raw}</p>")

        def polite_sleep(self):
            pass

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])
    downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1, end=2)
    assert chapter_calls == ["101", "202"]
    cache = tmp_path / "cache" / BOOK_ID
    assert (cache / "101.txt").is_file() and (cache / "202.txt").is_file()
    item_ids.reverse()
    output = downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1, end=2)
    assert chapter_calls == ["101", "202"]
    text = output.read_text(encoding="utf-8")
    assert text.index("这里有人") < text.index("人在这里")


def test_resumed_short_pua_chapter_uses_persisted_mode(tmp_path, monkeypatch):
    chapter_calls = []
    short_raw = chr(0xE3E8 + CHARSETS[0].index("人")) * 5

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.last_response_headers = {}
            self.last_status_code = 200

        def get(self, url, **_kwargs):
            if "/page/" in url:
                return _meta()
            if "/directory/detail" in url:
                return _directory(_item("101"), _item("202"))
            item_id = url.rsplit("/", 1)[-1]
            chapter_calls.append(item_id)
            raw = RAW if item_id == "101" else short_raw
            return _reader(item_id=item_id, content=f"<p>{raw}</p>")

        def polite_sleep(self):
            pass

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])
    downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0, retries=1, end=1)
    state = json.loads((tmp_path / "cache" / BOOK_ID / "fanqie_decoder.json").read_text(encoding="utf-8"))
    assert state == {"book_id": BOOK_ID, "mode": 0}
    result = downloader_task.download_novel(BOOK_URL, tmp_path / "out", delay=0,
                                            retries=1, start=2, end=2)
    assert chapter_calls == ["101", "202"]
    assert "人人人人人" in result.read_text(encoding="utf-8")
    assert (tmp_path / "cache" / BOOK_ID / "202.txt").read_text(encoding="utf-8") == "人人人人人"


def test_merged_chapter_cache_key_tracks_all_item_ids():
    from sites.base import Chapter
    adapter = FanqieAdapter()
    chapter = Chapter("第1章", READER_URL, ["https://fanqienovel.com/reader/202"])
    first = adapter.chapter_cache_filename(chapter, 1)
    chapter.extra_urls.append("https://fanqienovel.com/reader/303")
    assert first != adapter.chapter_cache_filename(chapter, 1)
