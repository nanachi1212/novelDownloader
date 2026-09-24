from sites.generic import GenericAdapter


def test_biquge_dl_dd_template_skips_latest_chapters_box():
    """筆趣閣類 dl/dt/dd:「最新章節」小清單重複連到後面完整清單的同一章,
    命中內建模板後,輸出必須是正文標題之後的完整清單,順序正確、不重複也不丟章。
    """
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/book/1.html")
    html = """
    <div id="list">
    <dl>
      <dt>最新章節</dt>
      <dd><a href="/book/1/5.html">第五章</a></dd>
      <dd><a href="/book/1/4.html">第四章</a></dd>
      <dt>正文</dt>
      <dd><a href="/book/1/1.html">第一章</a></dd>
      <dd><a href="/book/1/2.html">第二章</a></dd>
      <dd><a href="/book/1/3.html">第三章</a></dd>
      <dd><a href="/book/1/4.html">第四章</a></dd>
      <dd><a href="/book/1/5.html">第五章</a></dd>
    </dl>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert [c.title for c in book.chapters] == ["第一章", "第二章", "第三章", "第四章", "第五章"]
    assert book.chapters[0].url == "https://example.test/book/1/1.html"
    assert adapter.template_name == "#list dl"


def test_container_template_matches_by_id_without_dl():
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/n/1")
    html = """
    <div id="chapterlist">
      <a href="/n/1/1.html">第一章</a>
      <a href="/n/1/2.html">第二章</a>
      <a href="/n/1/3.html">第三章</a>
      <a href="/n/1/4.html">第四章</a>
      <a href="/n/1/5.html">第五章</a>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert len(book.chapters) == 5
    assert adapter.template_name == "#chapterlist"


def test_template_falls_back_to_heuristic_when_too_few_links():
    """命中的模板容器裡不足 5 個有效連結時視為未命中,交給啟發式規則,
    不能因為誤判模板而漏章。
    """
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/n/1")
    html = """
    <div id="catalog"><a href="/n/1/1.html">第一章</a></div>
    <div class="all-chapters">
      <a href="/n/1/1.html">第一章</a>
      <a href="/n/1/2.html">第二章</a>
      <a href="/n/1/3.html">第三章</a>
      <a href="/n/1/4.html">第四章</a>
      <a href="/n/1/5.html">第五章</a>
      <a href="/n/1/6.html">第六章</a>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert adapter.template_name is None
    assert len(book.chapters) == 6


def test_heuristic_skips_latest_chapters_mini_list_without_dl():
    """啟發式規則(無 dl/dt)一樣要排除「最新章節」小清單造成的順序污染。"""
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/n/1")
    html = """
    <div class="wrap">
      <div class="hd">最新章節</div>
      <a href="/n/1/5.html">第五章</a>
      <a href="/n/1/4.html">第四章</a>
      <div class="hd">正文</div>
      <a href="/n/1/1.html">第一章</a>
      <a href="/n/1/2.html">第二章</a>
      <a href="/n/1/3.html">第三章</a>
      <a href="/n/1/4.html">第四章</a>
      <a href="/n/1/5.html">第五章</a>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert [c.title for c in book.chapters] == ["第一章", "第二章", "第三章", "第四章", "第五章"]


class DeclarativeAdapter(GenericAdapter):
    domains = ["declarative.test"]
    catalog_selector = ".mychapters a"
    content_selector = "#body"
    remove_selectors = (".ad",)


def test_declarative_catalog_selector_is_trusted_even_with_few_chapters():
    adapter = DeclarativeAdapter()
    adapter.catalog_url("https://declarative.test/book")
    html = '<div class="mychapters"><a href="/c/1.html">第一章</a><a href="/c/2.html">第二章</a></div>'
    book = adapter.parse_catalog(html)
    assert [c.title for c in book.chapters] == ["第一章", "第二章"]
    assert adapter.template_name == "declarative"


def test_declarative_content_selector_and_remove_selectors():
    adapter = DeclarativeAdapter()
    html = '<div id="body"><p>正文內容</p><div class="ad">廣告</div><a href="/next">下一頁</a></div>'
    assert adapter.parse_chapter(html) == "正文內容"


def test_full_catalog_url_finds_same_origin_expand_link():
    adapter = GenericAdapter()
    html = '<a href="/book/1/full.html">查看全部章節</a><a href="/login">登入看更多</a>'
    assert adapter.full_catalog_url(html, "https://example.test/book/1.html") == (
        "https://example.test/book/1/full.html"
    )


def test_full_catalog_url_ignores_javascript_and_cross_origin_links():
    adapter = GenericAdapter()
    html = '<a href="javascript:void(0)">展開全部</a><a href="https://other.test/x">完整目錄</a>'
    assert adapter.full_catalog_url(html, "https://example.test/book/1.html") is None


def test_catalog_page_urls_next_page_link():
    adapter = GenericAdapter()
    html = '<div id="pager"><a href="/book/1/index_2.html">下一頁</a></div>'
    assert adapter.catalog_page_urls(html, "https://example.test/book/1/index_1.html") == [
        "https://example.test/book/1/index_2.html"
    ]


def test_catalog_page_urls_select_pagination_excludes_current_page():
    adapter = GenericAdapter()
    html = ('<select><option value="/list_1.html">1</option>'
            '<option value="/list_2.html">2</option>'
            '<option value="/list_3.html">3</option></select>')
    assert adapter.catalog_page_urls(html, "https://example.test/list_1.html") == [
        "https://example.test/list_2.html",
        "https://example.test/list_3.html",
    ]


def test_parse_chapter_strips_hidden_and_fullwidth_watermark_lines():
    adapter = GenericAdapter()
    html = """
    <article>
      <p>第一段正文</p>
      <p style="display:none">隱藏的干擾文字</p>
      <p hidden>另一段隱藏文字</p>
      <p>ｗｗｗ．ｅｘａｍｐｌｅ．ｃｏｍ</p>
      <p>第二段正文</p>
    </article>
    """
    assert adapter.parse_chapter(html) == "第一段正文\n\n第二段正文"


def test_parse_chapter_raises_on_private_use_font_obfuscation():
    adapter = GenericAdapter()
    garbled = "" * 20
    html = f'<article><p>{garbled}</p></article>'
    try:
        adapter.parse_chapter(html)
    except ValueError as exc:
        assert "自訂字型" in str(exc)
    else:
        raise AssertionError("private-use-area obfuscation should raise ValueError")


def test_parse_chapter_normal_unicode_is_not_flagged():
    adapter = GenericAdapter()
    html = "<article><p>正常的中文內容，沒有任何私用區字元。</p></article>"
    assert adapter.parse_chapter(html) == "正常的中文內容，沒有任何私用區字元。"
