"""novel543.com and its rotating chapter-reader domains."""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter


class Novel543Adapter(SiteAdapter):
    domains = [
        "novel543.com",
        "www.novel543.com",
        "look.thisiscm.com",
        "look.twword.com",
    ]
    encoding = "utf-8"

    @staticmethod
    def _book_number(url: str) -> str:
        match = re.search(r"/(\d{6,})/", urlparse(url).path + "/")
        if not match:
            raise ValueError(f"無法從網址解析 novel543 書籍 id: {url}")
        return match.group(1)

    def catalog_url(self, url: str) -> str:
        return f"https://www.novel543.com/{self._book_number(url)}/dir"

    def book_id(self, url: str) -> str:
        return f"novel543-{self._book_number(url)}"

    def meta_url(self, url: str):
        return f"https://www.novel543.com/{self._book_number(url)}/"

    def parse_meta(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        title_meta = soup.find("meta", property="og:novel:book_name")
        author_meta = soup.find("meta", property="og:novel:author")
        title = title_meta.get("content", "").strip() if title_meta else ""
        author = author_meta.get("content", "").strip() if author_meta else ""
        return title, author

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        catalog = soup.select_one("ul.all")
        if catalog is None:
            raise ValueError("目錄頁找不到完整章節清單(ul.all),網站版型可能已改")

        chapters = []
        seen = set()
        for link in catalog.select("a[href]"):
            url = urljoin("https://www.novel543.com/", link["href"])
            if url in seen:
                continue
            seen.add(url)
            title = link.get_text(" ", strip=True)
            if title:
                chapters.append(Chapter(title=title, url=url))
        if not chapters:
            raise ValueError("完整章節清單是空的,網站版型可能已改")

        h1 = soup.select_one("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
        title = re.sub(r"\s*章[節节]列表$", "", title).strip()
        h2 = soup.select_one("h2")
        author = h2.get_text(" ", strip=True) if h2 else ""
        author = re.sub(r"^作者\s*/\s*", "", author).strip()
        return BookInfo(title=title or "未知書名", author=author, chapters=chapters)

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#chapterWarp .chapter-content .content")
        if node is None:
            raise ValueError("章節頁找不到內文(#chapterWarp .content),網站版型或跳轉流程可能已改")
        for tag in node.select("script, style, ins, iframe, form, nav, a, .adsbygoogle"):
            tag.decompose()

        lines = []
        for raw in node.get_text("\n").splitlines():
            line = raw.strip()
            if not line or line == title.strip():
                continue
            if re.search(r"novel543|稷下書院|請記住本站|本章未完", line, re.I):
                continue
            if lines and line == lines[-1]:
                continue
            lines.append(line)
        return "\n\n".join(lines)

