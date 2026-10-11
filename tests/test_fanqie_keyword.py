import pytest

from sites import get_adapter
from sites.fanqie import FanqieAdapter, FanqieError
from fetcher import Fetcher

KEYWORD = "https://fanqienovel.com/keyword/7513287047927515177"
BOOK_ID = "7448581895861849112"


def test_keyword_rule_resolves_primary_book_and_reuses_result(monkeypatch):
    calls = []

    def fetch(self, url, **kwargs):
        calls.append(url)
        return f'''<div class="book-detail"><a class="book-title"
            href="/page/{BOOK_ID}">作品</a></div>
            <a class="book-title" href="/page/999">推薦</a>'''

    monkeypatch.setattr(Fetcher, "get", fetch)
    adapter = get_adapter(KEYWORD)
    assert isinstance(adapter, FanqieAdapter)
    assert adapter.rule_id == "fanqie_keyword"
    assert adapter.catalog_url(KEYWORD).endswith("bookId=" + BOOK_ID)
    assert adapter.meta_url(KEYWORD) == "https://fanqienovel.com/page/" + BOOK_ID
    assert adapter.book_id(KEYWORD) == BOOK_ID
    assert calls == [KEYWORD]
    assert adapter.supports_full_text_provider and adapter.require_complete_chapters
    assert type(get_adapter(adapter.meta_url(KEYWORD))) is FanqieAdapter


@pytest.mark.parametrize("links", [
    '<a class="book-title" href="https://example.com/page/123">外站</a>',
    '<a class="book-title" href="/page/123">一</a><a class="book-title" href="/page/456">二</a>',
    '',
])
def test_keyword_rule_rejects_missing_or_ambiguous_primary_book(monkeypatch, links):
    monkeypatch.setattr(Fetcher, "get", lambda *a, **k: '<div class="book-detail">' + links + '</div>')
    with pytest.raises(FanqieError, match="唯一作品"):
        get_adapter(KEYWORD).catalog_url(KEYWORD)
