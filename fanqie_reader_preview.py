"""Import a human-saved Fanqie reader page into an isolated safe HTML preview."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app_paths import write_json

MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_CSS_BYTES = 3 * 1024 * 1024
MAX_FONT_BYTES = 32 * 1024 * 1024
READER_ID_RE = re.compile(r"/reader/(\d+)")
FONT_FACE_RE = re.compile(r"@font-face\s*\{([^}]+)\}", re.I | re.S)
CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
FAMILY_RE = re.compile(r"font-family\s*:\s*(['\"]?)([^;'\"]+)\1", re.I)


class ReaderImportError(ValueError):
    """The saved reader page cannot safely provide a matching font preview."""


@dataclass(frozen=True)
class ReaderPreview:
    book_id: str
    item_id: str
    title: str
    preview_path: Path
    font_family: str
    font_sha256: str
    paragraph_count: int


def _safe_source_file(value: Path, root: Path) -> Path:
    """Resolve a saved asset under the HTML directory, refusing path escapes."""
    parsed = urlparse(unquote(str(value)))
    if parsed.scheme or parsed.netloc or str(value).startswith(("/", "\\")):
        raise ReaderImportError("字型或 CSS 必須是瀏覽器保存的本機資源。")
    try:
        candidate = (root / unquote(parsed.path.replace("\\", "/"))).resolve(strict=True)
    except OSError as exc:
        raise ReaderImportError("找不到瀏覽器保存的 CSS 或字型資源。") from exc
    base = root.resolve(strict=True)
    if candidate == base or not candidate.is_relative_to(base) or not candidate.is_file():
        raise ReaderImportError("HTML 資源路徑超出保存資料夾。")
    return candidate


def _read_bounded(path: Path, limit: int, description: str) -> bytes:
    try:
        size = path.stat().st_size
        if size <= 0 or size > limit:
            raise ReaderImportError(f"{description}大小不在允許範圍內。")
        data = path.read_bytes()
        if len(data) > limit:
            raise ReaderImportError(f"{description}讀取後超過允許大小。")
        return data
    except OSError as exc:
        raise ReaderImportError(f"無法讀取{description}：{exc}") from exc


def _font_signature(data: bytes, suffix: str) -> bool:
    signatures = {
        ".woff": data.startswith(b"wOFF"),
        ".woff2": data.startswith(b"wOF2"),
        ".ttf": data.startswith(b"\x00\x01\x00\x00"),
        ".otf": data.startswith(b"OTTO"),
    }
    return signatures.get(suffix.lower(), False)


def _extract_css(soup: BeautifulSoup, html_path: Path) -> list[str]:
    blocks = [tag.string or tag.get_text() for tag in soup.find_all("style")]
    if sum(len(block.encode("utf-8")) for block in blocks) > MAX_CSS_BYTES:
        raise ReaderImportError("HTML 內嵌 CSS 超過允許大小。")
    root = html_path.parent
    total_external_bytes = 0
    for link in soup.find_all("link", rel=lambda value: value and "stylesheet" in value):
        href = (link.get("href") or "").strip()
        if not href or urlparse(href).scheme or href.startswith(("//", "data:")):
            continue
        try:
            css_path = _safe_source_file(Path(href.split("?", 1)[0].split("#", 1)[0]), root)
            _require_saved_asset(css_path, html_path)
            css_bytes = _read_bounded(css_path, MAX_CSS_BYTES, "CSS")
            total_external_bytes += len(css_bytes)
            if total_external_bytes > MAX_CSS_BYTES:
                raise ReaderImportError("保存的 CSS 總大小超過允許限制。")
            stylesheet = css_bytes.decode("utf-8", errors="replace")
            prefix = Path(os.path.relpath(css_path.parent, root)).as_posix()
            if prefix != ".":
                stylesheet = URL_RE.sub(
                    lambda match: match.group(0) if urlparse(match.group(2)).scheme
                    or match.group(2).startswith(("//", "/", "data:"))
                    else f"url('{prefix}/{match.group(2)}')",
                    stylesheet,
                )
            blocks.append(stylesheet)
        except FileNotFoundError:
            continue
    return blocks


def _reader_body(soup: BeautifulSoup):
    selectors = (
        "#reader-content", "#readerContent", ".muye-reader-content",
        "[class*='reader-content']", "[class*='readerContent']",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            paragraphs = [p for p in node.find_all("p") if p.get_text()]
            if paragraphs:
                return node, paragraphs
    raise ReaderImportError("保存頁面找不到含正文段落的閱讀器區塊，沒有匯入任何資料。")


def _require_saved_asset(path: Path, html_path: Path) -> None:
    resource_dir = html_path.parent / f"{html_path.stem}_files"
    if resource_dir.is_symlink():
        raise ReaderImportError("HTML 對應的瀏覽器保存資源資料夾不能是符號連結。")
    try:
        path.relative_to(resource_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ReaderImportError("CSS／字型必須位於此 HTML 對應的瀏覽器保存資源資料夾。") from exc


def _validate_identity(soup: BeautifulSoup, book_id: str, item_id: str) -> None:
    identity_urls = []
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        identity_urls.append(canonical["href"])
    og_url = soup.find("meta", property="og:url")
    if og_url and og_url.get("content"):
        identity_urls.append(og_url["content"])
    reader_ids = {found for value in identity_urls for found in READER_ID_RE.findall(value)}
    if item_id not in reader_ids:
        raise ReaderImportError("無法從 canonical／og:url 確認保存頁面屬於所選章節 itemId。")

    book_ids = set()
    for tag in soup.find_all("meta"):
        name = (tag.get("name") or tag.get("property") or "").lower()
        if name in {"book_id", "bookid", "novel_id", "novelid"}:
            value = tag.get("content", "")
            if str(value).isdigit():
                book_ids.add(str(value))
    for tag in soup.find_all(attrs={"data-book-id": True}):
        value = tag.get("data-book-id", "")
        if str(value).isdigit():
            book_ids.add(str(value))
    if book_ids and book_ids != {book_id}:
        raise ReaderImportError("保存頁面的書籍 ID 與目前選取的書籍不符。")


def _selector_matches(selector: str, node) -> bool:
    selector = selector.strip()
    if not selector or any(mark in selector for mark in (":", ">", "+", "~", "[")):
        return False

    def matches(part, candidate):
        tag = re.match(r"^[a-zA-Z][\w-]*", part)
        if tag and candidate.name != tag.group(0).lower():
            return False
        ids = re.findall(r"#([\w-]+)", part)
        if ids and candidate.get("id") not in ids:
            return False
        classes = re.findall(r"\.([\w-]+)", part)
        return all(name in (candidate.get("class") or []) for name in classes)

    parts = selector.split()
    current = node
    if not matches(parts[-1], current):
        return False
    for part in reversed(parts[:-1]):
        current = current.parent
        while current and isinstance(current, Tag) and not matches(part, current):
            current = current.parent
        if not isinstance(current, Tag):
            return False
    return True


def _declared_font(node, css_blocks: list[str]) -> str:
    inline = node.get("style", "")
    inline_match = FAMILY_RE.search(inline)
    if inline_match:
        return inline_match.group(2).strip()
    selected = ""
    for block in css_blocks:
        for selector_text, declarations in CSS_RULE_RE.findall(block):
            if any(_selector_matches(selector, node) for selector in selector_text.split(",")):
                family = FAMILY_RE.search(declarations)
                if family:
                    selected = family.group(2).strip()
    return selected


def _font_resource(css_blocks: list[str], family: str, html_path: Path) -> tuple[bytes, str]:
    root = html_path.parent
    for block in css_blocks:
        for face in FONT_FACE_RE.findall(block):
            declared = FAMILY_RE.search(face)
            if not declared or declared.group(2).strip().strip("\"") != family:
                continue
            src = URL_RE.search(face)
            if not src:
                continue
            uri = src.group(2).strip()
            if uri.lower().startswith("data:"):
                header, separator, payload = uri.partition(",")
                if not separator or ";base64" not in header.lower():
                    raise ReaderImportError("內嵌字型格式不受支援。")
                media_type = header[5:].split(";", 1)[0].lower()
                suffix = {
                    "font/woff": ".woff", "application/font-woff": ".woff",
                    "font/woff2": ".woff2", "application/font-woff2": ".woff2",
                    "font/ttf": ".ttf", "application/x-font-ttf": ".ttf",
                    "font/otf": ".otf", "application/x-font-opentype": ".otf",
                }.get(media_type, "")
                try:
                    data = base64.b64decode(payload, validate=True)
                except (ValueError, base64.binascii.Error) as exc:
                    raise ReaderImportError("內嵌字型資料無效。") from exc
                if not data or len(data) > MAX_FONT_BYTES or not _font_signature(data, suffix):
                    raise ReaderImportError("內嵌字型大小或檔案格式不符。")
                return data, suffix
            parsed = urlparse(uri)
            if parsed.scheme or parsed.netloc or uri.startswith("//"):
                raise ReaderImportError("此字型仍是遠端資源；請在正常瀏覽器中保存頁面及資源後再匯入。")
            path = _safe_source_file(Path(uri.split("?", 1)[0].split("#", 1)[0]), root)
            _require_saved_asset(path, html_path)
            suffix = path.suffix.lower()
            if suffix not in {".woff", ".woff2", ".ttf", ".otf"}:
                raise ReaderImportError("字型檔案格式不受支援。")
            data = _read_bounded(path, MAX_FONT_BYTES, "字型檔")
            if not _font_signature(data, suffix):
                raise ReaderImportError("字型檔案內容與副檔名不符。")
            return data, suffix
    raise ReaderImportError("找不到正文實際使用字型的本機字型檔；原始資料未更動。")


def _paragraph_text(node) -> list[str]:
    paragraphs = []

    def append_text(source, output):
        for child in source.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                output.append(str(child))
            elif isinstance(child, Tag):
                if child.name in {"script", "style", "iframe", "object", "template"}:
                    continue
                if child.name == "br":
                    output.append("\n")
                else:
                    append_text(child, output)

    for p in node.find_all("p"):
        pieces = []
        append_text(p, pieces)
        text = "".join(pieces)
        if text.strip():
            paragraphs.append(text)
    return paragraphs


def _preview_document(title: str, paragraphs: list[str], family: str, relative_font: str) -> str:
    safe_family = json.dumps(family, ensure_ascii=True)
    safe_font = html.escape(relative_font, quote=True)
    body = "\n".join(f"<p>{html.escape(text, quote=True).replace(chr(10), '<br>')}</p>" for text in paragraphs)
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)}</title><style>
@font-face{{font-family:{safe_family};src:url('{safe_font}') format('{Path(relative_font).suffix[1:]}');font-display:block}}
body{{max-width:44rem;margin:3rem auto;padding:0 1.2rem;background:#f6f1e8;color:#29241e;font:16px/1.6 system-ui,sans-serif}}
main{{font:18px/2 {safe_family},serif}}
p{{margin:0 0 1.2em;white-space:pre-wrap}} #font-status{{font:14px/1.5 sans-serif;color:#745}}
</style></head><body><h1>{html.escape(title)}</h1>
<p id="font-status" role="status">正在確認章節字型載入…</p><main>{body}</main>
<script>document.fonts.load('18px ' + {safe_family}).then(function(f){{
 const e=document.getElementById('font-status');
 if(f.length && document.fonts.check('18px ' + {safe_family})){{e.textContent='字型已載入：' + {safe_family};e.style.color='#27632a';}}
 else{{e.textContent='字型載入失敗，視覺內容不可靠，請回原站閱讀。';}}
}}).catch(function(){{document.getElementById('font-status').textContent='字型載入失敗，請檢查保存的資源並回原站閱讀。';}});</script>
<footer>文字尚未還原。這是字型視覺預覽；複製、搜尋、朗讀及一般文字匯出可能不正確。</footer>
</body></html>"""


