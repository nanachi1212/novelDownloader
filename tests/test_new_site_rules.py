from sites import get_adapter


def test_zhys_cn_rule_keeps_the_cn_host_and_complete_catalog():
    adapter = get_adapter('https://cn.zhys.tw/book/9578130.html#full-catalog')
    assert adapter.rule_id == 'zhys_cn'
    assert adapter.catalog_url('https://cn.zhys.tw/read/9578130/82796836.html') == 'https://cn.zhys.tw/book/9578130.html'
    book = adapter.parse_catalog('''<h1>測試書</h1><a href="/author/1">作者</a>
        <a href="/read/9578130/9.html">最新</a><section id="full-catalog">
        <div class="catalog-wrap"><a href="/read/9578130/1.html">第1章</a>
        <template><a title="第2章" href="/read/9578130/2.html">第2章</a></template></div></section>''')
    assert len(book.chapters) == 2
    assert book.chapters[0].url == 'https://cn.zhys.tw/read/9578130/1.html'


def test_piaotian_rule_restores_reversed_paragraphs_and_same_chapter_pages():
    adapter = get_adapter('https://www.piaotianapp.com/ptinfo/1463532268.html')
    adapter.catalog_url('https://www.piaotianapp.com/ptchapter/1463532268/451931394144_2.html')
    html = '''<div id="content"><div class="m-tpage"><a href="/ptchapter/1463532268/451931394144_2.html">下一頁</a></div>
        <p>第一段正文。</p><p class="js-reversed-text">。文正段二第</p></div>'''
    assert adapter.parse_chapter(html) == '第一段正文。\n\n第二段正文。'
    first = 'https://www.piaotianapp.com/ptchapter/1463532268/451931394144.html'
    assert adapter.next_page_url(html, first) == first.replace('.html', '_2.html')
    assert adapter.next_page_url('<a href="/ptchapter/1463532268/451931403730.html">下一章</a>', first) is None
