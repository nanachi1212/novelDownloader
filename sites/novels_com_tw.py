"""novels.com.tw（繁體小說）adapter。"""
import base64
import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .base import BookInfo, Chapter, SiteAdapter


BOOK_ID_RE = re.compile(r"/novels/(no[0-9a-f]{64})(?:/|$)", re.I)
CHAPTER_PATH_RE = re.compile(
    r"^/novels/no[0-9a-f]{64}/\d+(?:_\d+)?\.html$", re.I
)
ENCRYPTED_CONTENT_RE = re.compile(
    r"window\.encryptedContent\s*=\s*(\"(?:\\.|[^\"\\])*\")", re.S
)
TEXT_AD_RE = re.compile(
    r"novels\.com\.tw|請前往.*繼續全文閱讀|加入書籤|本章未完|繁體小說",
    re.I,
)

# 網站目前在公開 reader JavaScript 中使用的 AES-128-CBC key 與零 IV。
CONTENT_KEY = b"WZc0cbzgY3lhz3X6"
CONTENT_IV = bytes(16)


class NovelsComTwAdapter(SiteAdapter):
    domains = ["novels.com.tw", "www.novels.com.tw"]
    encoding = "utf-8"
    max_chapter_workers = 3

    @staticmethod
    def _book_id_value(url: str) -> str:
        match = BOOK_ID_RE.search(urlparse(url.strip()).path)
        if not match:
            raise ValueError(f"無法從網址解析 novels.com.tw 書籍 id: {url}")
        return match.group(1).lower()

    def catalog_url(self, url: str) -> str:
        return f"https://www.novels.com.tw/novels/{self._book_id_value(url)}/"

    def book_id(self, url: str) -> str:
        return f"novels-com-tw-{self._book_id_value(url)}"

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        catalog = soup.select_one("#catalog .chapters")
        if catalog is None:
            raise ValueError("novels.com.tw 目錄頁找不到完整章節清單(#catalog .chapters)，網站版型可能已改")

        chapters = []
        seen = set()
        for link in catalog.select("a[href]"):
            chapter_url = urljoin("https://www.novels.com.tw/", link.get("href", ""))
            if not CHAPTER_PATH_RE.match(urlparse(chapter_url).path) or chapter_url in seen:
                continue
            title = link.get_text(" ", strip=True)
            if not title:
                continue
            seen.add(chapter_url)
            chapters.append(Chapter(title=title, url=chapter_url))
        if not chapters:
            raise ValueError("novels.com.tw 完整目錄是空的，網站版型可能已改")

        title_meta = soup.find("meta", property="og:novel:book_name")
        author_meta = soup.find("meta", property="og:novel:author")
        title = title_meta.get("content", "").strip() if title_meta else ""
        author = author_meta.get("content", "").strip() if author_meta else ""
        if not title:
            h1 = soup.select_one("h1")
            title = h1.get_text(" ", strip=True) if h1 else "未知書名"
        return BookInfo(title=title, author=author, chapters=chapters)

    @staticmethod
    def _decrypt_content(html: str) -> str:
        match = ENCRYPTED_CONTENT_RE.search(html)
        if not match:
            return ""
        try:
            encoded = json.loads(match.group(1))
            ciphertext = base64.b64decode(encoded, validate=True)
            decryptor = Cipher(
                algorithms.AES(CONTENT_KEY), modes.CBC(CONTENT_IV)
            ).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = PKCS7(128).unpadder()
            plaintext = unpadder.update(padded) + unpadder.finalize()
            # 網站在 WebCrypto 的 PKCS7 padding 之外，還保留一層舊式 padding。
            if plaintext and 1 <= plaintext[-1] <= 16:
                pad_length = plaintext[-1]
                if plaintext.endswith(bytes([pad_length]) * pad_length):
                    plaintext = plaintext[:-pad_length]
            return plaintext.decode("utf-8")
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError(f"novels.com.tw 章節正文解密失敗: {exc}") from exc

    def parse_chapter(self, html: str, title: str = "") -> str:
        decrypted = self._decrypt_content(html)
        soup = BeautifulSoup(decrypted or html, "lxml")
        node = soup if decrypted else soup.select_one("#chapter-content")
        if node is None:
            raise ValueError("novels.com.tw 章節頁找不到正文(#chapter-content)，網站版型可能已改")
        for tag in node.select("script, style, ins, iframe, form, nav, a, .adsbygoogle"):
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
            raise ValueError("novels.com.tw 章節正文解析結果為空，網站版型或加密方式可能已改")
        return "\n\n".join(lines)

    def next_page_url(self, html: str, url: str):
        soup = BeautifulSoup(html, "lxml")
        link = soup.select_one("a#next_url")
        if link is None or "下一頁" not in link.get_text(" ", strip=True):
            return None
        href = link.get("data-real-href") or link.get("href", "")
        next_url = urljoin(url, href)
        current_path = urlparse(url).path
        next_path = urlparse(next_url).path
        current_stem = re.sub(r"_\d+(?=\.html$)", "", current_path)
        next_stem = re.sub(r"_\d+(?=\.html$)", "", next_path)
        if next_path != current_path and next_stem == current_stem:
            return next_url
        return None
