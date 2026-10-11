"""Declarative site adapters loaded from sites/rules/*.json."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BookInfo, Chapter
from .generic import GenericAdapter
from .fanqie import FanqieAdapter, FanqieError
from fetcher import Fetcher


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2,
    "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "萬": 10000, "万": 10000}
_CHAPTER_NUMBER_RE = re.compile(
    r"第\s*([0-9０-９一二三四五六七八九十百千万萬兩两零〇]+)\s*[章节節回話话]"
)


def _chapter_number(value: str) -> int | None:
    value = value.translate(_FULLWIDTH_DIGITS)
    if value.isdigit():
        return int(value)
    total = section = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            section = section * 10 + _CHINESE_DIGITS[char]
        elif char in _CHINESE_UNITS:
            unit = _CHINESE_UNITS[char]
            section = section or 1
            if unit >= 10000:
                total += section * unit
                section = 0
            else:
                total += section * unit
                section = 0
        else:
            return None
    return total + section


def _format_template(template: str, match: re.Match) -> str:
    values = {"book_id": match.groupdict().get("book_id", "")}
    values.update(match.groupdict())
    return template.format(**values)


class RuleAdapter(GenericAdapter):
    """GenericAdapter configured entirely by one declarative JSON rule."""

    rule: dict = {}
    rule_id = ""
    adapter_source = "rules"

    def __init__(self):
        super().__init__()
        rule = self.rule
        self.rule_id = rule["id"]
        self.domains = list(rule["domains"])
        self.encoding = rule.get("encoding")
        self.max_chapter_workers = int(rule.get("max_chapter_workers", 8))

        catalog = rule.get("catalog", {})
        content = rule.get("content", {})
        self.catalog_selector = catalog.get("chapter_selector")
        self.content_selector = content.get("selector")
        self.remove_selectors = tuple(content.get("remove_selectors", ()))

    def _book_match(self, url: str) -> re.Match:
        pattern = self.rule.get("url", {}).get("book_id_regex")
        if not pattern:
            raise ValueError(f"{self.rule_id} 規則缺少 url.book_id_regex")
        match = re.search(pattern, url, re.I)
        if not match or not match.groupdict().get("book_id"):
            raise ValueError(f"無法從網址解析 {self.rule_id} 書籍 id: {url}")
        return match

    def catalog_url(self, url: str) -> str:
        match = self._book_match(url)
        template = self.rule.get("url", {}).get("catalog_template")
        normalized = _format_template(template, match) if template else url
        self._base_url = normalized
        return normalized

    def book_id(self, url: str) -> str:
        match = self._book_match(url)
        template = self.rule.get("url", {}).get("cache_id_template", "{book_id}")
        return _format_template(template, match)

    def parse_catalog(self, html: str) -> BookInfo:
        soup = BeautifulSoup(html, "lxml")
        catalog = self.rule.get("catalog", {})

        title = ""
        title_selector = catalog.get("title_selector")
        if title_selector:
            node = soup.select_one(title_selector)
            if node:
                title = node.get_text(" ", strip=True)
        if not title:
            title = catalog.get("default_title", "未知書名")

        author = ""
        author_selector = catalog.get("author_selector")
        if author_selector:
            node = soup.select_one(author_selector)
            if node:
                author = node.get_text(" ", strip=True)
                strip_re = catalog.get("author_strip_regex")
                if strip_re:
                    author = re.sub(strip_re, "", author).strip()
        if not author:
            title_text = soup.title.get_text(" ", strip=True) if soup.title else ""
            author_re = catalog.get("author_title_regex")
            if author_re:
                match = re.search(author_re, title_text)
                if match:
                    author = (match.groupdict().get("author") or match.group(1)).strip()

        selector = catalog.get("chapter_selector")
        if not selector:
            raise ValueError(f"{self.rule_id} 規則缺少 catalog.chapter_selector")
        chapters = []
        seen = set()
        for link in soup.select(selector):
            href = link.get("href")
            chapter_title = link.get_text(" ", strip=True)
            if not href or not chapter_title:
                continue
            chapter_url = urljoin(self._base_url, href)
            if chapter_url in seen:
                continue
            seen.add(chapter_url)
            chapters.append(Chapter(chapter_title, chapter_url))

        if not chapters:
            raise ValueError(
                catalog.get("empty_error")
                or f"{self.rule_id} 目錄頁找不到章節連結，網站版型可能已改"
            )

        if catalog.get("sort") == "chapter_number":
            numbered = []
            for index, chapter in enumerate(chapters):
                match = _CHAPTER_NUMBER_RE.search(chapter.title)
                number = _chapter_number(match.group(1)) if match else None
                if number is None:
                    numbered = []
                    break
                numbered.append((number, index, chapter))
            if numbered:
                chapters = [chapter for _number, _index, chapter in sorted(numbered)]

        return BookInfo(title=title, author=author, chapters=chapters)

    def catalog_page_urls(self, html: str, url: str) -> list[str]:
        pagination = self.rule.get("pagination")
        if not pagination:
            return []
        selector = pagination.get("option_selector")
        query_param = pagination.get("query_param")
        if not selector or not query_param:
            return []

        soup = BeautifulSoup(html, "lxml")
        current = urlparse(url)
        current_page = next(
            (value for key, value in parse_qsl(current.query)
             if key.lower() == query_param.lower()),
            pagination.get("default_page", "1"),
        )
        found = []
        for option in soup.select(selector):
            page = (option.get("value") or "").strip()
            if not page or page == current_page:
                continue
            if pagination.get("numeric", True) and not page.isdigit():
                continue
            query = [
                (key, value) for key, value in parse_qsl(current.query)
                if key.lower() != query_param.lower()
            ]
            query.append((query_param, page))
            target = current._replace(query=urlencode(query)).geturl()
            if target not in found:
                found.append(target)
        return found

    def parse_chapter(self, html: str, title: str = "") -> str:
        text = super().parse_chapter(html, title)
        patterns = [
            re.compile(pattern)
            for pattern in self.rule.get("content", {}).get("remove_text_regex", ())
        ]
        if not patterns:
            return text
        return "\n\n".join(
            line for line in text.split("\n\n")
            if not any(pattern.fullmatch(line.strip()) for pattern in patterns)
        )


class FanqieLinkRuleAdapter(FanqieAdapter):
    """Resolve a JSON-configured landing page, then use the native reader."""

    adapter_source = "rules"

    def _resolve_url(self, url):
        if url == getattr(self, "_landing_url", None):
            return self._resolved_url
        config = self.rule["url"]
        if not re.search(config["match_regex"], url, re.I):
            return url
        fetcher = Fetcher(headers=self.default_request_headers())
        try:
            html = fetcher.get(url, retries=1)
            self.validate_response(fetcher)
        finally:
            fetcher.session.close()
        soup = BeautifulSoup(html, "lxml")
        targets = set()
        for node in soup.select(config["book_link_selector"]):
            target = urljoin(url, node.get("href", ""))
            parsed = urlparse(target)
            if (parsed.scheme in ("http", "https") and parsed.netloc.lower() in self.domains
                    and re.fullmatch(r"/page/\d+/?", parsed.path)):
                targets.add("https://fanqienovel.com" + parsed.path.rstrip("/"))
        if len(targets) != 1:
            raise FanqieError("番茄關鍵字頁無法確定唯一作品，請貼上作品詳情的 /page/ 書籍網址。")
        self._landing_url = url
        self._resolved_url = targets.pop()
        return self._resolved_url

    def catalog_url(self, url):
        return super().catalog_url(self._resolve_url(url))

    def meta_url(self, url):
        return super().meta_url(self._resolve_url(url))

    def book_id(self, url):
        return super().book_id(self._resolve_url(url))


def load_rule_adapter_classes(rules_dir: Path | None = None) -> list[type]:
    rules_dir = rules_dir or Path(__file__).with_name("rules")
    classes = []
    for path in sorted(rules_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data if isinstance(data, list) else [data]
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            if not rule.get("id") or not rule.get("domains"):
                raise ValueError(f"無效站點規則: {path}")
            class_name = "".join(part.capitalize() for part in rule["id"].split("_")) + "RuleAdapter"
            base = FanqieLinkRuleAdapter if rule.get("base_adapter") == "fanqie" else RuleAdapter
            cls = type(
                class_name,
                (base,),
                {
                    "rule": rule,
                    "rule_id": rule["id"],
                    "domains": list(rule["domains"]),
                    "__module__": __name__,
                },
            )
            classes.append(cls)
    return classes