def import_reader_html(source_path, book_id: str, item_id: str, title: str, preview_root) -> ReaderPreview:
    """Import a user-saved reader page and atomically create a safe isolated preview."""
    if not re.fullmatch(r"\d{1,32}", str(book_id)) or not re.fullmatch(r"\d{1,32}", str(item_id)):
        raise ReaderImportError("書籍 ID 與章節 itemId 必須是目錄中的數字 ID。")
    source_path = Path(source_path).resolve(strict=True)
    if source_path.suffix.lower() not in {".html", ".htm"}:
        raise ReaderImportError("請選取瀏覽器保存的 .html／.htm 閱讀頁。")
    raw = _read_bounded(source_path, MAX_HTML_BYTES, "HTML")
    soup = BeautifulSoup(raw, "html.parser")
    _validate_identity(soup, str(book_id), str(item_id))
    content_node, _nodes = _reader_body(soup)
    paragraphs = _paragraph_text(content_node)
    if not paragraphs:
        raise ReaderImportError("閱讀器區塊沒有可保存的正文段落。")
    css = _extract_css(soup, source_path)
    family = _declared_font(content_node, css)
    paragraph_families = {_declared_font(p, css) for p in content_node.find_all("p")}
    paragraph_families.discard("")
    if family and paragraph_families - {family}:
        raise ReaderImportError("章節段落使用不同字型，無法安全套用單一字型預覽。")
    if not family and len(paragraph_families) == 1:
        family = next(iter(paragraph_families))
    elif not family and len(paragraph_families) > 1:
        raise ReaderImportError("章節段落使用不同字型，無法安全套用單一字型預覽。")
    if not family:
        raise ReaderImportError("無法確認正文實際使用的字型，無法宣稱字型閱讀預覽可用。")
    font_bytes, suffix = _font_resource(css, family, source_path)
    digest = hashlib.sha256(font_bytes).hexdigest()
    root = Path(preview_root) / str(book_id) / str(item_id)
    if root.exists():
        raise ReaderImportError("此書籍／章節已有匯入資料；為保留原始資料，本次不覆寫。")
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{item_id}-import-", dir=root.parent))
    fonts = stage / "fonts"
    fonts.mkdir()
    font_name = digest + suffix
    font_path = fonts / font_name
    temp_font = fonts / (font_name + ".tmp")
    try:
        with temp_font.open("xb") as stream:
            stream.write(font_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_font, font_path)
        # Keep a byte-for-byte copy for later diagnosis; never execute/open it.
        shutil.copyfile(source_path, stage / "source.html")
        preview_path = stage / "preview.html"
        document = _preview_document(title, paragraphs, family, f"fonts/{font_name}")
        temp_preview = stage / "preview.html.tmp"
        with temp_preview.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_preview, preview_path)
        write_json(stage / "manifest.json", {
            "book_id": str(book_id), "item_id": str(item_id), "title": title,
            "font_family": family, "font_sha256": digest,
            "paragraph_count": len(paragraphs), "text_restored": False,
        })
        # Publish the complete chapter folder in one same-volume rename.
        os.rename(stage, root)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return ReaderPreview(str(book_id), str(item_id), title, root / "preview.html", family, digest,
                         len(paragraphs))
