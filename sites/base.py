"""站點 adapter 介面。新增網站時繼承 SiteAdapter 並在 sites/__init__.py 註冊。"""
from dataclasses import dataclass


@dataclass
class Chapter:
    title: str
    url: str


@dataclass
class BookInfo:
    title: str
    author: str
    chapters: list


class SiteAdapter:
    domains: list = []      # 可處理的網域,例如 ["69shuba.com", "www.69shuba.com"]
    encoding: str = "utf-8"

    def catalog_url(self, url: str) -> str:
        """把使用者貼的網址(簡介頁/目錄頁)正規化成目錄頁網址。"""
        return url

    def book_id(self, url: str) -> str:
        """回傳書籍唯一 id,做為快取資料夾名稱。"""
        raise NotImplementedError

    def meta_url(self, url: str):
        """書名/作者所在頁面(如簡介頁)。回傳 None 表示目錄頁已含完整資訊。"""
        return None

    def parse_meta(self, html: str):
        """回傳 (title, author),供 meta_url 頁面解析用。"""
        return ("", "")

    def parse_catalog(self, html: str) -> BookInfo:
        raise NotImplementedError

    def parse_chapter(self, html: str, title: str = "") -> str:
        """回傳過濾廣告後的乾淨內文。title 用於去除內文開頭重複的章節標題。"""
        raise NotImplementedError

    def next_page_url(self, html: str, url: str):
        """一章拆成多頁的網站用:回傳同一章的下一頁網址,沒有下一頁回傳 None。

        實作時務必區分「同章下一頁」與「下一章」(通常看網址是
        456_2.html 這種頁碼字尾,還是章節 id 變了)。
        """
        return None


def join_pages(parts: list) -> str:
    """串接同一章的多頁內文,去除頁面交界重複的段落(部分網站防爬蟲手法)。"""
    result = []
    for part in parts:
        paras = [p for p in part.split("\n\n") if p]
        if result and paras and result[-1] == paras[0]:
            paras = paras[1:]
        result.extend(paras)
    return "\n\n".join(result)
