import json
import threading

import pytest

from sites import get_adapter
from sites.fanqie import (
    AccessVerificationRequired, FanqieAdapter, FanqieChapter, FanqieError,
    chapter_cache_path, parse_book_id, parse_directory_response, save_raw_chapters,
)


def _item(item_id, title, volume, **status):
    return {"itemId": str(item_id), "title": title, "volume_name": volume,
            "realChapterOrder": str(item_id), **status}


def _directory(*groups):
    ids = [entry["itemId"] for group in groups for entry in group]
    return json.dumps({"code": 0, "message": "success", "data": {
        "allItemIds": ids, "volumeNameList": [group[0]["volume_name"] for group in groups],
        "chapterListWithVolume": groups,
    }}, ensure_ascii=False)


def test_parse_book_id_and_full_directory_order_statuses():
    assert parse_book_id("https://fanqienovel.com/page/1234567890") == "1234567890"
    assert parse_book_id("https://fanqienovel.com/page/1234567890?from=share") == "1234567890"
    one = _item("101", "第一章", "第一卷", needPay=0, isPaidPublication=False,
                isPaidStory=False, isChapterLock=False)
    two = _item("202", "第二章", "第二卷", needPay=0, isPaidPublication=False,
                isPaidStory=False, isChapterLock=True)
    chapters = parse_directory_response(_directory([one], [two]))
    assert [(ch.order, ch.volume_name, ch.title, ch.item_id) for ch in chapters] == [
        (1, "第一卷", "第一章", "101"), (2, "第二卷", "第二章", "202")]
    assert chapters[0].access == "public_candidate"
    assert chapters[1].access == "unknown_locked"
    assert chapters[1].statuses["realChapterOrder"] == "202"


def test_directory_rejects_duplicate_ids_malformed_empty_and_count_order_mismatch():
    duplicate = _item("101", "一", "卷", needPay=0, isPaidPublication=False,
                      isPaidStory=False, isChapterLock=False)
    with pytest.raises(FanqieError):
        parse_directory_response(_directory([duplicate], [dict(duplicate)]))
    with pytest.raises(FanqieError):
        parse_directory_response(json.dumps({"code": 0, "data": {
            "chapterListWithVolume": [], "allItemIds": []}}))
    with pytest.raises(FanqieError):
        parse_directory_response("not json")
    mismatch = json.loads(_directory([duplicate]))
    mismatch["data"]["allItemIds"] = ["elsewhere"]
    with pytest.raises(FanqieError):
        parse_directory_response(json.dumps(mismatch))


def test_directory_surfaces_api_error_response():
    with pytest.raises(FanqieError):
        parse_directory_response(json.dumps({"code": 429, "message": "too many"}))


def test_directory_api_verification_is_not_misreported_as_empty_or_bypassed():
    with pytest.raises(AccessVerificationRequired):
        FanqieAdapter().fetch_directory(
            "https://fanqienovel.com/page/123456789",
            _FakeFetcher("", {"bdturing-verify": "challenge"}),
        )


def test_directory_requires_all_access_flags_to_call_chapter_public():
    partial = _item("101", "第一章", "卷", needPay=0, isChapterLock=False)
    assert parse_directory_response(_directory([partial]))[0].access == "unknown"
    paid = _item("102", "付費章", "卷", needPay=1, isPaidPublication=False,
                 isPaidStory=False, isChapterLock=False)
    assert parse_directory_response(_directory([paid]))[0].access == "restricted"


class _FakeFetcher:
    def __init__(self, raw, headers=None, status=None):
        self.raw = raw
        self.last_response_headers = headers or {}
        self.last_status_code = status

    def get(self, *_args, **_kwargs):
        return self.raw


def _public_chapter(item_id="101"):
    return FanqieChapter(item_id, "第一章", "第一卷", 1, {
        "needPay": 0, "isPaidPublication": False, "isPaidStory": False,
        "isChapterLock": False,
    })


