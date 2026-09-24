"""Import a human-saved Fanqie reader page into an isolated safe HTML preview."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from soupsieve import SelectorSyntaxError, match as css_selector_matches

from app_paths import write_json

MAX_HTML_BYTES = 12 * 1024 * 1024
MAX_CSS_BYTES = 3 * 1024 * 1024
MAX_FONT_BYTES = 32 * 1024 * 1024
MAX_PREVIEW_BYTES = 48 * 1024 * 1024
SAVED_FONT_HOSTS = {"lf6-awef.bytetos.com", "lf3-awef.bytetos.com"}
GATE_UI_IDS = {"bdturing-verify", "captcha-container", "login-dialog", "paywall"}
GATE_ONLY_TEXT = {"人机验证", "人機驗證", "请先登录", "請先登入", "购买本章", "購買本章"}
FONT_FACE_RE = re.compile(r"@font-face\s*\{([^}]+)\}", re.I | re.S)
CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.S)
URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
FAMILY_RE = re.compile(r'''(?:^|;)\s*font-family\s*:\s*("(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^;}]+)''', re.I)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
CONDITIONAL_CSS_RE = re.compile(
    r"@(?:media|supports|container|layer|document|scope|keyframes|-webkit-keyframes)\b[^{}]*\{", re.I
)


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
    previous_path: Path | None = None


def reading_preview_status(preview_root, book_id: str, item_id: str) -> tuple[bool, str]:
    """Check a published preview and its exact font bytes before enabling it."""
    if not re.fullmatch(r"\d{1,32}", str(book_id)) or not re.fullmatch(r"\d{1,32}", str(item_id)):
        return False, "書籍或章節 ID 無效"
    folder = Path(preview_root) / str(book_id) / str(item_id)
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        return False, "尚未匯入"
    try:
        manifest = json.loads(_read_bounded(manifest_path, 64 * 1024, "預覽索引"))
        digest = manifest.get("font_sha256", "")
        if (manifest.get("book_id") != str(book_id)
                or manifest.get("item_id") != str(item_id)
                or manifest.get("text_restored") is not False
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            return False, "預覽索引與所選章節不符"
        preview_path = folder / "preview.html"
        if not preview_path.is_file():
            return False, "預覽頁缺失"
        preview_bytes = _read_bounded(preview_path, MAX_PREVIEW_BYTES, "預覽頁")
        preview_digest = manifest.get("preview_sha256")
        if preview_digest is not None:
            if (not isinstance(preview_digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", preview_digest)
                    or hashlib.sha256(preview_bytes).hexdigest() != preview_digest):
                return False, "預覽頁內容與索引不符"
        elif not _legacy_preview_structure(preview_bytes, digest):
            return False, "舊版預覽頁內容無法驗證"
        fonts = [folder / "fonts" / f"{digest}{suffix}"
                 for suffix in (".woff2", ".woff", ".ttf", ".otf")]
        available = [path for path in fonts if path.is_file() and not path.is_symlink()]
        if len(available) != 1:
            return False, "字型檔缺失或不唯一"
        font_bytes = _read_bounded(available[0], MAX_FONT_BYTES, "字型檔")
        if not _font_signature(font_bytes, available[0].suffix):
            return False, "字型格式無效"
        if hashlib.sha256(font_bytes).hexdigest() != digest:
            return False, "字型內容與預覽索引不符"
        return True, "字型檔已核對，開啟後仍須確認瀏覽器載入狀態"
    except (ReaderImportError, OSError, ValueError, TypeError, AttributeError):
        return False, "預覽資料無法驗證"


def _legacy_preview_structure(preview_bytes: bytes, font_digest: str) -> bool:
    """Keep older generated previews usable, while rejecting obvious damage."""
    try:
        content = preview_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return (content.lstrip().lower().startswith("<!doctype html>")
            and "<main>" in content and "</main>" in content
            and 'id="font-status"' in content
            and f"fonts/{font_digest}." in content)


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
    return [CSS_COMMENT_RE.sub("", block) for block in blocks]


def _closing_css_brace(block: str, opening: int) -> int:
    depth = 1
    quote = ""
    escaped = False
    for index in range(opening + 1, len(block)):
        char = block[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ReaderImportError("保存的條件式 CSS 不完整，無法核對正文字型。")


def _separate_conditional_css(css_blocks: list[str]) -> tuple[list[str], list[str]]:
    regular, conditional = [], []
    for block in css_blocks:
        parts = []
        position = 0
        for match in CONDITIONAL_CSS_RE.finditer(block):
            if match.start() < position:
                continue
            parts.append(block[position:match.start()])
            closing = _closing_css_brace(block, match.end() - 1)
            conditional.append(block[match.end():closing])
            position = closing + 1
        parts.append(block[position:])
        regular.append("".join(parts))
    return regular, conditional


def _conditional_font_affects_reader(conditional: list[str], reader_node: Tag,
                                     family: str, visibility_rules=()) -> bool:
    nodes = [reader_node, *(node for node in reader_node.parents if isinstance(node, Tag)),
             *(node for node in reader_node.descendants
               if isinstance(node, Tag) and not _is_inert_node(node, visibility_rules))]
    for block in conditional:
        for face in FONT_FACE_RE.findall(block):
            declared = FAMILY_RE.search(face)
            if declared and _first_family(declared) == family:
                return True
        for selector_text, declarations in CSS_RULE_RE.findall(block):
            if (not FONT_ATTRIBUTE_RE.search(declarations)
                    or selector_text.lstrip().startswith("@")):
                continue
            if any(_selector_matches(selector, node)
                   for selector in selector_text.split(",") for node in nodes):
                return True
    return False


def _is_inert_node(node: Tag, visibility_rules=()) -> bool:
    ancestors = [ancestor for ancestor in (node, *node.parents) if isinstance(ancestor, Tag)]
    visibility = "visible"
    for ancestor in reversed(ancestors):
        if (ancestor.name in {"script", "style", "template", "noscript"}
                or ancestor.has_attr("hidden")
                or _css_visibility_value(ancestor, visibility_rules, "display") == "none"):
            return True
        value = _css_visibility_value(ancestor, visibility_rules, "visibility")
        if value in {"visible", "hidden", "collapse"}:
            visibility = value
        elif value == "initial":
            visibility = "visible"
        elif value not in {"", "inherit", "unset"}:
            raise ReaderImportError("無法確認保存頁面的 CSS 可見性，未建立預覽。")
    return visibility != "visible"


def _reader_body(soup: BeautifulSoup, visibility_rules=()):
    selectors = (
        "#reader-content", "#readerContent", ".muye-reader-content",
        "[class*='reader-content']", "[class*='readerContent']",
    )
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            paragraphs = []
            for p in node.find_all("p"):
                if _is_inert_node(p, visibility_rules):
                    if _has_visible_descendant_text(p, visibility_rules):
                        raise ReaderImportError("正文含部分可見的隱藏段落，無法安全建立預覽。")
                    continue
                if p.get_text():
                    paragraphs.append(p)
            if paragraphs:
                return node, paragraphs
    raise ReaderImportError("保存頁面找不到含正文段落的閱讀器區塊，沒有匯入任何資料。")


def _has_visible_descendant_text(node: Tag, visibility_rules=()) -> bool:
    return any(isinstance(child, NavigableString) and str(child).strip()
               and isinstance(child.parent, Tag)
               and not _is_inert_node(child.parent, visibility_rules)
               for child in node.descendants)


def _has_gate_ui(soup: BeautifulSoup, paragraphs: list[str], visibility_rules=()) -> bool:
    """Recognize an actual gate widget or a reader body consisting of a gate notice."""
    for meta in soup.find_all("meta"):
        if (meta.get("name") or "").lower() == "x-vc-bdturing-parameters":
            return True
    for node in soup.find_all(True):
        if _is_inert_node(node, visibility_rules):
            continue
        identifiers = [node.get("id", ""), *(node.get("class") or [])]
        if any(value.lower() in GATE_UI_IDS for value in identifiers if isinstance(value, str)):
            return True
        if node.name == "iframe" and "bdturing-verify" in (node.get("src") or "").lower():
            return True
    stripped = [paragraph.strip() for paragraph in paragraphs if paragraph.strip()]
    return len(stripped) <= 2 and bool(stripped) and all(text in GATE_ONLY_TEXT for text in stripped)


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
    if not identity_urls:
        raise ReaderImportError("無法從 canonical／og:url 確認保存頁面屬於所選章節 itemId。")
    for value in identity_urls:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or parsed.hostname != "fanqienovel.com"
                or parsed.username or parsed.password
                or parsed.path != f"/reader/{item_id}"):
            raise ReaderImportError("canonical／og:url 來源或章節 itemId 與所選章節不符。")

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
    if not selector:
        return False
    parts = selector.split()
    if (any(mark in selector for mark in (":", ">", "+", "~", "["))
            or not all(re.fullmatch(r"(?:[a-zA-Z][\w-]*|\*|#[\w-]+|\.[\w-]+)+", part)
                       for part in parts)):
        try:
            if css_selector_matches(selector, node):
                raise ReaderImportError("正文命中不支援的 CSS 字型選擇器，未建立預覽。")
        except (SelectorSyntaxError, NotImplementedError) as exc:
            # Browser file-upload controls are never reader paragraphs.
            if selector == "::-webkit-file-upload-button":
                return False
            raise ReaderImportError("無法確認 CSS 字型選擇器是否套用正文，未建立預覽。") from exc
        return False

    def matches(part, candidate):
        if not re.fullmatch(r"(?:[a-zA-Z][\w-]*|\*|#[\w-]+|\.[\w-]+)+", part):
            return False
        tag = re.match(r"^[a-zA-Z][\w-]*", part)
        if tag and candidate.name != tag.group(0).lower():
            return False
        ids = re.findall(r"#([\w-]+)", part)
        if ids and candidate.get("id") not in ids:
            return False
        classes = re.findall(r"\.([\w-]+)", part)
        return all(name in (candidate.get("class") or []) for name in classes)

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


def _font_family_value(value: str) -> str:
    value = value.strip()
    if value.lower() in {"inherit", "unset", "initial", "revert", "revert-layer"}:
        return value.lower()
    if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
        return re.sub(r"\\([\\'\"])", r"\1", value[1:-1]).strip()
    return re.sub(r"\s*!important\s*$", "", value.split(",", 1)[0], flags=re.I).strip().strip("'\"")


def _first_family(match) -> str:
    return _font_family_value(match.group(1))


FONT_ATTRIBUTE_RE = re.compile(r"(?:^|;)\s*(font-family|font-weight|font-style|font)\s*:\s*([^;]+)", re.I)
VISIBILITY_RE = re.compile(r"(?:^|;)\s*(display|visibility)\s*:\s*([^;]+)", re.I)
FONT_SIZE_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:px|pt|em|rem|%)|xx-small|x-small|small|medium|large|x-large|xx-large)(?:/\S+)?$",
    re.I,
)


def _font_shorthand_attribute(value: str, name: str) -> str:
    if value.lower() in {"inherit", "unset", "initial", "revert", "revert-layer"}:
        return value.lower()
    tokens = value.split()
    size_index = next((index for index, token in enumerate(tokens) if FONT_SIZE_RE.fullmatch(token)), None)
    if size_index is None or size_index == len(tokens) - 1:
        raise ReaderImportError("正文使用無法安全解析的 font 簡寫，未建立字型閱讀預覽。")
    style, weight = "normal", "normal"
    for token in tokens[:size_index]:
        lowered = token.lower()
        if lowered in {"italic", "oblique"}:
            style = lowered
        elif lowered in {"bold", "bolder", "lighter"} or re.fullmatch(r"[1-9]00", lowered):
            weight = lowered
        elif lowered not in {"normal", "small-caps"}:
            raise ReaderImportError("正文使用無法安全解析的 font 簡寫，未建立字型閱讀預覽。")
    if name == "font-family":
        return _font_family_value(" ".join(tokens[size_index + 1:]))
    return weight if name == "font-weight" else style


def _font_attribute_declarations(declarations: str, name: str) -> list[tuple[str, str, bool]]:
    found = []
    for match in FONT_ATTRIBUTE_RE.finditer(declarations):
        property_name, raw_value = match.group(1).lower(), match.group(2).strip()
        important = bool(re.search(r"!important\s*$", raw_value, re.I))
        value = re.sub(r"\s*!important\s*$", "", raw_value, flags=re.I).strip()
        if property_name in {name, "font"}:
            found.append((property_name, value, important))
    return found


def _font_attribute_rules(css_blocks: list[str], name: str) -> list[tuple[list[str], str, str, bool, int]]:
    rules = []
    for block in css_blocks:
        for selector_text, declarations in CSS_RULE_RE.findall(block):
            if selector_text.lstrip().startswith("@"):
                continue
            selectors = selector_text.split(",")
            for property_name, value, important in _font_attribute_declarations(declarations, name):
                rules.append((selectors, property_name, value, important, len(rules)))
    return rules


def _selector_specificity(selector: str) -> tuple[int, int, int, int]:
    parts = selector.strip().split()
    return (0, selector.count("#"), selector.count("."),
            sum(bool(re.match(r"^[A-Za-z][\w-]*", part)) for part in parts))


class _VisibilityRules(list):
    def __init__(self):
        super().__init__()
        self.cache = {}


def _visibility_rules(css_blocks: list[str]) -> _VisibilityRules:
    rules = _VisibilityRules()
    for block in css_blocks:
        for selector_text, declarations in CSS_RULE_RE.findall(block):
            if selector_text.lstrip().startswith("@"):
                continue
            for match in VISIBILITY_RE.finditer(declarations):
                raw = match.group(2).strip()
                important = bool(re.search(r"!important\s*$", raw, re.I))
                value = re.sub(r"\s*!important\s*$", "", raw, flags=re.I).strip().lower()
                rules.append((selector_text.split(","), match.group(1).lower(),
                              value, important, len(rules)))
    return rules


def _css_visibility_value(node: Tag, rules, name: str) -> str:
    key = (id(node), name)
    if isinstance(rules, _VisibilityRules) and key in rules.cache:
        return rules.cache[key]
    candidates = []
    for selectors, property_name, value, important, order in rules:
        if property_name != name:
            continue
        specificity = max((_selector_specificity(selector) for selector in selectors
                           if "::" not in selector and not re.search(
                               r"(?<!:):(?:before|after|first-line|first-letter)\b", selector, re.I)
                           if _selector_matches(selector, node)), default=None)
        if specificity is not None:
            candidates.append(((int(important), specificity, order), value))
    for order, match in enumerate(VISIBILITY_RE.finditer(node.get("style", ""))):
        if match.group(1).lower() != name:
            continue
        raw = match.group(2).strip()
        important = bool(re.search(r"!important\s*$", raw, re.I))
        value = re.sub(r"\s*!important\s*$", "", raw, flags=re.I).strip().lower()
        candidates.append(((int(important), (1, 0, 0, 0), len(rules) + order), value))
    result = max(candidates, key=lambda candidate: candidate[0])[1] if candidates else ""
    if isinstance(rules, _VisibilityRules):
        rules.cache[key] = result
    return result


def _declared_font_attribute(node, rules: list[tuple[list[str], str, str, bool, int]], name: str) -> str:
    candidates = []
    for selectors, property_name, value, important, order in rules:
        specificity = max((_selector_specificity(selector) for selector in selectors
                           if _selector_matches(selector, node)), default=None)
        if specificity is not None:
            candidates.append(((int(important), specificity, order), property_name, value))
    for order, (property_name, value, important) in enumerate(
            _font_attribute_declarations(CSS_COMMENT_RE.sub("", node.get("style", "")), name)):
        candidates.append(((int(important), (1, 0, 0, 0), len(rules) + order), property_name, value))
    if not candidates:
        return ""
    _priority, property_name, value = max(candidates, key=lambda candidate: candidate[0])
    if property_name == "font":
        return _font_shorthand_attribute(value, name)
    return _font_family_value(value) if name == "font-family" else value.lower()


def _effective_font_attribute(node, rules: list[tuple[list[str], str, str, bool, int]], name: str,
                              default: str) -> str:
    current = node
    while isinstance(current, Tag):
        value = _declared_font_attribute(current, rules, name)
        if value in {"revert", "revert-layer"}:
            raise ReaderImportError("正文使用無法安全解析的字型重設，未建立預覽。")
        if value and value not in {"inherit", "unset"}:
            return default if value == "initial" else value
        current = current.parent
    return default


def _font_weight(value: str) -> int:
    if value == "normal":
        return 400
    if value == "bold":
        return 700
    if re.fullmatch(r"[1-9]00", value):
        return int(value)
    raise ReaderImportError("無法確認正文實際字重，未建立字型閱讀預覽。")


def _font_style(value: str) -> str:
    if value in {"normal", "italic", "oblique"}:
        return value
    raise ReaderImportError("無法確認正文實際字型樣式，未建立字型閱讀預覽。")


def _face_matches(face: str, weight: int, style: str) -> bool:
    declared_weight = re.search(r"(?:^|;)\s*font-weight\s*:\s*([^;}]+)", face, re.I)
    weight_value = declared_weight.group(1).strip().lower() if declared_weight else "normal"
    parts = weight_value.split()
    if len(parts) == 2 and all(re.fullmatch(r"[1-9]00", part) for part in parts):
        weight_matches = int(parts[0]) <= weight <= int(parts[1])
    else:
        weight_matches = _font_weight(weight_value) == weight
    declared_style = re.search(r"(?:^|;)\s*font-style\s*:\s*([^;}]+)", face, re.I)
    style_value = declared_style.group(1).strip().lower() if declared_style else "normal"
    return weight_matches and _font_style(style_value) == style


def _font_resource(css_blocks: list[str], family: str, weight: int, style: str,
                   html_path: Path) -> tuple[bytes, str]:
    root = html_path.parent
    remote_font_missing = False
    faces = [face for block in css_blocks for face in FONT_FACE_RE.findall(block)
             if (declared := FAMILY_RE.search(face)) and _first_family(declared) == family]

    matching_faces = [face for face in faces if _face_matches(face, weight, style)]
    if faces and not matching_faces:
        raise ReaderImportError("找不到與正文實際字重／樣式相符的字型檔，未建立預覽。")
    if any(re.search(r"(?:^|;)\s*unicode-range\s*:", face, re.I)
           for face in matching_faces):
        raise ReaderImportError("正文字型使用 unicode-range 分割字型，無法安全建立單一字型預覽。")
    for face in matching_faces:
        for src in URL_RE.finditer(face):
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
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    raise ReaderImportError("字型來源網址不是有效的 HTTPS 資源。")
                if parsed.hostname.lower() not in SAVED_FONT_HOSTS or not parsed.path.startswith("/obj/awesome-font/c/"):
                    raise ReaderImportError("此字型仍是遠端資源，來源不是已核對的番茄字型資源；原始資料未更動。")
                name = unquote(Path(parsed.path).name)
                if not re.fullmatch(r"[a-f0-9]{12,64}(?:-(?:500|700))?\.(?:woff2?|ttf|otf)", name, re.I):
                    raise ReaderImportError("字型來源檔名無效。")
                local = Path(f"{html_path.stem}_files") / name
                if not (root / local).exists():
                    remote_font_missing = True
                    continue
                path = _safe_source_file(local, root)
            else:
                path = _safe_source_file(Path(uri.split("?", 1)[0].split("#", 1)[0]), root)
            _require_saved_asset(path, html_path)
            suffix = path.suffix.lower()
            if suffix not in {".woff", ".woff2", ".ttf", ".otf"}:
                raise ReaderImportError("字型檔案格式不受支援。")
            data = _read_bounded(path, MAX_FONT_BYTES, "字型檔")
            if not _font_signature(data, suffix):
                raise ReaderImportError("字型檔案內容與副檔名不符。")
            return data, suffix
    if remote_font_missing:
        raise ReaderImportError("此字型仍是遠端資源，保存的 _files 資料夾沒有同名字型檔；原始資料未更動。")
    raise ReaderImportError("找不到正文實際使用字型的本機字型檔；原始資料未更動。")


def _paragraph_text(node, visibility_rules=()) -> list[str]:
    paragraphs = []

    def append_text(source, output):
        for child in source.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                output.append(str(child))
            elif isinstance(child, Tag):
                if child.name in {"script", "style", "iframe", "object"}:
                    continue
                if _is_inert_node(child, visibility_rules):
                    if _has_visible_descendant_text(child, visibility_rules):
                        raise ReaderImportError("正文含部分可見的隱藏文字，無法安全建立預覽。")
                    continue
                if child.name == "br":
                    output.append("\n")
                else:
                    append_text(child, output)

    for p in node.find_all("p"):
        if _is_inert_node(p, visibility_rules):
            continue
        pieces = []
        append_text(p, pieces)
        text = "".join(pieces)
        if text.strip():
            paragraphs.append(text)
    return paragraphs


def _preview_document(title: str, paragraphs: list[str], family: str, relative_font: str,
                      item_id: str, next_item_id: str | None = None,
                      weight: int = 400, style: str = "normal") -> str:
    safe_family = json.dumps(family, ensure_ascii=True)
    safe_font = html.escape(relative_font, quote=True)
    body = "\n".join(f"<p>{html.escape(text, quote=True).replace(chr(10), '<br>')}</p>" for text in paragraphs)
    original = f"https://fanqienovel.com/reader/{item_id}"
    font_spec = json.dumps(f"{style} {weight} 18px ")
    next_link = (f'<a href="https://fanqienovel.com/reader/{next_item_id}" '
                 'rel="noopener noreferrer">下一章（原站）</a>' if next_item_id else "")
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{html.escape(title)}</title><style>
@font-face{{font-family:{safe_family};font-weight:{weight};font-style:{style};src:url('{safe_font}') format('{Path(relative_font).suffix[1:]}');font-display:block}}
body{{max-width:44rem;margin:3rem auto;padding:0 1.2rem;background:#f6f1e8;color:#29241e;font:16px/1.6 system-ui,sans-serif}}
main{{font:{style} {weight} 18px/2 {safe_family},serif}}
p{{margin:0 0 1.2em;white-space:pre-wrap}} #font-status{{font:14px/1.5 sans-serif;color:#745}}
</style></head><body><h1>{html.escape(title)}</h1>
<p id="font-status" role="status">正在確認章節字型載入…</p><main>{body}</main>
<script>document.fonts.load({font_spec} + {safe_family}).then(function(f){{
 const e=document.getElementById('font-status');
 if(f.length && document.fonts.check({font_spec} + {safe_family})){{e.textContent='字型已載入：' + {safe_family};e.style.color='#27632a';}}
 else{{e.textContent='字型載入失敗，視覺內容不可靠，請回原站閱讀。';}}
}}).catch(function(){{document.getElementById('font-status').textContent='字型載入失敗，請檢查保存的資源並回原站閱讀。';}});</script>
<footer>文字尚未還原。這是字型視覺預覽；複製、搜尋、朗讀及一般文字匯出可能不正確。</footer>
<nav aria-label="章節導覽"><p>本機只保存這一章。<a href="{original}" rel="noopener noreferrer">回原站閱讀此章</a>
{next_link}</p></nav>
</body></html>"""


