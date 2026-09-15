from sites import get_adapter
from sites.book8 import Book8Adapter
from sites.czbooks import CzbooksAdapter
from sites.novel543 import Novel543Adapter
from sites.shuba69 import Shuba69
from sites.shuku52 import Shuku52Adapter
from sites.twkan import TwkanAdapter
from sites.xbanxia import XbanxiaAdapter
from sites.novels_com_tw import NovelsComTwAdapter
from sites.zhys import ZhysAdapter
from downloader_task import chapter_number_warning


def test_shuku52_catalog_normalization_and_parsing():
    adapter = Shuku52Adapter()
    assert adapter.catalog_url("https://www.52shuku.net/bl/21_b/bjZUj_3.html") == (
        "https://www.52shuku.net/bl/21_b/bjZUj.html"
    )
    html = """
    <h1>測試書名_作者甲【完結】</h1>
    <ul class="list clearfix">
      <li><a href="bjZUj_2.html">第1頁</a></li>
      <li><a href="bjZUj_3.html">第2頁</a></li>
    </ul>
    """
    book = adapter.parse_catalog(html)
    assert (book.title, book.author) == ("測試書名", "作者甲")
    assert [chapter.title for chapter in book.chapters] == ["第1頁", "第2頁"]
    assert book.chapters[0].url == "https://www.52shuku.net/bl/21_b/bjZUj_2.html"
    assert adapter.book_id("https://www.52shuku.net/bl/21_b/bjZUj_2.html") == (
        "52shuku-bl-21_b-bjZUj"
    )


def test_shuku52_chapter_extracts_only_article_content():
    adapter = Shuku52Adapter()
    html = """
    <article id="nr1">
      <p>第一段正文</p><script>廣告</script><a href="/next">下一頁</a>
      <p>第二段正文</p>
    </article>
    <div>站外雜訊</div>
    """
    assert adapter.parse_chapter(html) == "第一段正文\n\n第二段正文"


def test_xbanxia_repeated_recommendations_do_not_break_string_parent():
    adapter = XbanxiaAdapter()
    html = """
    <article>
      <p>第一段正文</p>
      <div>每日推薦：甲</div>
      <p>第二段正文</p>
      <div>推薦閱讀：乙</div>
      <p>第三段正文</p>
    </article>
    """
    assert adapter.parse_chapter(html) == "第一段正文\n\n第二段正文\n\n第三段正文"


def test_novel543_urls_meta_and_catalog():
    adapter = Novel543Adapter()
    assert adapter.catalog_url("https://look.thisiscm.com/0710607590/8086_2.html") == (
        "https://www.novel543.com/0710607590/dir"
    )
    assert adapter.meta_url("https://www.novel543.com/0710607590/") == (
        "https://www.novel543.com/0710607590/"
    )
    meta = """
    <meta property="og:novel:book_name" content="測試小說">
    <meta property="og:novel:author" content="作者乙">
    """
    assert adapter.parse_meta(meta) == ("測試小說", "作者乙")

    catalog = """
    <h1>測試小說 章節列表</h1><h2>作者 / 作者乙</h2>
    <ul><li><a href="/0710607590/8086_9.html">最新章</a></li></ul>
    <ul class="flex one all">
      <li><a href="/0710607590/8086_1.html">第1章</a></li>
      <li><a href="/0710607590/8086_2.html">第2章</a></li>
    </ul>
    """
    book = adapter.parse_catalog(catalog)
    assert (book.title, book.author) == ("測試小說", "作者乙")
    assert [chapter.title for chapter in book.chapters] == ["第1章", "第2章"]
    assert book.chapters[-1].url == "https://www.novel543.com/0710607590/8086_2.html"


