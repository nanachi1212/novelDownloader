"""半夏小說 (xbanxia.cc) adapter"""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .base import SiteAdapter, BookInfo, Chapter


class XbanxiaAdapter(SiteAdapter):
    domains = ["xbanxia.cc", "www.xbanxia.cc"]
    encoding = "utf-8"

    def catalog_url(self, url: str) -> str:
        """標準化為 /books/{book_id}.html 格式"""
        m = re.search(r"/books/(\d+)", url)
        if not m:
            raise ValueError(f"無法解析 xbanxia URL: {url}")
        return f"http://www.xbanxia.cc/books/{m.group(1)}.html"

    def book_id(self, url: str) -> str:
        """從 URL 中提取書籍 ID（含站點名稱）"""
        m = re.search(r"/books/(\d+)", url)
        return f"xbanxia-{m.group(1)}" if m else "xbanxia-unknown"

    def parse_catalog(self, html: str) -> BookInfo:
        """抓取目錄頁，提取書名和章節清單"""
        soup = BeautifulSoup(html, "lxml")

        # 提取書名:優先 og:novel:book_name,次為 <title>,最後跳過站名的 h1
        title = ""
        book_meta = soup.find("meta", property="og:novel:book_name")
        if book_meta and book_meta.get("content", "").strip():
            title = book_meta["content"].strip()

        if not title and soup.title:
            # <title> 通常形如「書名, 詳細 - 站名」,取逗號前的部分
            title = re.split(r"[,，]", soup.title.get_text())[0].strip()

        if not title:
            # h1 可能有多個,跳過明顯是站名的(如「半夏小說」),找真正的書名
            for h1 in soup.find_all("h1"):
                h1_text = h1.get_text().strip()
                # 站名通常很短且是固定詞(「半夏小說」「69书吧」等),書名通常更長
                if h1_text and len(h1_text) > 4 and h1_text not in ("半夏小說", "69书吧", "69shuba"):
                    title = h1_text
                    break

        if not title:
            title = "未知書名"

        # 提取作者
        author = ""
        author_meta = soup.find("meta", property="og:novel:author")
        if author_meta and author_meta.get("content", "").strip():
            author = author_meta["content"].strip()
        if not author:
            for elem in soup.find_all(string=re.compile(r"作者")):
                parent = elem.parent
                if parent:
                    link = parent.find("a")
                    if link:
                        author = link.get_text().strip()
                        break

        # 提取章節 — 直接搜尋所有匹配 /books/{id}/{cid}.html 的連結
        chapters = []
        book_id_match = re.search(r"/books/(\d+)", html)
        if book_id_match:
            book_id = book_id_match.group(1)
            pattern = f"/books/{book_id}/\\d+"
            # 收集所有章節連結，然後按 URL 順序排序（確保 cid 遞增）
            chapter_links = []
            for link in soup.find_all("a", href=re.compile(pattern)):
                chapter_title = link.get_text().strip()
                chapter_url = link.get("href", "")
                if not chapter_url.startswith("http"):
                    chapter_url = urljoin("http://www.xbanxia.cc", chapter_url)
                if chapter_title:
                    # 提取 cid 以便排序
                    cid_match = re.search(r"/(\d+)\.html", chapter_url)
                    cid = int(cid_match.group(1)) if cid_match else 0
                    chapter_links.append((cid, chapter_title, chapter_url))

            # 按 cid 排序
            chapter_links.sort(key=lambda x: x[0])
            for _, ch_title, url in chapter_links:
                chapters.append(Chapter(title=ch_title, url=url))

        return BookInfo(title=title, author=author, chapters=chapters)

    def parse_chapter(self, html: str, title: str = "") -> str:
        """抓取章節內容，過濾廣告"""
        soup = BeautifulSoup(html, "lxml")

        # 找內容區域（article 標籤）
        article = soup.find("article")
        if not article:
            return ""

        # 移除廣告和不需要的元素
        for tag in article.find_all(["script", "ins", "iframe", "nav", "form"]):
            tag.decompose()

        # 移除常見廣告 classes 和 ids
        for cls in ["ad", "advertisement", "adv", "error-hint", "report-error"]:
            for elem in article.find_all(class_=cls):
                elem.decompose()

        # 移除推薦區域
        for elem in article.find_all(string=re.compile(r"每日推薦|推薦閱讀")):
            parent = elem.parent
            if parent:
                parent_parent = parent.parent
                if parent_parent:
                    parent_parent.decompose()

        # 提取文字
        text = article.get_text()

        # 逐行過濾廣告文字
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
                "xbanxia", "半夏小說", "www.", ".cc", ".com", ".net",
                "點擊", "下載", "廣告", "點錯", "錯誤提交", "問題類型",
                "每日推薦", "推薦閱讀", "快樂很多", "相關推薦"
            ]):
                continue

            # 過濾只有空格或特殊字符的行
            if stripped and (not filtered_lines or stripped != filtered_lines[-1].strip()):
                filtered_lines.append(line)

        # 合併並清理
        text = "\n".join(filtered_lines).strip()

        # 壓縮多餘空行
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")

        return text
