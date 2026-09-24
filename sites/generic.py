"""通用備援 adapter:未註冊的網站用啟發式規則自動解析目錄與正文。

原理:
- 目錄頁:優先試「宣告式規則」(子類別填 catalog_selector/content_selector 等
  class 屬性)與「內建模板」(常見免費小說站共用的 HTML 結構),都沒命中才用
  啟發式規則 —— 把同站連結按「數字換成 {n} 的 URL 模式」分群,最大群 = 章節列表;
  再找「涵蓋 ≥90% 章節連結的最深 DOM 容器」,以容器內文件順序為準
  (自然排除頁面上另外的「最新章節」小區塊)
- 章節頁:先試宣告式/內建正文 selector,都沒命中才用文字密度最高、連結最少的
  區塊(瀏覽器閱讀模式原理)
- 編碼:encoding=None,由 Fetcher 依回應自動偵測
- 多頁章節:找「下一页/下一頁」連結,網址只差頁碼字尾(_2 之類)才視為同章
- 目錄摺疊/分頁:full_catalog_url()/catalog_page_urls() 由 downloader_task 呼叫
"""
import hashlib
import os.path
import re
import unicodedata
from collections import Counter
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import SiteAdapter, BookInfo, Chapter

# 章節標題常見字樣(用於分群評分,不是硬條件)
CHAPTER_HINT = re.compile(
    r"第\s*[0-9零一二三四五六七八九十百千萬万兩两]+\s*[章节節回話话卷]"
    r"|楔子|序章|番外|尾[声聲]|[终終]章|[后後][记記]"
)

# 逐行廣告/雜訊過濾(安全網,通用於各站)
AD_LINE_PATTERNS = [re.compile(p) for p in [
    r"https?://|www\.",
    r"\.(?:com|net|cc|org|xyz|info|la|me|tw)\b",
    r"本章未完",
    r"[点點][击擊].{0,6}下一[页頁]",
    r"^[（(]?本章完[）)]?$",
    r"最新章[节節]",
    r"[请請][记記]住本站",
    r"加入[书書][签籤]",
    r"推[荐薦]本[书書]",
    r"^\s*(上一[章页頁]|下一[章页頁]|目[录錄]|返回[书書]?[页頁目]?[录錄]?)\s*$",
    r"^\s*(字[体體]|[护護]眼|[关關][灯燈]|[报報][错錯]|[举舉][报報])",
    r"^[>》«»\[\]()（）\s]*$",                      # 只有符號的行(麵包屑分隔等)
    r"背景[颜顏]色|字[体體]大小|繁[简簡][转轉]換|^\[?[繁简簡]\]?$",  # 閱讀設定 UI
    r"^\[?(特大|大|中|小)\]?$",
]]

NEXT_PAGE_TEXT = re.compile(r"^下一?[页頁]")
PAGE_SUFFIX = re.compile(r"[_-]\d+(?=\.\w+$|/?$)")

# --- 內建模板:常見免費小說站共用的 HTML 結構,命中就直接用,不必猜 ---
MIN_TEMPLATE_CHAPTERS = 5

# 筆趣閣/頂點類常見:同一個 <dl> 裡先塞「最新章節」小清單,真正完整清單在
# 「正文/全文/章節目錄」等 <dt> 標題之後。這兩個清單依序試,命中就用。
TEMPLATE_CATALOG_SELECTORS = [
    "#list dl", ".listmain dl",
    "#chapterlist", ".chapter-list", "ul.chapter",
    "#catalog", ".mulu", ".volume-list", ".book_list",
]
TEMPLATE_CONTENT_SELECTORS = [
    "#content", "#chaptercontent", "#htmlContent", "#txt", "#nr1",
    "#booktxt", ".txtnav", ".read-content", "#BookText",
]
LATEST_HEADING_RE = re.compile(r"最新章[节節]")
FULL_LIST_HEADING_RE = re.compile(r"正文|全文|完整目[录錄]|章[节節]目[录錄]|全部章[节節]")

