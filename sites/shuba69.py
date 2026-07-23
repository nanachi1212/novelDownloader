"""69shuba(69书吧)adapter。"""
import re

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter

# 文字層廣告過濾(結構過濾漏掉時的安全網),逐行比對
AD_LINE_PATTERNS = [
    re.compile(r"69书吧|69書吧|69shuba", re.I),
    re.compile(r"www\.\S+\.(?:com|net|cc|org|xyz|top|info)", re.I),
    re.compile(r"本章未完.{0,10}点击下一页"),
    re.compile(r"^[（(]本章完[）)]$"),
]
DATE_AUTHOR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}.*作者")


class Shuba69(SiteAdapter):
    domains = [
        "69shuba.com",
        "www.69shuba.com",
        "69shu.com",
        "www.69shu.com",
        "69shuba.tw",
        "www.69shuba.tw",
    ]
    encoding = "gbk"

    def catalog_url(self, url: str) -> str:
        m = re.search(r"/(?:book|indexlist)/(\d+)", url)
        if not m:
            raise ValueError(f"無法從網址解析書籍 id: {url}")
        if "69shuba.tw" in url:
            return f"https://69shuba.tw/indexlist/{m.group(1)}/"
        base = url.split("/book/")[0]
        return f"{base}/book/{m.group(1)}/"

    def book_id(self, url: str) -> str:
        m = re.search(r"/(?:book|indexlist)/(\d+)", url)
        return f"69shuba-{m.group(1)}" if m else "69shuba-unknown"

    def meta_url(self, url: str):
        m = re.search(r"/(?:book|indexlist)/(\d+)", url)
        if "69shuba.tw" in url:
            return f"https://69shuba.tw/book/{m.group(1)}.htm"
        base = url.split("/book/")[0]
        return f"{base}/book/{m.group(1)}.htm"

    def parse_meta(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        a_tag = soup.select_one('a[href*="author.php"]')
        author = a_tag.get_text(strip=True) if a_tag else ""
        return (title, author)

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        items = soup.select("#catalog li")
        if not items:
            items = soup.select(".catalog li, .listmain li, .chapter-list li")
        entries = []
        for li in items:
            a = li.select_one("a")
            if a is None or not a.get("href"):
                continue
            num = li.get("data-num")
            entries.append((int(num) if num and num.isdigit() else None,
                            Chapter(a.get_text(strip=True), a["href"])))
        if not entries:
            raise ValueError("目錄頁找不到章節清單(#catalog li a),網站版型可能已改")
        # 原始 HTML 是新→舊;有 data-num 就照它升冪排,否則整串反轉
        if all(n is not None for n, _ in entries):
            entries.sort(key=lambda e: e[0])
            chapters = [c for _, c in entries]
        else:
            chapters = [c for _, c in reversed(entries)]

        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        title = re.sub(r"最新章节$", "", title).strip()
        return BookInfo(title=title, author="", chapters=chapters)

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("div.txtnav")
        if node is None:
            raise ValueError("章節頁找不到內文(div.txtnav),網站版型可能已改")
        # 結構過濾:標題、資訊列、廣告區塊全部移除
        for sel in ("h1", ".txtinfo", ".txtright", ".contentadv", ".bottom-ad",
                    "script", "style", "ins", "iframe", "a"):
            for el in node.select(sel):
                el.decompose()

        lines = []
        for raw in node.get_text("\n").split("\n"):
            line = raw.strip()  # 含 　 全形空格、\xa0、  等 Unicode 空白
            if not line:
                continue
            if any(p.search(line) for p in AD_LINE_PATTERNS):
                continue
            if DATE_AUTHOR_RE.match(line):
                continue
            if title and line == title.strip():  # 內文開頭重複的章節標題
                continue
            lines.append("　　" + line)
        return "\n\n".join(lines)