def test_fetch_raw_chapter_preserves_source_response_and_refuses_locked_or_challenged():
    raw = json.dumps({"code": 0, "data": {"chapterData": {
        "itemId": "101", "content": "原字元㐂\n\n第二段", "font": "source-font"
    }}}, ensure_ascii=False)
    adapter = FanqieAdapter()
    envelope = json.loads(adapter.fetch_raw_chapter("999", _public_chapter(), _FakeFetcher(raw)))
    assert envelope["book_id"] == "999"
    assert envelope["item_id"] == "101"
    assert envelope["source_response_text"] == raw
    assert envelope["source_headers"] == {}
    font_raw = adapter.fetch_raw_chapter("999", _public_chapter(),
                                         _FakeFetcher(raw, {"X-TT-ZHAL": "font-map-raw"}))
    assert json.loads(font_raw)["source_headers"]["x-tt-zhal"] == "font-map-raw"
    locked = FanqieChapter("102", "鎖定章", "第一卷", 2, {
        "needPay": 0, "isPaidPublication": False, "isPaidStory": False,
        "isChapterLock": True,
    })
    with pytest.raises(AccessVerificationRequired):
        adapter.fetch_raw_chapter("999", locked, _FakeFetcher(raw))
    with pytest.raises(AccessVerificationRequired):
        adapter.fetch_raw_chapter("999", _public_chapter(),
                                  _FakeFetcher("", {"Bdturing-Verify": '{"type":"verify"}'}))
    with pytest.raises(AccessVerificationRequired):
        adapter.fetch_raw_chapter("999", _public_chapter(), _FakeFetcher("", status=403))
    with pytest.raises(FanqieError):
        adapter.fetch_raw_chapter("999", _public_chapter(), _FakeFetcher(""))
    with pytest.raises(FanqieError):
        adapter.fetch_raw_chapter("999", _public_chapter(), _FakeFetcher('{"code":0,"data":{}}'))
    wrong_item = json.dumps({"code": 0, "data": {"chapterData": {
        "itemId": "other", "content": "text"
    }}})
    with pytest.raises(FanqieError):
        adapter.fetch_raw_chapter("999", _public_chapter(), _FakeFetcher(wrong_item))
    response_locked = json.dumps({"code": 0, "data": {"chapterData": {
        "itemId": "101", "content": "content", "isChapterLock": True
    }}})
    with pytest.raises(AccessVerificationRequired):
        adapter.fetch_raw_chapter("999", _public_chapter(), _FakeFetcher(response_locked))


def test_raw_preview_store_isolated_and_resumes_by_item_id(tmp_path):
    chapters = [_public_chapter("101"), _public_chapter("202")]
    raw_values = {
        item_id: json.dumps({"book_id": "999", "item_id": item_id,
            "source_response_text": json.dumps({"code": 0, "data": {"chapterData": {
                "itemId": item_id, "content": content
            }}}, ensure_ascii=False), "source_headers": {}}, ensure_ascii=False)
        for item_id, content in (("101", "第一章㐂"), ("202", "第二章"))
    }
    calls = []
    cancel = threading.Event()

    class FakeAdapter:
        def fetch_raw_chapter(self, book_id, chapter, fetcher):
            calls.append(chapter.item_id)
            return raw_values[chapter.item_id]

    def interrupt_before_second(chapter):
        if chapter.item_id == "202":
            cancel.set()

    preview_root = tmp_path / "app-data" / "preview" / "fanqie"
    normal_cache = tmp_path / "app-data" / "cache"
    assert chapter_cache_path(preview_root, "999", "101").is_relative_to(preview_root)
    assert not chapter_cache_path(preview_root, "999", "101").is_relative_to(normal_cache)
    first = save_raw_chapters(FakeAdapter(), "999", chapters, preview_root,
                              fetcher=object(), cancel_event=cancel,
                              progress=interrupt_before_second)
    assert [entry[0] for entry in first] == ["101"]
    assert chapter_cache_path(preview_root, "999", "101").read_text(encoding="utf-8") == raw_values["101"]
    assert not chapter_cache_path(preview_root, "999", "202").exists()

    cancel.clear()
    second = save_raw_chapters(FakeAdapter(), "999", chapters, preview_root,
                               fetcher=object(), cancel_event=cancel)
    assert [entry[0] for entry in second] == ["101", "202"]
    assert second[0][2] == "cached"
    assert calls == ["101", "202"]
    assert chapter_cache_path(preview_root, "999", "202").read_text(encoding="utf-8") == raw_values["202"]


def test_invalid_existing_preview_cache_is_not_overwritten(tmp_path):
    chapter = _public_chapter()
    preview_root = tmp_path / "preview" / "fanqie"
    path = chapter_cache_path(preview_root, "999", chapter.item_id)
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")

    class ShouldNotFetch:
        def fetch_raw_chapter(self, *_args):
            raise AssertionError("existing invalid file should not be silently replaced")

    with pytest.raises(FanqieError):
        save_raw_chapters(ShouldNotFetch(), "999", [chapter], preview_root, fetcher=object())
    assert path.read_text(encoding="utf-8") == "{}"


def test_preview_adapter_cannot_export_unrestored_text_and_is_registered():
    adapter = get_adapter("https://fanqienovel.com/page/123456")
    assert isinstance(adapter, FanqieAdapter)
    with pytest.raises(FanqieError):
        adapter.parse_catalog("<html></html>")
    with pytest.raises(FanqieError):
        adapter.parse_chapter("mapped text")