EXPAND_LINK_TEXT_DEFAULT = re.compile(
    r"展開全部|展开全部|查看全部章節|查看全部章节|完整目錄|完整目录|"
    r"更多章節|更多章节|全部章節|全部章节|載入全部|加载全部"
)
EXPAND_LINK_EXCLUDE = re.compile(r"登入|登錄|login|VIP|會員|会员", re.I)
# 「下一頁」語意明確,整份文件搜尋也安全,前綴比對可以吃「下一頁 »」這種文字。
NEXT_CATALOG_PAGE_TEXT = re.compile(r"^下一?[页頁]")
# 「更多」這種泛用詞整頁搜尋容易誤中「更多推薦」之類的不相干連結,只有在
# 明確的分頁標記元素(class/id 帶 page/pagination)內才信任、且要求全字比對。
LOOSE_MORE_PAGE_TEXT = re.compile(r"^(更多|載入更多|加载更多)$")
# 目錄容器裡常見的翻頁/導覽控制項文字,不是章節,絕不能被當成一個 Chapter。
NON_CHAPTER_LINK_TEXT = re.compile(
    r"^(上一?[页頁]|下一?[页頁]|首[页頁]|尾[页頁]|末[页頁]|更多|載入更多|加载更多|\d+)$"
)
# 分頁容器的 class/id 必須整個 token 相符,不接受 bare "page"(太容易誤中版面
# 標記,如 <body class="page">、#page-wrapper)。
PAGER_TOKENS = {"pager", "pagination", "pages", "pagelist", "page-list", "pagenav", "page-nav", "page"}

# 防盜/反爬安全網
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
FULLWIDTH_URL_RE = re.compile(r"[wW]{3}\s*[.．。][a-z0-9]+\s*[.．。][a-z]{2,6}", re.I)
PRIVATE_USE_RE = re.compile(r"[-]")
PRIVATE_USE_RATIO_THRESHOLD = 0.01


