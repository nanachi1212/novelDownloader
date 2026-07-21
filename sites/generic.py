"""通用備援 adapter:未註冊的網站用啟發式規則自動解析目錄與正文。

原理:
- 目錄頁:把同站連結按「數字換成 {n} 的 URL 模式」分群,最大群 = 章節列表;
  再找「涵蓋 ≥90% 章節連結的最深 DOM 容器」,以容器內文件順序為準
  (自然排除頁面上另外的「最新章節」小區塊)
- 章節頁:文字密度最高、連結最少的區塊 = 正文(瀏覽器閱讀模式原理)
- 編碼:encoding=None,由 Fetcher 依回應自動偵測
- 多頁章節:找「下一页/下一頁」連結,網址只差頁碼字尾(_2 之類)才視為同章
"""
import hashlib
import os.path
import re
from collections import Counter
from urllib.parse import urljoin, urlparse

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


class GenericAdapter(SiteAdapter):
    domains = []          # 不靠網域配對,由 get_adapter 當備援使用
    encoding = None       # None = Fetcher 依回應自動偵測
    is_generic = True

    def __init__(self):
        self._base_url = ""

    def catalog_url(self, url: str) -> str:
        self._base_url = url  # 記下來給 parse_catalog 解析相對連結用
        return url

    def book_id(self, url: str) -> str:
        p = urlparse(url)
        digits = "-".join(re.findall(r"\d+", p.path))
        if not digits:
            digits = hashlib.md5(p.path.encode()).hexdigest()[:8]
        return f"generic-{p.netloc.replace(':', '_')}-{digits}"

    # --- 目錄 ---
    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        base = self._base_url
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

        # 容器內收連結時放寬成「共同前綴」比對:
        # 補回簡碼剛好不含數字而沒進分群的章節(如 czbooks 的 base36 碼)
        link_map = {id(a): (absu, text) for a, absu, text in links}
        prefix = self._common_prefix([absu for _, absu, _ in links])
        seen, chapters = set(), []
        for a in container.find_all("a", href=True):
            info = link_map.get(id(a))
            if info:
                absu, text = info
            else:
                absu = urljoin(base, a["href"])
                text = a.get_text().strip()
                if not text or not prefix or not absu.startswith(prefix) or absu == base:
                    continue
            if absu in seen:
                continue
            seen.add(absu)
            chapters.append(Chapter(title=text, url=absu))

        chapters = self._fix_order(chapters)
        title, author = self._title_author(soup)
        return BookInfo(title=title, author=author, chapters=chapters)

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
            return ""

        # 往下鑽:某個子區塊佔了 ≥95% 文字就取更緊的那層,甩掉外圍雜訊
        while True:
            total_len = len(best.get_text())
            child = next((c for c in best.find_all(["div", "article", "section"], recursive=False)
                          if total_len and len(c.get_text()) >= 0.95 * total_len), None)
            if child is None:
                break
            best = child

        # 正文區塊裡的連結(麵包屑、上一章/下一章、設定鈕)都不是內文,整個移除
        for a in best.find_all("a"):
            a.decompose()

        title = (title or "").strip()
        lines = []
        for raw in best.get_text("\n").split("\n"):
            line = raw.strip()
            if not line:
                continue
            # 章節標題重複行:完全相同,或是「《書名》+標題」這種短組合行
            if title and (line == title or (title in line and len(line) <= len(title) + 12)):
                continue
            if any(p.search(line) for p in AD_LINE_PATTERNS):
                continue
            if lines and line == lines[-1]:
                continue
            lines.append(line)
        return "\n\n".join(lines)

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
