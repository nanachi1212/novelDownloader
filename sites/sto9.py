"""sto9.com（思兔）adapter。"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter


BOOK_ID_RE = re.compile(
    r"/(?:book|txt)/(\d+)(?:[/.]|$)|/ajax_novels/chapterlist/(\d+)\.html",
    re.I,
)
TEXT_AD_RE = re.compile(r"sto9\.com|免費小說|返回目錄|最新更新", re.I)


class Sto9Adapter(SiteAdapter):
    domains = ["sto9.com", "www.sto9.com"]
    encoding = "utf-8"
    max_chapter_workers = 3

    @staticmethod
    def _book_id_value(url: str) -> str:
        match = BOOK_ID_RE.search(urlparse(url.strip()).path)
        if not match:
            raise ValueError(f"無法從網址解析 sto9 書籍 id: {url}")
        return match.group(1) or match.group(2)

    @staticmethod
    def _origin(url: str) -> str:
        host = urlparse(url.strip()).netloc.lower()
        return f"https://{host if host in Sto9Adapter.domains else 'sto9.com'}"

    def catalog_url(self, url: str) -> str:
        return f"{self._origin(url)}/ajax_novels/chapterlist/{self._book_id_value(url)}.html"

    def meta_url(self, url: str):
        return f"{self._origin(url)}/book/{self._book_id_value(url)}.html"

    def book_id(self, url: str) -> str:
        return f"sto9-{self._book_id_value(url)}"

    def parse_meta(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        title_node = soup.select_one(".booknav2 h1, .bookbox h1, h1")
        author_node = soup.select_one(
            '.booknav2 a[href*="/author/"], .bookbox a[href*="/author/"]'
        )
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
            entries.append((order, Chapter(title, urljoin("https://sto9.com", href))))
        if not entries:
            raise ValueError("sto9 完整目錄找不到章節連結，網站版型可能已改")
        if all(order is not None for order, _chapter in entries):
            entries.sort(key=lambda entry: entry[0])
        return BookInfo(title="", author="", chapters=[chapter for _order, chapter in entries])

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one(".txtnav")
        if node is None:
            raise ValueError("sto9 章節頁找不到正文(.txtnav)，網站版型可能已改")
        for selector in ("h1", ".txtad", ".txtright", ".txtcenter", "script", "style", "ins", "iframe", "nav", "a"):
            for element in node.select(selector):
                element.decompose()

        lines = []
        for raw in node.get_text("\n").splitlines():
            line = raw.strip()
            if not line or (title and line == title.strip()) or TEXT_AD_RE.search(line):
                continue
            lines.append(line)
        if not lines:
            raise ValueError("sto9 章節正文解析結果為空，網站版型可能已改")
        return "\n\n".join(lines)
