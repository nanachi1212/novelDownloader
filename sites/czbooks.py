"""小說狂人 (czbooks.net) adapter"""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .base import SiteAdapter, BookInfo, Chapter


class CzbooksAdapter(SiteAdapter):
    domains = ["czbooks.net", "www.czbooks.net"]
    encoding = "utf-8"

    def catalog_url(self, url: str) -> str:
        """標準化為 /n/{book_code} 格式"""
        m = re.search(r"/n/([a-z0-9]+)", url)
        if not m:
            raise ValueError(f"無法解析 czbooks URL: {url}")
        return f"https://czbooks.net/n/{m.group(1)}"

    def book_id(self, url: str) -> str:
        """從 URL 中提取書籍 ID（含站點名稱）"""
        m = re.search(r"/n/([a-z0-9]+)", url)
        return f"czbooks-{m.group(1)}" if m else "czbooks-unknown"

    def parse_catalog(self, html: str) -> BookInfo:
        """抓取目錄頁，提取書名和章節清單"""
        soup = BeautifulSoup(html, "lxml")

        info = soup.select_one(".info")
        title = ""
        if info:
            m = re.search(r"《([^》]+)》", info.get_text(" ", strip=True))
            title = m.group(1).strip() if m else ""
        if not title:
            title_elem = soup.find("h2") or soup.find("h1")
            title = title_elem.get_text(strip=True) if title_elem else ""
        if not title and soup.title:
            m = re.search(r"《([^》]+)》", soup.title.get_text(" ", strip=True))
            title = m.group(1).strip() if m else ""
        title = re.sub(r"[《》【】]", "", title).strip() or "未知書名"

        # 提取作者
        author = ""
        if info:
            m = re.search(r"作者:\s*([^\s]+)", info.get_text(" ", strip=True))
            author = m.group(1).strip() if m else ""
        for text in soup.find_all(string=re.compile(r"作者")):
            if author:
                break
            parent = text.parent
            if parent:
                link = parent.find("a")
                if link:
                    author = link.get_text().strip()
                    break

        # 提取章節 — 找最長的 ul，應該是章節清單
        chapters = []
        all_lists = soup.find_all("ul")
        max_list = None
        max_items = 0
        for ul in all_lists:
            items = ul.find_all("li", recursive=False)
            if len(items) > max_items:
                max_items = len(items)
                max_list = ul

        if max_list and max_items > 1:
            for li in max_list.find_all("li", recursive=False):
                link = li.find("a")
                if not link:
                    continue
                chapter_title = link.get_text().strip()
                chapter_url = link.get("href", "")

                # 跳過「卷」標題（沒有連結的列表項）
                if not chapter_url or not chapter_title or chapter_title.endswith("卷"):
                    continue

                if not chapter_url.startswith("http"):
                    chapter_url = urljoin("https://czbooks.net", chapter_url)

                # 確保是章節連結
                if "/n/" in chapter_url:
                    chapters.append(Chapter(title=chapter_title, url=chapter_url))

        return BookInfo(title=title, author=author, chapters=chapters)

    def parse_chapter(self, html: str, title: str = "") -> str:
        """抓取章節內容，過濾廣告"""
        soup = BeautifulSoup(html, "lxml")

        # 找內容區域（通常在 article 或特定 class 的 div）
        content = soup.select_one(".chapter-detail .content")
        if not content:
            content = soup.find("article")
        if not content:
            content = soup.find("div", class_=re.compile(r"content|chapter|text|body", re.I))
        if not content:
            return ""

        # 移除廣告和不需要的元素
        for tag in content.find_all(["script", "ins", "iframe", "nav", "form", "style"]):
            tag.decompose()

        # 移除常見廣告 classes
        for cls in ["ad", "advertisement", "adv", "sidebar", "comment", "related"]:
            for elem in content.find_all(class_=re.compile(cls, re.I)):
                elem.decompose()

        # 移除推薦區域
        for elem in content.find_all(string=re.compile(r"推薦|廣告|評論|相關", re.I)):
            parent = elem.parent
            if parent and parent.name in ["div", "section", "aside"]:
                parent.decompose()

        # 提取文字
        text = content.get_text()

        # 逐行過濾廣告文字和雜訊
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

            # 過濾廣告文本
            if any(pattern in stripped for pattern in [
                "czbooks", "狂人", "www.", ".net", ".com",
                "點擊", "下載", "廣告", "推薦", "評論", "相關",
                "登入", "註冊", "收藏", "分享"
            ]):
                continue

            # 過濾只有特殊字符的行
            if stripped and (not filtered_lines or stripped != filtered_lines[-1].strip()):
                filtered_lines.append(line)

        # 合併並清理
        text = "\n".join(filtered_lines).strip()

        # 壓縮多餘空行
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")

        return text
