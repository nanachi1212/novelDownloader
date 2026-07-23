"""8book.com adapter."""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter, SiteAdapter


AD_LINE_RE = re.compile(
    r"8book|無限小說|請記住本站|加入書簽|章節列表|報錯|開燈|關燈|本章未完",
    re.I,
)


class Book8Adapter(SiteAdapter):
    domains = [
        "8book.com",
        "www.8book.com",
        "finance.binaccount.com",
    ]
    encoding = "utf-8"

    @staticmethod
    def _book_number(url: str) -> str:
        parsed = urlparse(url)
        match = re.search(r"/(?:novelbooks|read)/(\d+)", parsed.path)
        if match:
            return match.group(1)
        raise ValueError(f"無法從網址解析 8book 書籍 id: {url}")

    def catalog_url(self, url: str) -> str:
        return f"https://www.8book.com/novelbooks/{self._book_number(url)}/"

    def book_id(self, url: str) -> str:
        return f"8book-{self._book_number(url)}"

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        title = ""
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()
        if not title and soup.title:
            title = re.split(r"[,，|-]", soup.title.get_text(" ", strip=True))[0].strip()

        author = ""
        keywords = soup.find("meta", attrs={"name": "keywords"})
        if keywords and keywords.get("content"):
            match = re.search(r"([^,，\s]+)作品", keywords["content"])
            if match:
                author = match.group(1)
        if not author:
            match = re.search(r"作者[:：]\s*([^\s<,，]+)", soup.get_text(" ", strip=True))
            if match:
                author = match.group(1)

        chapters = []
        seen = set()
        for link in soup.find_all("a", href=True):
            text = link.get_text(" ", strip=True)
            href = urljoin("https://www.8book.com/", link["href"])
            if not text or href in seen:
                continue
            if text.startswith("開始看"):
                continue
            parsed = urlparse(href)
            if not re.search(r"/read/\d+/", parsed.path) or not parsed.query.isdigit():
                continue
            seen.add(href)
            chapters.append(Chapter(text, href))

        if not chapters:
            raise ValueError("目錄頁找不到章節連結(/read/{book_id}/?{chapter_id}),網站版型可能已改")

        return BookInfo(title=title or "未知書名", author=author, chapters=chapters)

    @staticmethod
    def _meta_content(soup, name: str, default: str = "") -> str:
        tag = soup.find("meta", attrs={"name": name})
        return tag.get("content", "").strip() if tag else default

    @staticmethod
    def _js_int(html: str, name: str, default: int) -> int:
        match = re.search(rf"var\s+{re.escape(name)}\s*=\s*(\d+)", html)
        return int(match.group(1)) if match else default

    def chapter_source_url(self, html: str, url: str):
        soup = BeautifulSoup(html, "lxml")
        text_node = soup.select_one("#text")
        if text_node is not None and text_node.get_text(strip=True):
            return None

        book_id = self._meta_content(soup, "itemid") or self._book_number(url)
        category_id = self._meta_content(soup, "catid", "4")
        parsed = urlparse(url)
        query = parsed.query.split("&", 1)[0]
        chapter_id = query.split("_", 1)[0]
        chapter_id = re.sub(r"\D", "", chapter_id)
        if not chapter_id:
            return None

        ids_match = re.search(r'var\s+hni2v2c31\s*=\s*"([^"]+)"\.split', html)
        if ids_match is None:
            return None
        secret = ids_match.group(1).split(",")[-1]
        pe = self._js_int(html, "pe682qj", 7)
        ea = self._js_int(html, "ea8v8420", 3)
        nk = self._js_int(html, "nk____", 100)
        suffix_len = self._js_int(html, "t8pj3yk__", 5)
        start = (int(chapter_id) * ea) % nk
        suffix = secret[start:start + suffix_len]
        if not suffix:
            return None
        return f"https://8book.com/txt/{category_id}/{book_id}/{chapter_id}{suffix}.html"

    def parse_chapter(self, html: str, title: str = "") -> str:
        soup = BeautifulSoup(html, "lxml")
        node = soup.select_one("#text")
        if node is None or not node.get_text(strip=True):
            node = soup.select_one(".text, .chapter-content, .read-content, article, body")
        if node is None:
            raise ValueError("章節頁找不到內文(#text),網站版型或動態載入流程可能已改")

        for tag in node.select("script, style, ins, iframe, form, nav, a, button"):
            tag.decompose()

        lines = []
        for raw in node.get_text("\n").splitlines():
            line = raw.strip()
            if not line or line == title.strip():
                continue
            if AD_LINE_RE.search(line):
                continue
            if lines and line == lines[-1]:
                continue
            lines.append(line)
        if not lines:
            raise ValueError("章節頁正文是空的,可能需要瀏覽器執行 JavaScript 後才能讀取")
        return "\n\n".join(lines)