class GenericAdapter(SiteAdapter):
    domains = []          # 不靠網域配對,由 get_adapter 當備援使用
    encoding = None       # None = Fetcher 依回應自動偵測
    is_generic = True

    # 子類別/使用者自訂 adapter 只需要填以下欄位,不必改寫解析邏輯:
    catalog_selector = None       # CSS selector,指到目錄章節清單的容器
    content_selector = None       # CSS selector,指到正文容器
    remove_selectors = ()         # 額外要從正文移除的 CSS selector
    expand_link_text = None       # 額外的「展開全部章節」連結文字(str 或 list)
    catalog_skip_until = None     # 額外的「完整清單起點」關鍵字(str 或 list)

    def __init__(self):
        self._base_url = ""
        self._catalog_container = None  # 供 catalog_page_urls 縮小分頁搜尋範圍
        self.template_name = None       # 命中的內建模板名稱,供上層 log

    def catalog_url(self, url: str) -> str:
        self._base_url = url  # 記下來給 parse_catalog 解析相對連結用
        return url

    def book_id(self, url: str) -> str:
        p = urlparse(url)
        digits = "-".join(re.findall(r"\d+", p.path))
        if not digits:
            digits = hashlib.md5(p.path.encode()).hexdigest()[:8]
        return f"generic-{p.netloc.replace(':', '_')}-{digits}"

    # --- 目錄展開/分頁 ---
    def _expand_pattern(self):
        extra = self.expand_link_text
        if not extra:
            return EXPAND_LINK_TEXT_DEFAULT
        extra = [extra] if isinstance(extra, str) else list(extra)
        return re.compile(EXPAND_LINK_TEXT_DEFAULT.pattern + "|" +
                           "|".join(re.escape(e) for e in extra))

    def full_catalog_url(self, html: str, url: str):
        soup = BeautifulSoup(html, "lxml")
        host = urlparse(url).netloc
        pattern = self._expand_pattern()
        candidates = []
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True)
            if not text or not pattern.search(text) or EXPAND_LINK_EXCLUDE.search(text):
                continue
            href = a["href"].strip()
            if not href or href.startswith("javascript:") or href == "#":
                continue
            nxt = urljoin(url, href)
            parsed = urlparse(nxt)
            if parsed.scheme not in ("http", "https") or parsed.netloc != host or nxt == url:
                continue
            if nxt not in candidates:
                candidates.append(nxt)
        if not candidates:
            return None

        # 多個「全部章節」連結常見於推薦卡片。只在唯一候選與目前書目有
        # 明確路徑關係時選它，避免跟到推薦書或全站目錄。
        current = urlparse(url)
        current_book_params = [
            (key, value) for key, value in parse_qsl(current.query, keep_blank_values=True)
            if re.fullmatch(r"(?:book|novel)?_?id", key, flags=re.I)
        ]

        def route_stem(path):
            path = unquote(path).rstrip("/") or "/"
            path = re.sub(r"/(?:index|catalog|list|full)(?:\.[^/]+)?$", "", path, flags=re.I)
            return re.sub(r"\.[^/.]+$", "", path).rstrip("/") or "/"

        current_stem = route_stem(current.path)
        related = []
        for candidate in candidates:
            parsed = urlparse(candidate)
            candidate_params = parse_qsl(parsed.query, keep_blank_values=True)
            query_related = bool(current_book_params) and all(
                param in candidate_params for param in current_book_params
            )
            if current_book_params and not query_related:
                continue
            candidate_stem = route_stem(parsed.path)
            path_related = (
                candidate_stem == current_stem
                or candidate_stem.startswith(current_stem + "/")
            )
            if path_related or query_related:
                related.append(candidate)
        return related[0] if len(related) == 1 else None

    def catalog_page_urls(self, html: str, url: str) -> list:
        # 這個 hook 只會用在目錄頁(不是章節正文頁),分頁控制項通常在章節清單
        # 容器「外面」(清單下方的頁碼列),所以「下一頁」文字要整頁搜尋,
        # 不能只找 self._catalog_container 裡面,否則常常什麼都找不到。
        soup = BeautifulSoup(html, "lxml")
        host = urlparse(url).netloc
        found, seen = [], {url}

        def _accept(href):
            nxt = urljoin(url, href)
            if nxt in seen or urlparse(nxt).netloc != host:
                return None
            seen.add(nxt)
            return nxt

        def _has_pager_token(el):
            # 用「整個 class/id token」比對,不是子字串。單獨的 "page" 很常
            # 被當作整頁版面 class,只有本身含至少兩個數字頁碼連結的窄容器才接受。
            classes = [c.lower() for c in (el.get("class") or [])]
            node_id = (el.get("id") or "").lower()
            tokens = [c for c in classes if c in PAGER_TOKENS]
            if node_id in PAGER_TOKENS:
                tokens.append(node_id)
            if not tokens:
                return False
            if "page" not in tokens:
                return True
            if getattr(el, "name", None) in ("body", "html"):
                return False
            numeric_controls = [
                node for node in el.find_all(["a", "span", "strong", "em", "b"])
                if (node.get_text(strip=True) or "").isdigit()
                and (node.name != "a" or node.get("href"))
            ]
            top_level_controls = [
                node for node in numeric_controls
                if not any(
                    descendant is candidate
                    for descendant in node.find_all(["a", "span", "strong", "em", "b"])
                    for candidate in numeric_controls
                )
            ]
            return len(top_level_controls) >= 2

        def _is_pagination_marked(el, max_depth=4):
            # 只往上找幾層(附近的分頁容器),不要一路走到 body/html,
            # 避免頁面最外層的版面標記把整頁所有元素都算成分頁控制項。
            node, depth = el, 0
            while node is not None and depth <= max_depth:
                if hasattr(node, "get") and _has_pager_token(node):
                    return True
                node = getattr(node, "parent", None)
                depth += 1
            return False

        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True) or ""
            # 「下一頁」語意夠明確,整頁找也安全;「更多」這種泛用詞只信任
            # 分頁標記元素內、且要求全字比對,避免整頁掃描誤中「更多推薦」。
            if NEXT_CATALOG_PAGE_TEXT.match(text) or (
                LOOSE_MORE_PAGE_TEXT.match(text) and _is_pagination_marked(a)
            ):
                nxt = _accept(a["href"])
                if nxt:
                    found.append(nxt)

        for select in soup.find_all("select"):
            # 沒有分頁標記就跳過:字體大小、編碼、主題切換這類 <select> 也常常
            # 剛好有兩個以上帶數字的 option(如 16/18),不能單靠數字判斷。
            if not _is_pagination_marked(select):
                continue
            options = [o for o in select.find_all("option") if o.get("value") and re.search(r"\d", o["value"])]
            if len(options) < 2:
                continue
            for option in options:
                nxt = _accept(option["value"])
                if nxt:
                    found.append(nxt)

        for el in soup.find_all(True):
            if not _has_pager_token(el):
                continue
            for a in el.find_all("a", href=True):
                if (a.get_text(strip=True) or "").isdigit():
                    nxt = _accept(a["href"])
                    if nxt:
                        found.append(nxt)
        return found

    def parse_catalog_page(self, html: str, url: str) -> BookInfo:
        self._base_url = url
        return self.parse_catalog(html)

    # --- 目錄 ---
    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        base = self._base_url
        title, author = self._title_author(soup)

        items = self._declarative_or_template_links(soup, base)
        if items is not None:
            chapters = [Chapter(title=text, url=absu) for _a, absu, text in items]
            return BookInfo(title=title, author=author, chapters=chapters)

        return self._heuristic_parse_catalog(soup, base, title, author)

    def _declarative_or_template_links(self, soup, base):
        """回傳 [(a_tag, 絕對網址, 文字), ...],或 None 表示交給啟發式規則。"""
        if self.catalog_selector:
            containers = soup.select(self.catalog_selector)
            self._catalog_container = containers[0] if containers else soup
            items = []
            for container in containers:
                items.extend(self._extract_container_links(container, base))
            self.template_name = "declarative"
            return items  # 明確指定 selector,即使章數少也信任使用者設定

        for selector in TEMPLATE_CATALOG_SELECTORS:
            containers = soup.select(selector)
            if not containers:
                continue
            items = []
            for container in containers:
                items.extend(self._extract_container_links(container, base))
            if len(items) >= MIN_TEMPLATE_CHAPTERS:
                self._catalog_container = containers[0]
                self.template_name = selector
                return items
        return None

    def _extract_container_links(self, container, base):
        if container.name == "a":
            # selector 直接指到 <a>(例如宣告式的 ".mychapters a"),本身就是一個連結
            if not container.get("href"):
                return []
            text = container.get_text(strip=True)
            return [(container, urljoin(base, container["href"]), text)] if text else []
        dts = container.find_all("dt") if container.name == "dl" else []
        if dts:
            return self._dl_links_skip_latest(container, dts, base)
        items = []
        for a in container.find_all("a", href=True):
            text = a.get_text(strip=True)
            if text and not NON_CHAPTER_LINK_TEXT.match(text):
                items.append((a, urljoin(base, a["href"]), text))
        return items

    def _dl_links_skip_latest(self, container, dts, base):
        """收集 <dl> 內所有 <dd> 連結;文件順序,不受多個 <dt>(多卷小說常見)影響。

        只有在「明確看起來」是最新章節小清單時(第一個 dt 就命中 LATEST_HEADING_RE)
        才略過起始那幾個 dd,直到找到「正文/全文/章節目錄」這類完整清單的 dt 為止;
        找不到這種邊界或第一個 dt 本來就不是「最新章節」,就完整收下所有 dd,
        不能因為多卷小說用多個 dt 分卷就只留下最後一卷。
        """
        keywords = FULL_LIST_HEADING_RE
        extra = self.catalog_skip_until
        if extra:
            extra = [extra] if isinstance(extra, str) else list(extra)
            keywords = re.compile(FULL_LIST_HEADING_RE.pattern + "|" +
                                   "|".join(re.escape(e) for e in extra))

        boundary = None
        if len(dts) > 1 and LATEST_HEADING_RE.search(dts[0].get_text(strip=True)):
            boundary = next((dt for dt in dts[1:] if keywords.search(dt.get_text(strip=True))),
                             dts[1])

        items, skipping = [], boundary is not None
        for child in container.find_all(["dt", "dd"]):
            if skipping:
                if child is boundary:
                    skipping = False
                else:
                    continue
            if child.name != "dd":
                continue
            a = child.find("a", href=True)
            text = a.get_text(strip=True) if a else ""
            if text:
                items.append((a, urljoin(base, a["href"]), text))
        return items

    def _heuristic_parse_catalog(self, soup, base, title, author):
        host = urlparse(base).netloc

        # 1. 收集同站、URL 含數字的連結,按模式分群
        #    路徑逐段正規化:段落含數字就整段換成 {id}(相容 uif2b0 這種英數簡碼)
        groups = {}  # pattern -> [(a_tag, abs_url, text)]
        for a in soup.find_all("a", href=True):
            text = a.get_text().strip()
            if not text:
                continue
            absu = urljoin(base, a["href"])
            pu = urlparse(absu)
            if pu.netloc != host or pu.scheme not in ("http", "https"):
                continue
            path_q = pu.path + (f"?{pu.query}" if pu.query else "")
            pattern = "/".join("{id}" if re.search(r"\d", seg) else seg
                               for seg in path_q.split("/"))
            if "{id}" not in pattern:
                continue
            groups.setdefault(pattern, []).append((a, absu, text))

        if not groups:
            raise ValueError("[自動偵測] 頁面上找不到任何章節樣式的連結,"
                             "此網站可能用 JavaScript 載入目錄,需要寫專屬 adapter")

        def score(links):
            hint = sum(1 for _, _, t in links if CHAPTER_HINT.search(t))
            return len(links) * (1 + hint / len(links))

        links = max(groups.values(), key=score)

        # 2. 找涵蓋 ≥90% 章節連結的最深容器,取其文件順序(排除「最新章節」等小區塊)
        elems, counts = {}, Counter()
        for a, _, _ in links:
            for anc in a.parents:
                if anc is None or anc.name in ("html", "[document]"):
                    continue
                elems[id(anc)] = anc
                counts[id(anc)] += 1
        container, best_depth = None, -1
        threshold = 0.9 * len(links)
        for key, cnt in counts.items():
            if cnt >= threshold:
                depth = sum(1 for _ in elems[key].parents)
                if depth > best_depth:
                    best_depth, container = depth, elems[key]

        self._catalog_container = container

        # 容器內收連結時放寬成「共同前綴」比對:
        # 補回簡碼剛好不含數字而沒進分群的章節(如 czbooks 的 base36 碼)
        link_map = {id(a): (absu, text) for a, absu, text in links}
        prefix = self._common_prefix([absu for _, absu, _ in links])
        order, boundary = self._full_list_boundary(container)

        def collect(skip_before_boundary):
            # dedup 用「同一網址第一次出現」;若容器內先有一段「最新章節」小清單
            # 重複連到後面完整清單的同一章,必須先跳過小清單那次,dedup 才會留下
            # 完整清單裡順序正確的那次連結,而不是把章節整個弄丟。
            seen_local, out = set(), []
            for a in container.find_all("a", href=True):
                if skip_before_boundary and boundary is not None and order.get(id(a), boundary) < boundary:
                    continue
                info = link_map.get(id(a))
                if info:
                    absu, text = info
                else:
                    absu = urljoin(base, a["href"])
                    text = a.get_text().strip()
                    if not text or not prefix or not absu.startswith(prefix) or absu == base:
                        continue
                if absu in seen_local:
                    continue
                seen_local.add(absu)
                out.append((a, absu, text))
            return out

        items = collect(True) if boundary is not None else collect(False)
        if boundary is not None and len(items) < 3:
            items = collect(False)  # 篩過頭(誤判邊界)就退回不過濾,不要把章節丟光

        chapters = self._fix_order([Chapter(title=text, url=absu) for _a, absu, text in items])
        return BookInfo(title=title, author=author, chapters=chapters)

    @staticmethod
    def _full_list_boundary(container):
        """找容器內「最新章節」小清單與「正文/全文/章節目錄」完整清單標題的
        文件順序位置;回傳 (tag→順序 dict, 完整清單起點順序或 None)。
        """
        tags = container.find_all(True)
        order = {id(t): i for i, t in enumerate(tags)}

        def heading_positions(pattern):
            positions = []
            for tag in tags:
                direct = "".join(s for s in tag.find_all(string=True, recursive=False)).strip()
                if direct and len(direct) <= 20 and pattern.search(direct):
                    positions.append(order[id(tag)])
            return positions

        latest_pos = heading_positions(LATEST_HEADING_RE)
        if not latest_pos:
            return order, None
        full_pos = heading_positions(FULL_LIST_HEADING_RE)
        boundary = next((p for p in sorted(full_pos) if p > min(latest_pos)), None)
        return order, boundary

    @staticmethod
    def _common_prefix(urls):
        """章節網址的共同前綴,截到最後一個 / 為止;太短(只剩網站根目錄)就放棄。"""
        prefix = os.path.commonprefix(urls)
        prefix = prefix[:prefix.rfind("/") + 1]
        if len(urlparse(prefix).path) <= 1:
            return ""
        return prefix

    @staticmethod
    def _fix_order(chapters):
        """用 URL 末段數字檢查順序:整體遞減就反轉,亂序就依數字排序,大致遞增則保持原樣。

        只在「每章 URL 末段都是純數字 id」時啟用;英數簡碼站(如 czbooks)
        的數字不代表順序,保持文件順序。
        """
        ids = []
        for ch in chapters:
            m = re.search(r"/(\d+)(?:[_-]\d+)?(?:\.\w+)?/?$", urlparse(ch.url).path)
            if not m:
                return chapters  # 末段不是純數字,保持原順序
            ids.append(int(m.group(1)))
        n = len(ids) - 1
        if n < 2:
            return chapters
        inc = sum(1 for a, b in zip(ids, ids[1:]) if b > a)
        dec = sum(1 for a, b in zip(ids, ids[1:]) if b < a)
        if dec / n > 0.9:
            return chapters[::-1]
        if inc / n < 0.9:  # 亂序(例如網頁把章節分欄直排)
            return [ch for _, ch in sorted(zip(ids, chapters), key=lambda x: x[0])]
        return chapters

    @staticmethod
    def _title_author(soup):
        # 中文小說站慣例 meta(最可靠):og:novel:book_name / og:novel:author
        book_meta = soup.find("meta", property="og:novel:book_name")
        author_meta = soup.find("meta", property="og:novel:author")
        if book_meta and book_meta.get("content", "").strip():
            author = author_meta.get("content", "").strip() if author_meta else ""
            return book_meta["content"].strip(), author

        # og:title 與 h1 互補:站名橫幅常佔用 h1,但 og:title 又常黏作者/後綴。
        # og:title 以 h1 開頭 → h1 是書名(取較乾淨的 h1);否則優先 og:title。
        h1_text = ""
        h1 = soup.find("h1")
        if h1:
            h1_text = h1.get_text().strip()
        og_text = ""
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            og_text = re.split(r"[-_|,，—«»《》]", og["content"])[0].strip()
        if h1_text and og_text.startswith(h1_text):
            title = h1_text
        else:
            title = og_text or h1_text
        if not title and soup.title:
            title = re.split(r"[-_|,，—«»《》]", soup.title.get_text())[0].strip()
        title = re.sub(r"(最新章[节節]|全文.{0,4}[阅閱][读讀]|[免免][费費][阅閱][读讀]"
                       r"|[无無][弹彈]窗|章[节節]?目[录錄]|列表)\s*$", "", title).strip()
        author = ""
        m = re.search(r"作\s*者[::]\s*([^\s<>,，|/：:]{1,20})", soup.get_text())
        if m:
            author = m.group(1).strip()
        return title or "未知書名", author

    # --- 章節 ---
    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        self._strip_hidden(soup)

        node = self._declarative_or_template_content(soup)
        if node is None:
            node = self._heuristic_content_node(soup)
        if node is None:
            return ""

        for selector in self.remove_selectors:
            for el in node.select(selector):
                el.decompose()
        # 正文區塊裡的連結(麵包屑、上一章/下一章、設定鈕)都不是內文,整個移除
        for a in node.find_all("a"):
            a.decompose()

        text = self._extract_lines(node, title)
        self._check_private_use_font(text)
        return text

    @staticmethod
    def _strip_hidden(soup):
        """移除 inline display:none / visibility:hidden / hidden 屬性的隱藏干擾文字。"""
        for el in soup.find_all(True):
            if el.decomposed:
                # find_all 先拍好整份清單;上一輪把某個祖先 decompose 掉時,
                # 它底下的子孫標籤物件也一併被清空,這裡再存取會炸 AttributeError。
                continue
            style = el.get("style", "")
            if el.has_attr("hidden") or (style and HIDDEN_STYLE_RE.search(style)):
                el.decompose()

    def _declarative_or_template_content(self, soup):
        if self.content_selector:
            return soup.select_one(self.content_selector)
        for selector in TEMPLATE_CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if node is not None and len(node.get_text(strip=True)) >= 30:
                return node
        return None

    @staticmethod
    def _heuristic_content_node(soup):
        for tag in soup.find_all(["script", "style", "ins", "iframe", "nav", "form",
                                  "header", "footer", "aside", "select", "button"]):
            tag.decompose()

        # 文字密度最高的區塊 = 正文
        best, best_score = None, 0
        for el in soup.find_all(["div", "article", "section", "td", "main"]):
            text_len = len(el.get_text())
            link_len = sum(len(a.get_text()) for a in el.find_all("a"))
            s = text_len - 3 * link_len
            if s > best_score:
                best, best_score = el, s
        if best is None:
            return None

        # 往下鑽:某個子區塊佔了 ≥95% 文字就取更緊的那層,甩掉外圍雜訊
        while True:
            total_len = len(best.get_text())
            child = next((c for c in best.find_all(["div", "article", "section"], recursive=False)
                          if total_len and len(c.get_text()) >= 0.95 * total_len), None)
            if child is None:
                break
            best = child
        return best

    @staticmethod
    def _extract_lines(best, title):
        title = (title or "").strip()
        lines = []
        for raw in best.get_text("\n").split("\n"):
            line = raw.strip()
            if not line:
                continue
            # 章節標題重複行:完全相同,或是「《書名》+標題」這種短組合行
            if title and (line == title or (title in line and len(line) <= len(title) + 12)):
                continue
            normalized = unicodedata.normalize("NFKC", line)
            if any(p.search(normalized) for p in AD_LINE_PATTERNS) or FULLWIDTH_URL_RE.search(normalized):
                continue
            if lines and line == lines[-1]:
                continue
            lines.append(line)
        return "\n\n".join(lines)

    @staticmethod
    def _check_private_use_font(text):
        """自訂字型反爬:把正文字符換成 Unicode 私用區碼位,一般解析只會得到亂碼。"""
        visible = text.replace("\n", "").replace(" ", "")
        if not visible:
            return
        hits = len(PRIVATE_USE_RE.findall(visible))
        if hits / len(visible) > PRIVATE_USE_RATIO_THRESHOLD:
            raise ValueError("[自動偵測] 此站使用自訂字型加密(私用區字元佔比過高),需專屬 adapter")

    def next_page_url(self, html: str, url: str):
        soup = BeautifulSoup(html, "lxml")
        cur_path = urlparse(url).path
        cur_stem = PAGE_SUFFIX.sub("", cur_path)
        for a in soup.find_all("a", href=True):
            text = a.get_text().strip()
            if not NEXT_PAGE_TEXT.match(text) or "章" in text:
                continue
            nxt = urljoin(url, a["href"])
            nxt_path = urlparse(nxt).path
            if nxt_path == cur_path:
                continue
            if PAGE_SUFFIX.sub("", nxt_path) == cur_stem:  # 只差頁碼字尾 → 同章下一頁
                return nxt
        return None