def _make_import_stage(parent: Path, item_id: str) -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix=f".{item_id}-import-", dir=parent))
    # Inherit the private user-data directory ACL. Python's mode 0700 can
    # exclude the Windows sandbox token from the directory it just created.
    for _ in range(8):
        stage = parent / f".{item_id}-import-{secrets.token_hex(8)}"
        try:
            stage.mkdir()
            return stage
        except FileExistsError:
            continue
    raise ReaderImportError("無法建立不重複的匯入暫存資料夾。")


def _reader_sidecars(paragraphs: list[str], source_bytes: bytes, book_id: str,
                     item_id: str, family: str, font_digest: str,
                     weight: int, style: str) -> tuple[dict, dict]:
    raw_text = {"paragraphs": paragraphs}
    canonical = json.dumps(raw_text, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    metadata = {
        "book_id": book_id,
        "item_id": item_id,
        "source_kind": "browser_saved_html",
        "source_url": f"https://fanqienovel.com/reader/{item_id}",
        "source_html_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "raw_text_sha256": hashlib.sha256(canonical).hexdigest(),
        "font_family": family,
        "font_weight": weight,
        "font_style": style,
        "font_sha256": font_digest,
        "used_codepoints": sorted({ord(char) for paragraph in paragraphs for char in paragraph}),
        "used_pua_codepoints": sorted({ord(char) for paragraph in paragraphs for char in paragraph
                                       if 0xE000 <= ord(char) <= 0xF8FF}),
        "decoder_version": None,
        "text_restored": False,
    }
    return raw_text, metadata


def import_reader_html(source_path, book_id: str, item_id: str, title: str, preview_root,
                       next_item_id: str | None = None) -> ReaderPreview:
    """Import a user-saved reader page and atomically create a safe isolated preview."""
    if not re.fullmatch(r"\d{1,32}", str(book_id)) or not re.fullmatch(r"\d{1,32}", str(item_id)):
        raise ReaderImportError("書籍 ID 與章節 itemId 必須是目錄中的數字 ID。")
    if next_item_id is not None and not re.fullmatch(r"\d{1,32}", str(next_item_id)):
        raise ReaderImportError("下一章 itemId 必須是目錄中的數字 ID。")
    source_path = Path(source_path).resolve(strict=True)
    if source_path.suffix.lower() not in {".html", ".htm"}:
        raise ReaderImportError("請選取瀏覽器保存的 .html／.htm 閱讀頁。")
    raw = _read_bounded(source_path, MAX_HTML_BYTES, "HTML")
    # Fanqie saved reader pages are UTF-8. Do not let an isolated short gate
    # notice be misidentified as another encoding by byte-level heuristics.
    soup = BeautifulSoup(raw, "html.parser", from_encoding="utf-8")
    _validate_identity(soup, str(book_id), str(item_id))
    css, conditional_css = _separate_conditional_css(_extract_css(soup, source_path))
    visibility_rules = _visibility_rules(css)
    content_node, paragraph_nodes = _reader_body(soup, visibility_rules)
    paragraphs = _paragraph_text(content_node, visibility_rules)
    if not paragraphs:
        raise ReaderImportError("閱讀器區塊沒有可保存的正文段落。")
    if _has_gate_ui(soup, paragraphs, visibility_rules):
        raise ReaderImportError("保存頁面含登入、購買或人機驗證要求，未將其當作正文保存。")
    family_rules = _font_attribute_rules(css, "font-family")
    family = _effective_font_attribute(content_node, family_rules, "font-family", "")
    paragraph_families = {
        _effective_font_attribute(p, family_rules, "font-family", "")
        for p in paragraph_nodes
    }
    if "" in paragraph_families:
        raise ReaderImportError("無法確認正文實際使用的字型：章節段落有未確認的正文字型，無法建立預覽。")
    if family and paragraph_families - {family}:
        raise ReaderImportError("章節段落使用不同字型，無法安全套用單一字型預覽。")
    if not family and len(paragraph_families) == 1:
        family = next(iter(paragraph_families))
    elif not family and len(paragraph_families) > 1:
        raise ReaderImportError("章節段落使用不同字型，無法安全套用單一字型預覽。")
    if not family:
        raise ReaderImportError("無法確認正文實際使用的字型，無法宣稱字型閱讀預覽可用。")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", family):
        raise ReaderImportError("正文字型名稱含不安全字元，無法建立預覽。")
    if _conditional_font_affects_reader(conditional_css, content_node, family, visibility_rules):
        raise ReaderImportError("正文或其字型有條件式 CSS 規則，無法安全確認實際字型。")
    weight_rules = _font_attribute_rules(css, "font-weight")
    style_rules = _font_attribute_rules(css, "font-style")
    paragraph_weights = {_font_weight(_effective_font_attribute(p, weight_rules, "font-weight", "normal"))
                         for p in paragraph_nodes}
    paragraph_styles = {_font_style(_effective_font_attribute(p, style_rules, "font-style", "normal"))
                        for p in paragraph_nodes}
    if len(paragraph_weights) != 1 or len(paragraph_styles) != 1:
        raise ReaderImportError("章節段落使用不同字重／樣式，無法安全套用單一字型預覽。")
    weight = next(iter(paragraph_weights))
    style = next(iter(paragraph_styles))
    for paragraph in paragraph_nodes:
        for descendant in paragraph.descendants:
            if (not isinstance(descendant, Tag) or _is_inert_node(descendant, visibility_rules)
                    or not any(isinstance(child, NavigableString) and str(child).strip()
                               for child in descendant.children)):
                continue
            if (_effective_font_attribute(descendant, family_rules, "font-family", "") != family
                    or _font_weight(_effective_font_attribute(
                        descendant, weight_rules, "font-weight", "normal")) != weight
                    or _font_style(_effective_font_attribute(
                        descendant, style_rules, "font-style", "normal")) != style):
                raise ReaderImportError("章節段落內使用不同字型、字重或樣式，無法安全建立單一字型預覽。")
    font_bytes, suffix = _font_resource(css, family, weight, style, source_path)
    digest = hashlib.sha256(font_bytes).hexdigest()
    root = Path(preview_root) / str(book_id) / str(item_id)
    if root.is_symlink():
        raise ReaderImportError("章節資料夾是符號連結，不能安全重新匯入。")
    if root.exists() and reading_preview_status(preview_root, str(book_id), str(item_id))[0]:
        raise ReaderImportError("此書籍／章節已有匯入資料；為保留原始資料，本次不覆寫。")
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = _make_import_stage(root.parent, str(item_id))
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
        raw_text, source_metadata = _reader_sidecars(
            paragraphs, raw, str(book_id), str(item_id), family, digest, weight, style
        )
        write_json(stage / "raw_text.json", raw_text)
        write_json(stage / "source_metadata.json", source_metadata)
        preview_path = stage / "preview.html"
        document = _preview_document(title, paragraphs, family, f"fonts/{font_name}",
                                     str(item_id), str(next_item_id) if next_item_id else None,
                                     weight, style)
        preview_bytes = document.encode("utf-8")
        if len(preview_bytes) > MAX_PREVIEW_BYTES:
            raise ReaderImportError("產生的預覽頁超過允許大小，原始資料未更動。")
        temp_preview = stage / "preview.html.tmp"
        with temp_preview.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_preview, preview_path)
        write_json(stage / "manifest.json", {
            "book_id": str(book_id), "item_id": str(item_id), "title": title,
            "font_family": family, "font_sha256": digest,
            "font_weight": weight, "font_style": style,
            "preview_sha256": hashlib.sha256(preview_bytes).hexdigest(),
            "paragraph_count": len(paragraphs), "text_restored": False,
        })
        # Keep a broken earlier import intact under a unique sibling name.
        previous_path = None
        if root.exists():
            if root.is_symlink() or reading_preview_status(preview_root, str(book_id), str(item_id))[0]:
                raise ReaderImportError("此書籍／章節已有可用資料，本次不覆寫。")
            previous_path = root.parent / f"{item_id}-previous-{secrets.token_hex(16)}"
            os.rename(root, previous_path)
        try:
            # Publish the complete chapter folder in one same-volume rename.
            os.rename(stage, root)
        except Exception:
            if previous_path is not None:
                try:
                    os.rename(previous_path, root)
                except OSError as rollback_exc:
                    raise ReaderImportError(f"修復未完成；舊資料保留在 {previous_path}") from rollback_exc
            raise
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return ReaderPreview(str(book_id), str(item_id), title, root / "preview.html", family, digest,
                         len(paragraphs), previous_path)
