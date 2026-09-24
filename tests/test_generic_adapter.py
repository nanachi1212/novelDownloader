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


def test_multi_volume_dl_catalog_keeps_all_volumes_not_just_the_last():
    """多卷小說常見:多個 <dt> 各自是卷名(不是「最新章節/正文」這種邊界字樣),
    沒有邊界就不該只挑最後一個 dt 之後的內容,不然會把前面幾卷整個丟掉。
    """
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/book/1.html")
    html = """
    <div id="list">
    <dl>
      <dt>第一卷</dt>
      <dd><a href="/book/1/1.html">第一章</a></dd>
      <dd><a href="/book/1/2.html">第二章</a></dd>
      <dt>第二卷</dt>
      <dd><a href="/book/1/3.html">第三章</a></dd>
      <dd><a href="/book/1/4.html">第四章</a></dd>
      <dd><a href="/book/1/5.html">第五章</a></dd>
      <dd><a href="/book/1/6.html">第六章</a></dd>
      <dd><a href="/book/1/7.html">第七章</a></dd>
    </dl>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert [c.title for c in book.chapters] == [
        "第一章", "第二章", "第三章", "第四章", "第五章", "第六章", "第七章"]


def test_biquge_dl_dd_boundary_still_keeps_chapters_after_a_later_volume_heading():
    """「最新章節」之後的完整清單若又用多個 dt 分卷,不能在遇到下一個 dt
    (例如「第二卷」)時就提早停止收集。
    """
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/book/2.html")
    html = """
    <div id="list">
    <dl>
      <dt>最新章節</dt>
      <dd><a href="/book/2/7.html">第七章</a></dd>
      <dt>正文</dt>
      <dd><a href="/book/2/1.html">第一章</a></dd>
      <dd><a href="/book/2/2.html">第二章</a></dd>
      <dt>第二卷</dt>
      <dd><a href="/book/2/3.html">第三章</a></dd>
      <dd><a href="/book/2/4.html">第四章</a></dd>
      <dd><a href="/book/2/5.html">第五章</a></dd>
      <dd><a href="/book/2/6.html">第六章</a></dd>
      <dd><a href="/book/2/7.html">第七章</a></dd>
    </dl>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert [c.title for c in book.chapters] == [
        "第一章", "第二章", "第三章", "第四章", "第五章", "第六章", "第七章"]


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


def test_container_template_excludes_pager_link_from_chapters():
    """章節清單容器裡混了一個「下一頁」翻頁連結,不能被當成一個章節
    (Codex review 抓到的 bug:整個容器內的 <a> 一律被當成 Chapter)。
    """
    adapter = GenericAdapter()
    adapter.catalog_url("https://example.test/n/1")
    html = """
    <div id="chapterlist">
      <a href="/n/1/1.html">第一章</a>
      <a href="/n/1/2.html">第二章</a>
      <a href="/n/1/3.html">第三章</a>
      <a href="/n/1/4.html">第四章</a>
      <a href="/n/1/5.html">第五章</a>
      <a href="/n/1/index_2.html">下一頁</a>
    </div>
    """
    book = adapter.parse_catalog(html)
    assert len(book.chapters) == 5
    assert "下一頁" not in [c.title for c in book.chapters]


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


def test_full_catalog_url_prefers_the_current_books_expand_link_over_recommendations():
    adapter = GenericAdapter()
    html = '''
    <div class="recommend"><a href="/book/456/full.html">全部章節</a></div>
    <div id="catalog"><a href="/book/123/full.html">全部章節</a></div>
    '''
    assert adapter.full_catalog_url(html, "https://example.test/book/123.html") == (
        "https://example.test/book/123/full.html"
    )


def test_full_catalog_url_rejects_a_lone_recommendation_for_another_book():
    adapter = GenericAdapter()
    html = '<a href="/book/456/full.html">全部章節</a>'
    assert adapter.full_catalog_url(html, "https://example.test/book/123.html") is None


def test_full_catalog_url_matches_book_id_query_parameter():
    adapter = GenericAdapter()
    html = '''
    <a href="/reader?book_id=456&all=1">全部章節</a>
    <a href="/reader?book_id=123&all=1">全部章節</a>
    '''
    assert adapter.full_catalog_url(html, "https://example.test/reader?book_id=123") == (
        "https://example.test/reader?book_id=123&all=1"
    )


def test_full_catalog_url_matches_bid_identifier_across_catalog_routes():
    adapter = GenericAdapter()
    html = '''
    <a href="/chapters.php?bid=456">全部章節</a>
    <a href="/chapters.php?bid=123">全部章節</a>
    '''
    assert adapter.full_catalog_url(html, "https://example.test/book.php?bid=123&sessionid=abc") == (
        "https://example.test/chapters.php?bid=123"
    )


def test_catalog_page_urls_next_page_link():
    adapter = GenericAdapter()
    html = '<div id="pager"><a href="/book/1/index_2.html">下一頁</a></div>'
    assert adapter.catalog_page_urls(html, "https://example.test/book/1/index_1.html") == [
        "https://example.test/book/1/index_2.html"
    ]


def test_catalog_page_urls_accepts_unmarked_next_page_only_in_same_catalog_family():
    adapter = GenericAdapter()
    html = '''
    <div class="recommendations"><a href="/recommend/index_2.html">下一頁</a></div>
    <a href="/book/1/index_2.html">下一頁</a>
    '''
    assert adapter.catalog_page_urls(html, "https://example.test/book/1/index_1.html") == [
        "https://example.test/book/1/index_2.html"
    ]


def test_catalog_page_urls_treats_missing_query_page_as_page_one():
    adapter = GenericAdapter()
    html = '<a href="?page=2">下一頁</a>'
    assert adapter.catalog_page_urls(html, "https://example.test/catalog") == [
        "https://example.test/catalog?page=2"
    ]


