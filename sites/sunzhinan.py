"""醋溜儿文学 (sunzhinan.com) adapter"""
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .base import SiteAdapter, BookInfo, Chapter


class SunzhinanAdapter(SiteAdapter):
    domains = ["sunzhinan.com", "www.sunzhinan.com"]
    encoding = "utf-8"

    def catalog_url(self, url: str) -> str:
        """標準化為 /books/{book_id}/ 格式"""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if "/books/" in path:
            parts = path.split("/")
            book_id = parts[-1]
            return f"https://www.sunzhinan.com/books/{book_id}/"
        if "/read/" in path:
            parts = path.split("/")
            book_id = parts[2]
            return f"https://www.sunzhinan.com/books/{book_id}/"
        raise ValueError(f"無法解析 sunzhinan URL: {url}")

    def book_id(self, url: str) -> str:
        """從 URL 中提取書籍 ID（含站點名稱以避免衝突）"""
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if "/books/" in path or "/read/" in path:
            parts = path.split("/")
            return f"sunzhinan-{parts[2]}"
        raise ValueError(f"無法解析 sunzhinan URL: {url}")

    def parse_catalog(self, html: str) -> BookInfo:
        """抓取目錄頁，提取書名和章節清單"""
        soup = BeautifulSoup(html, "lxml")

        # 提取書名
        title_elem = soup.find("h1")
        title = title_elem.get_text().strip() if title_elem else "未知書名"

        # 提取作者
        author = ""
        for elem in soup.find_all("a"):
            href = elem.get("href", "")
            if "/author/" in href:
                author = elem.get_text().strip()
                break

        # 提取章節（找最長的 ul 作為章節列表）
        chapters = []
        lists = soup.find_all("ul")
        max_list = None
        max_length = 0
        for ul in lists:
            items = ul.find_all("li", recursive=False)
            if len(items) > max_length:
                max_length = len(items)
                max_list = ul

        if max_list:
            for li in max_list.find_all("li", recursive=False):
                link = li.find("a")
                if not link:
                    continue
                chapter_title = link.get_text().strip()
                chapter_url = link.get("href", "")
                if not chapter_url.startswith("http"):
                    chapter_url = urljoin("https://www.sunzhinan.com", chapter_url)
                if "/read/" in chapter_url:
                    chapters.append(Chapter(title=chapter_title, url=chapter_url))

        return BookInfo(title=title, author=author, chapters=chapters)

    def parse_chapter(self, html: str, title: str = "") -> str:
        """抓取章節內容，過濾廣告"""
        soup = BeautifulSoup(html, "lxml")

        # 找內容區域
        article = soup.find("article")
        if not article:
            return ""

        # 移除廣告和不需要的元素
        for tag in article.find_all(["script", "ins", "iframe"]):
            tag.decompose()

        # 移除常見廣告 classes
        for cls in ["ad", "advertisement", "contentadv", "bottom-ad", "txtinfo", "txtright"]:
            for elem in article.find_all(class_=cls):
                elem.decompose()

        # 提取文字
        text = article.get_text()

        # 過濾廣告文本（正規表達式及字符串）
        import re
        ad_patterns = [
            r"[()（）？\[\]【】♂♀🔞]*来\[\]♂?看最新章节♂?完整章节[()（）？\[\]【】♂♀🔞]*",
            r"[()（）？\[\]【】♂♀🔞]*最新章节.*?完整章节[()（）？\[\]【】♂♀🔞]*",
            r"sunzhinan",
            r"www\.\S+\.(com|net|cc|org)",
            r"醋溜儿",
            r"書城",
            r"廣告",
            r"點擊下載",
        ]

        lines = text.split("\n")
        filtered_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                filtered_lines.append("")
                continue

            # 檢查是否與標題重複
            if title and stripped == title:
                continue

            # 檢查廣告模式
            is_ad = False
            for pattern in ad_patterns:
                if re.search(pattern, stripped, re.IGNORECASE):
                    is_ad = True
                    break

            if is_ad:
                continue

            # 過濾空行和重複行
            if stripped and (not filtered_lines or stripped != filtered_lines[-1].strip()):
                filtered_lines.append(line)

        # 合併並清理
        text = "\n".join(filtered_lines).strip()

        # 壓縮多餘空行
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")

        return text

    def next_page_url(self, html: str, url: str) -> str | None:
        """檢查是否有下一頁（多頁章節）"""
        soup = BeautifulSoup(html, "lxml")

        # 尋找「下一页」連結
        for link in soup.find_all("a"):
            text = link.get_text().strip()
            if "下一页" in text or "下一頁" in text:
                next_url = link.get("href", "")
                if next_url.startswith("http"):
                    return next_url
                return urljoin(url, next_url)

        return None