def test_novel543_chapter_and_domain_registration():
    html = """
    <div id="chapterWarp"><div class="chapter-content"><div class="content py-5">
      <p>第一段正文</p><script>廣告</script><p>第二段正文</p>
    </div></div></div>
    """
    adapter = Novel543Adapter()
    assert adapter.parse_chapter(html) == "第一段正文\n\n第二段正文"
    assert isinstance(get_adapter("https://www.novel543.com/0710607590/"), Novel543Adapter)
    assert isinstance(
        get_adapter("https://look.thisiscm.com/0710607590/8086_1.html"), Novel543Adapter
    )


def test_chapter_number_warning_reports_site_numbering_drift():
    chapters = [
        type("Chapter", (), {"title": "第1章 一"})(),
        type("Chapter", (), {"title": "第2章 二"})(),
        type("Chapter", (), {"title": "第2章 另一個二"})(),
        type("Chapter", (), {"title": "第4章 四"})(),
    ]
    warning = chapter_number_warning(chapters)
    assert "偵測到重複章號 1 個" in warning
    assert "缺少章號 3" in warning


def test_czbooks_current_layout_catalog_and_chapter():
    adapter = CzbooksAdapter()
    catalog = """
    <title>【免費小說】《諸界末日在線》2026最新連載、線上看 | 小說狂人</title>
    <div class="info">《諸界末日在線》 作者: 煙火成城</div>
    <ul id="chapter-list" class="chapter-list">
      <li><a href="/n/u70o3/uifj2">第1章 死人坑</a></li>
      <li><a href="/n/u70o3/uifjk">第2章 末日</a></li>
    </ul>
    """
    book = adapter.parse_catalog(catalog)
    assert (book.title, book.author) == ("諸界末日在線", "煙火成城")
    assert [chapter.title for chapter in book.chapters] == ["第1章 死人坑", "第2章 末日"]

    chapter = """
    <div class="chapter-sidebar"></div>
    <div class="chapter-detail">
      <h1 class="name">《諸界末日在線》第1章 死人坑</h1>
      <div class="content">雨下了一天一夜。<br>死人坑中，伸出了一隻手。</div>
    </div>
    """
    assert adapter.parse_chapter(chapter, "第1章 死人坑") == "雨下了一天一夜。死人坑中，伸出了一隻手。"


def test_czbooks_chapter_removes_repeated_ad_parent_once():
    adapter = CzbooksAdapter()
    chapter = """
    <div class="chapter-detail">
      <div class="content">
        <p>第一段正文</p>
        <div>推薦閱讀<br>廣告</div>
        <p>第二段正文</p>
      </div>
    </div>
    """
    assert adapter.parse_chapter(chapter) == "第一段正文\n\n第二段正文"


def test_book8_catalog_and_chapter_parsing():
    adapter = Book8Adapter()
    catalog = """
    <title>旺福小娘子, 旺福小娘子小說全文在線閱讀 - 無限小說</title>
    <meta property="og:title" content="旺福小娘子">
    <meta name="keywords" content="旺福小娘子最新章節,燕小陌作品">
    <a href="https://finance.binaccount.com/read/131302/?2374669">開始看第1集</a>
    <a href="https://finance.binaccount.com/read/131302/?2374669">第一章 五福</a>
    <a href="//finance.binaccount.com/read/131302/?2374673">第二章 停下你的豬手</a>
    """
    book = adapter.parse_catalog(catalog)
    assert (book.title, book.author) == ("旺福小娘子", "燕小陌")
    assert [chapter.title for chapter in book.chapters] == ["第一章 五福", "第二章 停下你的豬手"]
    assert book.chapters[0].url == "https://finance.binaccount.com/read/131302/?2374669"
    assert adapter.catalog_url("https://finance.binaccount.com/read/131302/?2374669") == (
        "https://www.8book.com/novelbooks/131302/"
    )
    assert isinstance(get_adapter("https://www.8book.com/novelbooks/131302/"), Book8Adapter)
    assert isinstance(get_adapter("https://finance.binaccount.com/read/131302/?2374669"), Book8Adapter)

    reader = """
    <meta name="itemid" content="131302">
    <meta name="catid" content="4">
    <div id="text"></div>
    <script>
      var nk____=100;var pe682qj=7;var ea8v8420=3;var t8pj3yk__=5;
      var hni2v2c31="2374669,432587752999513611237679581452598879933973788962629829875391639941164467726488153983367819574344384789736321449624875678".split(',');
    </script>
    """
    assert adapter.chapter_source_url(reader, "https://finance.binaccount.com/read/131302/?2374669") == (
        "https://8book.com/txt/4/131302/237466952999.html"
    )

    chapter = """
    <div id="text">
      <p>第一段正文</p>
      <p>第二段正文</p>
      <p>章節列表</p>
    </div>
    """
    assert adapter.parse_chapter(chapter, "第一章 五福") == "第一段正文\n\n第二段正文"