def test_catalog_page_urls_accepts_unnumbered_query_page_one_from_later_page():
    adapter = GenericAdapter()
    html = '<a href="/catalog">下一頁</a>'
    assert adapter.catalog_page_urls(html, "https://example.test/catalog?page=2") == [
        "https://example.test/catalog"
    ]


def test_catalog_page_urls_requires_matching_non_page_query_parameters():
    adapter = GenericAdapter()
    html = '<a href="?book=other&page=2">下一頁</a>'
    assert adapter.catalog_page_urls(html, "https://example.test/catalog?book=123") == []


def test_catalog_page_urls_finds_next_link_outside_the_matched_template_container():
    """分頁控制項通常在章節清單容器外面(清單下方的頁碼列),不能只在
    命中模板的 _catalog_container 裡面找「下一頁」,不然常常找不到。
    """
    adapter = GenericAdapter()
    html = """
    <div id="chapterlist">
      <a href="/n/1/1.html">第一章</a>
      <a href="/n/1/2.html">第二章</a>
      <a href="/n/1/3.html">第三章</a>
      <a href="/n/1/4.html">第四章</a>
      <a href="/n/1/5.html">第五章</a>
    </div>
    <div class="pager"><a href="/n/1/index_2.html">下一頁</a></div>
    """
    adapter.catalog_url("https://example.test/n/1")
    adapter.parse_catalog(html)  # 命中 #chapterlist 模板,_catalog_container 只涵蓋章節清單
    assert adapter.catalog_page_urls(html, "https://example.test/n/1") == [
        "https://example.test/n/1/index_2.html"
    ]


def test_catalog_page_urls_ignores_bare_more_link_outside_pagination_marker():
    """整頁搜尋「更多」這種泛用詞時,不能誤中不相干的「更多推薦」連結
    (Codex review 抓到的 bug),只有在分頁標記元素內才信任。
    """
    adapter = GenericAdapter()
    html = '<a href="/promo/more-recommend.html">更多推薦</a>'
    assert adapter.catalog_page_urls(html, "https://example.test/n/1") == []


def test_catalog_page_urls_accepts_bare_more_link_inside_pagination_marker():
    adapter = GenericAdapter()
    html = '<div class="pagination"><a href="/n/1/index_2.html">更多</a></div>'
    assert adapter.catalog_page_urls(html, "https://example.test/n/1") == [
        "https://example.test/n/1/index_2.html"
    ]


def test_pagination_marker_rejects_bare_page_layout_class():
    """<body class="page"> 這種版面標記不是分頁容器,不能讓底下所有元素都被
    當成分頁控制項(Codex review 抓到的 bug:bare "page" 子字串比對太寬)。
    """
    adapter = GenericAdapter()
    html = (
        '<body class="page">'
        '<select id="fontsize"><option value="16">16px</option><option value="18">18px</option></select>'
        '<a href="/promo/more.html">更多</a>'
        '</body>'
    )
    assert adapter.catalog_page_urls(html, "https://example.test/n/1") == []


def test_catalog_page_urls_accepts_numeric_links_in_page_class_container():
    adapter = GenericAdapter()
    html = '''
    <div class="page">
      <span>1</span>
      <a href="/list_2.html">2</a>
    </div>
    '''
    assert adapter.catalog_page_urls(html, "https://example.test/list_1.html") == [
        "https://example.test/list_2.html",
    ]


def test_catalog_page_urls_accepts_select_options_in_page_class_container():
    adapter = GenericAdapter()
    html = '''
    <div class="page">
      <select>
        <option value="/catalog?page=1">1</option>
        <option value="/catalog?page=2">2</option>
        <option value="/catalog?page=3">3</option>
      </select>
    </div>
    '''
    assert adapter.catalog_page_urls(html, "https://example.test/catalog?page=1") == [
        "https://example.test/catalog?page=2",
        "https://example.test/catalog?page=3",
    ]


def test_catalog_page_urls_rejects_body_page_class_even_with_numeric_links():
    adapter = GenericAdapter()
    html = '''
    <body class="page">
      <a href="/list_2.html">2</a><a href="/list_3.html">3</a>
    </body>
    '''
    assert adapter.catalog_page_urls(html, "https://example.test/list_1.html") == []


def test_catalog_page_urls_select_pagination_excludes_current_page():
    adapter = GenericAdapter()
    html = ('<div class="pagination"><select><option value="/list_1.html">1</option>'
            '<option value="/list_2.html">2</option>'
            '<option value="/list_3.html">3</option></select></div>')
    assert adapter.catalog_page_urls(html, "https://example.test/list_1.html") == [
        "https://example.test/list_2.html",
        "https://example.test/list_3.html",
    ]


def test_catalog_page_urls_ignores_unmarked_select_like_font_size_picker():
    """沒有分頁標記的 <select>(例如字體大小選單剛好是兩個數字)不能被誤判成分頁。"""
    adapter = GenericAdapter()
    html = '<select id="fontsize"><option value="16">16px</option><option value="18">18px</option></select>'
    assert adapter.catalog_page_urls(html, "https://example.test/book/1.html") == []


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


def test_parse_chapter_strips_hidden_container_with_nested_tags():
    """隱藏元素裡面還有子標籤時,decompose 父層後不能再去存取已經被清空的子孫標籤
    (Codex review 抓到的 bug:會炸 AttributeError,讓整章解析失敗)。
    """
    adapter = GenericAdapter()
    html = """
    <article>
      <p>第一段正文</p>
      <div style="display:none"><span>隱藏廣告<b>連結</b></span></div>
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
