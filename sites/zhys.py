"""zhys.tw（免費小說）adapter。"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter


BOOK_ID_RE = re.compile(r"/(?:book|read)/(\d+)(?:\.html|/)", re.I)
CHAPTER_PATH_RE = re.compile(r"^/read/(\d+)/(\d+)\.html$", re.I)
TEXT_AD_RE = re.compile(r"zhys\.tw|免費小說|App\s*閱讀|返回目錄", re.I)


class ZhysAdapter(SiteAdapter):
    domains = ["twp.zhys.tw"]
    encoding = "utf-8"
    max_chapter_workers = 3

    @staticmethod
    def _book_id_value(url: str) -> str:
        match = BOOK_ID_RE.search(urlparse(url.strip()).path)
        if not match:
            raise ValueError(
                "zhys 分類頁不是單本小說網址，請貼上 /book/{id}.html "
                "或 /read/{book_id}/{chapter_id}.html 網址"
            )
        return match.group(1)

    def catalog_url(self, url: str) -> str:
        return f"https://twp.zhys.tw/book/{self._book_id_value(url)}.html"

    def book_id(self, url: str) -> str:
        return f"zhys-{self._book_id_value(url)}"

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        catalog = soup.select_one("#full-catalog")
        if catalog is None:
            raise ValueError("zhys 目錄頁找不到完整章節清單(#full-catalog)，網站版型可能已改")

        chapters = []
        seen = set()
        for link in catalog.select('.catalog-wrap a[href*="/read/"]'):
            chapter_url = urljoin("https://twp.zhys.tw/", link.get("href", ""))
            if not CHAPTER_PATH_RE.match(urlparse(chapter_url).path) or chapter_url in seen:
                continue
            title = link.get_text(" ", strip=True) or link.get("title", "").strip()
            if not title:
                continue
            seen.add(chapter_url)
            chapters.append(Chapter(title=title, url=chapter_url))
        if not chapters:
            raise ValueError("zhys 完整目錄是空的，網站版型可能已改")

        h1 = soup.select_one("h1")
        author_node = soup.select_one('a[href^="/author/"], a[href*="twp.zhys.tw/author/"]')
        title = h1.get_text(" ", strip=True) if h1 else "未知書名"
        author = author_node.get_text(" ", strip=True) if author_node else ""
        return BookInfo(title=title, author=author, chapters=chapters)

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#article-content")
        if node is None:
            raise ValueError("zhys 章節頁找不到正文(#article-content)，網站版型可能已改")
        for tag in node.select("script, style, ins, iframe, form, nav, a"):
            tag.decompose()

        lines = []
        for raw in node.get_text("\n").splitlines():
            line = raw.strip()
            if not line or (title and line == title.strip()) or TEXT_AD_RE.search(line):
                continue
            if lines and line == lines[-1]:
                continue
            lines.append(line)
        if not lines:
            raise ValueError("zhys 章節正文解析結果為空，網站版型可能已改")
        return "\n\n".join(lines)