def test_shuba69_tw_urls_are_supported():
    adapter = Shuba69()
    assert adapter.catalog_url("https://69shuba.tw/indexlist/67008/") == (
        "https://69shuba.tw/indexlist/67008/"
    )
    assert adapter.meta_url("https://69shuba.tw/indexlist/67008/") == (
        "https://69shuba.tw/book/67008/"
    )
    assert adapter.meta_url("https://69shuba.tw/book/345685/") == (
        "https://69shuba.tw/book/345685/"
    )
    assert adapter.book_id("https://69shuba.tw/indexlist/67008/") == "69shuba-67008"
    assert isinstance(get_adapter("https://69shuba.tw/indexlist/67008/"), Shuba69)


def test_shuba69_info_page_is_normalized_to_catalog():
    adapter = Shuba69()
    assert adapter.catalog_url("https://www.69shuba.com/book/35215.htm") == (
        "https://www.69shuba.com/book/35215/"
    )
    assert adapter.catalog_url("https://www.69shuba.com/book/35215.htm?from=home") == (
        "https://www.69shuba.com/book/35215/"
    )
    assert adapter.max_chapter_workers == 1


def test_twkan_urls_meta_and_full_ajax_catalog():
    adapter = TwkanAdapter()
    index_url = "https://twkan.com/book/80493/index.html"
    assert adapter.catalog_url(index_url) == (
        "https://twkan.com/ajax_novels/chapterlist/80493.html"
    )
    assert adapter.meta_url(index_url) == "https://twkan.com/book/80493.html"
    assert adapter.book_id(index_url) == "twkan-80493"

    meta = """
    <div class="booknav2">
      <h1><a href="/book/80493.html">從送子鯉魚到天庭仙官</a></h1>
      <p>作者：<a href="/author/錦繡灰.html">錦繡灰</a></p>
    </div>
    """
    assert adapter.parse_meta(meta) == ("從送子鯉魚到天庭仙官", "錦繡灰")

    catalog = """
    <ul>
      <li data-num="2"><a href="/txt/80493/48236622">第二章 我有一座控制台</a></li>
      <li data-num="1"><a href="https://twkan.com/txt/80493/48236621">第一章 我是送子鯉魚</a></li>
    </ul>
    """
    book = adapter.parse_catalog(catalog)
    assert [chapter.title for chapter in book.chapters] == [
        "第一章 我是送子鯉魚", "第二章 我有一座控制台"
    ]
    assert book.chapters[0].url == "https://twkan.com/txt/80493/48236621"
    assert isinstance(get_adapter(index_url), TwkanAdapter)


def test_twkan_chapter_removes_structural_and_text_ads():
    adapter = TwkanAdapter()
    chapter = """
    <div id="txtcontent0">
      第一段正文<br><br>
      【記住本站域名 讀台灣好書上台灣小說網，ᴛᴡᴋᴀɴ.ᴄᴏᴍ超省心】<br><br>
      <div class="txtad"><script>loadAdv(10, 0)</script></div>
      第二段正文
    </div>
    """
    assert adapter.parse_chapter(chapter) == "第一段正文\n\n第二段正文"


