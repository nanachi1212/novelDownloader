"""twkan.com（台灣小說網）adapter。"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter


BOOK_ID_RE = re.compile(
    r"/(?:book|txt)/(\d+)(?:[/.]|$)|/ajax_novels/chapterlist/(\d+)\.html",
    re.I,
)
TEXT_AD_RE = re.compile(
    r"記住本站域名|台灣小說網|台灣好書|twkan\.com|ᴛᴡᴋᴀɴ",
    re.I,
)


class TwkanAdapter(SiteAdapter):
    domains = ["twkan.com", "www.twkan.com"]
    encoding = "utf-8"
    # 網站使用 Cloudflare；保留少量並行，避免同一本書瞬間觸發挑戰。
    max_chapter_workers = 3

    @staticmethod
    def _book_id_value(url: str) -> str:
        match = BOOK_ID_RE.search(urlparse(url.strip()).path)
        if not match:
            raise ValueError(f"無法從網址解析 twkan 書籍 id: {url}")
        return match.group(1) or match.group(2)

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url.strip())
        host = parsed.netloc.lower()
        if host not in TwkanAdapter.domains:
            host = "twkan.com"
        return f"https://{host}"

    def catalog_url(self, url: str) -> str:
        book_id = self._book_id_value(url)
        return f"{self._origin(url)}/ajax_novels/chapterlist/{book_id}.html"

    def meta_url(self, url: str):
        book_id = self._book_id_value(url)
        return f"{self._origin(url)}/book/{book_id}.html"

    def book_id(self, url: str) -> str:
        return f"twkan-{self._book_id_value(url)}"

    def parse_meta(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.select_one(".booknav2 h1, .bookbox h1")
        author_node = soup.select_one('.booknav2 a[href*="/author/"], .bookbox a[href*="/author/"]')
        title = title_node.get_text(" ", strip=True) if title_node else ""
        author = author_node.get_text(" ", strip=True) if author_node else ""
        return title, author

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        entries = []
        for link in soup.select('li a[href*="/txt/"]'):
            href = link.get("href", "").strip()
            title = link.get_text(" ", strip=True)
            if not href or not title:
                continue
            item = link.find_parent("li")
            raw_order = item.get("data-num", "") if item else ""
            order = int(raw_order) if raw_order.isdigit() else None
            entries.append((order, Chapter(title, urljoin("https://twkan.com", href))))
        if not entries:
            raise ValueError("twkan 完整目錄找不到章節連結，網站版型可能已改")
        if all(order is not None for order, _chapter in entries):
            entries.sort(key=lambda entry: entry[0])
        return BookInfo(title="", author="", chapters=[chapter for _order, chapter in entries])

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#txtcontent0")
        if node is None:
            raise ValueError("twkan 章節頁找不到正文(#txtcontent0)，網站版型可能已改")
        for selector in (".txtad", ".txtcenter", "script", "style", "ins", "iframe"):
            for element in node.select(selector):
                element.decompose()

        lines = []
        for raw in node.get_text("\n").splitlines():
            line = raw.strip()
            if not line or TEXT_AD_RE.search(line):
                continue
            if title and line == title.strip():
                continue
            lines.append(line)
        if not lines:
            raise ValueError("twkan 章節正文解析結果為空，網站版型可能已改")
        return "\n\n".join(lines)
