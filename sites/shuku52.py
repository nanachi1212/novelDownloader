"""52shuku.net adapter: the catalog lists sequential reading pages."""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter


class Shuku52Adapter(SiteAdapter):
    domains = ["52shuku.net", "www.52shuku.net"]
    encoding = "utf-8"

    def __init__(self):
        self._catalog_url = "https://www.52shuku.net/"

    def catalog_url(self, url: str) -> str:
        parsed = urlparse(url)
        path = re.sub(r"_\d+(?=\.html$)", "", parsed.path)
        self._catalog_url = parsed._replace(path=path, query="", fragment="").geturl()
        return self._catalog_url

    def book_id(self, url: str) -> str:
        path = urlparse(self.catalog_url(url)).path.strip("/")
        slug = re.sub(r"\.html$", "", path)
        return "52shuku-" + re.sub(r"[^0-9A-Za-z_-]+", "-", slug.replace("/", "-"))

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        catalog = soup.select_one("ul.list.clearfix")
        if catalog is None:
            raise ValueError("目錄頁找不到閱讀頁清單(ul.list.clearfix),網站版型可能已改")

        chapters = []
        seen = set()
        for link in catalog.select("a[href]"):
            url = urljoin(self._catalog_url, link["href"])
            if url in seen:
                continue
            seen.add(url)
            title = link.get_text(" ", strip=True)
            if title:
                chapters.append(Chapter(title=title, url=url))
        if not chapters:
            raise ValueError("目錄頁的閱讀頁清單是空的,網站版型可能已改")

        heading = soup.select_one("h1")
        full_title = heading.get_text(" ", strip=True) if heading else ""
        title, author = self._split_title_author(full_title)
        return BookInfo(title=title or "未知書名", author=author, chapters=chapters)

    @staticmethod
    def _split_title_author(full_title: str):
        full_title = re.sub(r"小說在線閱讀$", "", full_title).strip()
        if "_" not in full_title:
            return full_title, ""
        title, author = full_title.rsplit("_", 1)
        author = re.sub(r"[【〖\[].*$", "", author).strip()
        return title.strip(), author

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("article#nr1")
        if node is None:
            raise ValueError("閱讀頁找不到內文(article#nr1),網站版型可能已改")
        for tag in node.select("script, style, ins, iframe, form, nav, a, .contentadv, .adsbygoogle"):
            tag.decompose()

        lines = []
        for raw in node.get_text("\n").splitlines():
            line = raw.strip()
            if not line or line == title.strip():
                continue
            if re.search(r"52書庫|52书库|本章未完|點擊下一頁|点击下一页", line, re.I):
                continue
            if lines and line == lines[-1]:
                continue
            lines.append(line)
        return "\n\n".join(lines)