def test_novels_com_tw_urls_catalog_and_registration():
    adapter = NovelsComTwAdapter()
    book_id = "no" + "a" * 64
    book_url = f"https://www.novels.com.tw/novels/{book_id}/"
    chapter_url = f"{book_url}116028087.html"
    assert adapter.catalog_url(chapter_url) == book_url
    assert adapter.book_id(book_url) == f"novels-com-tw-{book_id}"

    catalog = f"""
    <meta property="og:novel:book_name" content="測試小說">
    <meta property="og:novel:author" content="作者甲">
    <div id="catalog"><div class="chapters"><ul>
      <li><a href="/novels/{book_id}/100.html">第一章</a></li>
      <li><a href="/novels/{book_id}/200.html">第二章</a></li>
    </ul></div></div>
    """
    book = adapter.parse_catalog(catalog)
    assert (book.title, book.author) == ("測試小說", "作者甲")
    assert [chapter.title for chapter in book.chapters] == ["第一章", "第二章"]
    assert book.chapters[0].url == f"https://www.novels.com.tw/novels/{book_id}/100.html"
    assert isinstance(get_adapter(book_url), NovelsComTwAdapter)


def test_novels_com_tw_decrypts_chapter_and_follows_only_same_chapter_pages():
    adapter = NovelsComTwAdapter()
    encrypted = (
        "HesVRxgAexWqrC1LAUgujngJwQ3qB3VkntWEcgH5BdFMcwSfXOhbysfBLTHlV+"
        "DmkWP34Wzd9gNg/T95dLdmqA=="
    )
    html = f'<div id="chapter-content"><script>window.encryptedContent = "{encrypted}";</script></div>'
    assert adapter.parse_chapter(html) == "第一段正文\n\n第二段正文"

    first_url = "https://www.novels.com.tw/novels/no" + "a" * 64 + "/100.html"
    page2 = '<a id="next_url" data-real-href="100_2.html">下一頁</a>'
    assert adapter.next_page_url(page2, first_url) == first_url.replace("100.html", "100_2.html")
    next_chapter = '<a id="next_url" href="200.html">下一章</a>'
    assert adapter.next_page_url(next_chapter, first_url) is None


def test_zhys_urls_catalog_chapter_and_registration():
    adapter = ZhysAdapter()
    assert adapter.catalog_url("https://twp.zhys.tw/read/224981/50896868.html") == (
        "https://twp.zhys.tw/book/224981.html"
    )
    assert adapter.book_id("https://twp.zhys.tw/book/224981.html") == "zhys-224981"

    catalog = """
    <h1>測試小說</h1><a href="/author/123">作者乙</a>
    <section id="full-catalog">
      <div class="catalog-wrap"><a href="/read/224981/1.html">第一章</a></div>
      <div class="catalog-wrap"><a href="/read/224981/2.html" title="第二章"></a></div>
    </section>
    """
    book = adapter.parse_catalog(catalog)
    assert (book.title, book.author) == ("測試小說", "作者乙")
    assert [chapter.title for chapter in book.chapters] == ["第一章", "第二章"]
    assert book.chapters[0].url == "https://twp.zhys.tw/read/224981/1.html"

    chapter = """
    <div id="article-content">
      <p>第一段正文</p><script>廣告</script><p>第二段正文</p>
    </div>
    """
    assert adapter.parse_chapter(chapter) == "第一段正文\n\n第二段正文"
    assert isinstance(get_adapter("https://twp.zhys.tw/book/224981.html"), ZhysAdapter)


def test_zhys_category_url_is_rejected_as_not_a_single_book():
    adapter = ZhysAdapter()
    try:
        adapter.catalog_url("https://twp.zhys.tw/chuanyuejiakong")
    except ValueError as exc:
        assert "分類頁不是單本小說網址" in str(exc)
    else:
        raise AssertionError("zhys 分類頁應被拒絕")

