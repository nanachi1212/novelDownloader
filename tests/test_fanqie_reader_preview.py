import base64
import hashlib
import json
import os

import pytest

from fanqie_reader_preview import ReaderImportError, import_reader_html, reading_preview_status


FONT_BYTES = b"wOF2" + b"synthetic-font-data"


def _saved_reader(tmp_path, *, item_id="101", book_id="999", font=True, family="MappedFont"):
    page = tmp_path / "chapter.html"
    assets = tmp_path / "chapter_files"
    assets.mkdir()
    css = f"""
    @font-face {{ font-family: '{family}'; src: url('font.woff2') format('woff2'); }}
    #reader-content {{ font-family: '{family}'; }}
    """
    (assets / "reader.css").write_text(css, encoding="utf-8")
    if font:
        (assets / "font.woff2").write_bytes(FONT_BYTES)
    page.write_text(f"""<!doctype html><html><head>
      <link rel="canonical" href="https://fanqienovel.com/reader/{item_id}">
      <meta name="book_id" content="{book_id}">
      <link rel="stylesheet" href="chapter_files/reader.css">
      <script>window.secret = 'ignored';</script>
      </head><body><div id="reader-content" onclick="steal()">
      <p>原字元㐂 &amp; &lt;script&gt;保留為文字&lt;/script&gt;</p><p>第二段<br>換行</p>
      <iframe src="https://bad.invalid"></iframe></div></body></html>""", encoding="utf-8")
    return page


def test_import_creates_sanitized_item_isolated_preview_with_hashed_font(tmp_path):
    page = _saved_reader(tmp_path)
    preview = import_reader_html(page, "999", "101", "第一章 <標題>",
                                 tmp_path / "app" / "preview" / "fanqie", next_item_id="102")
    html = preview.preview_path.read_text(encoding="utf-8")
    manifest = json.loads((preview.preview_path.parent / "manifest.json").read_text(encoding="utf-8"))

    assert preview.font_sha256 == hashlib.sha256(FONT_BYTES).hexdigest()
    assert (preview.preview_path.parent / "fonts" / f"{preview.font_sha256}.woff2").read_bytes() == FONT_BYTES
    assert preview.paragraph_count == 2
    assert "window.secret" not in html and "onclick" not in html and "<iframe" not in html
    assert "document.fonts.load" in html  # Only the generated local-font status check runs.
    assert "&lt;script&gt;保留為文字&lt;/script&gt;" in html
    assert "第一章 &lt;標題&gt;" in html
    assert "文字尚未還原" in html
    assert 'href="https://fanqienovel.com/reader/102"' in html
    assert "下一章（原站）" in html and "本機只保存這一章" in html
    assert manifest["text_restored"] is False
    assert manifest["book_id"] == "999" and manifest["item_id"] == "101"
    assert preview.preview_path.parent.is_relative_to(tmp_path / "app" / "preview" / "fanqie")
    assert reading_preview_status(tmp_path / "app" / "preview" / "fanqie", "999", "101")[0]
    raw_text = json.loads((preview.preview_path.parent / "raw_text.json").read_text(encoding="utf-8"))
    metadata = json.loads((preview.preview_path.parent / "source_metadata.json").read_text(encoding="utf-8"))
    assert raw_text["paragraphs"][0].startswith("原字元㐂")
    assert metadata["source_url"] == "https://fanqienovel.com/reader/101"
    assert metadata["font_sha256"] == preview.font_sha256
    assert ord("㐂") in metadata["used_codepoints"]
    assert metadata["used_pua_codepoints"] == []
    assert metadata["decoder_version"] is None and metadata["text_restored"] is False


def test_preview_status_rejects_missing_or_changed_font(tmp_path):
    page = _saved_reader(tmp_path)
    root = tmp_path / "preview"
    preview = import_reader_html(page, "999", "101", "第一章", root)
    font = preview.preview_path.parent / "fonts" / f"{preview.font_sha256}.woff2"
    font.write_bytes(FONT_BYTES + b"changed")
    assert reading_preview_status(root, "999", "101") == (False, "字型內容與預覽索引不符")
    font.unlink()
    assert reading_preview_status(root, "999", "101") == (False, "字型檔缺失或不唯一")


def test_preview_status_detects_corrupt_html_and_allows_preserving_repair(tmp_path):
    page = _saved_reader(tmp_path)
    root = tmp_path / "preview"
    initial = import_reader_html(page, "999", "101", "第一章", root)
    initial.preview_path.write_text("<html>broken</html>", encoding="utf-8")
    assert reading_preview_status(root, "999", "101") == (False, "預覽頁內容與索引不符")
    repaired = import_reader_html(page, "999", "101", "第一章", root)
    assert repaired.previous_path is not None
    assert (repaired.previous_path / "preview.html").read_text(encoding="utf-8") == "<html>broken</html>"
    assert reading_preview_status(root, "999", "101")[0]


def test_legacy_preview_without_digest_requires_expected_structure(tmp_path):
    page = _saved_reader(tmp_path)
    root = tmp_path / "preview"
    preview = import_reader_html(page, "999", "101", "第一章", root)
    manifest_path = preview.preview_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["preview_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert reading_preview_status(root, "999", "101")[0]
    preview.preview_path.write_text("<html>broken</html>", encoding="utf-8")
    assert reading_preview_status(root, "999", "101") == (False, "舊版預覽頁內容無法驗證")


def test_reimport_repairs_invalid_preview_and_preserves_previous_files(tmp_path):
    page = _saved_reader(tmp_path)
    root = tmp_path / "preview"
    initial = import_reader_html(page, "999", "101", "第一章", root)
    old_font = initial.preview_path.parent / "fonts" / f"{initial.font_sha256}.woff2"
    old_font.write_bytes(b"damaged-font")

    repaired = import_reader_html(page, "999", "101", "第一章", root)
    assert repaired.previous_path is not None
    assert repaired.previous_path.parent == repaired.preview_path.parent.parent
    assert (repaired.previous_path / "fonts" / old_font.name).read_bytes() == b"damaged-font"
    assert (repaired.previous_path / "source.html").is_file()
    assert reading_preview_status(root, "999", "101")[0]

    with pytest.raises(ReaderImportError, match="已有匯入資料"):
        import_reader_html(page, "999", "101", "第一章", root)
    assert repaired.previous_path.is_dir()


def test_failed_repair_restores_original_invalid_folder(tmp_path, monkeypatch):
    page = _saved_reader(tmp_path)
    root = tmp_path / "preview"
    initial = import_reader_html(page, "999", "101", "第一章", root)
    old_font = initial.preview_path.parent / "fonts" / f"{initial.font_sha256}.woff2"
    old_font.write_bytes(b"damaged-font")
    rename = os.rename

    def fail_promotion(source, target):
        if str(source).find("-import-") >= 0 and target == initial.preview_path.parent:
            raise OSError("synthetic rename failure")
        return rename(source, target)

    monkeypatch.setattr("fanqie_reader_preview.os.rename", fail_promotion)
    with pytest.raises(OSError, match="synthetic rename failure"):
        import_reader_html(page, "999", "101", "第一章", root)
    assert old_font.read_bytes() == b"damaged-font"
    assert not list(old_font.parent.parent.parent.glob("101-previous-*"))


@pytest.mark.parametrize("item_id,book_id", [("wrong", "999"), ("101", "other")])
def test_import_rejects_wrong_chapter_or_book_before_writing(tmp_path, item_id, book_id):
    page = _saved_reader(tmp_path, item_id="101", book_id="999")
    with pytest.raises(ReaderImportError):
        import_reader_html(page, book_id, item_id, "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_import_rejects_non_numeric_path_identity(tmp_path):
    page = _saved_reader(tmp_path)
    with pytest.raises(ReaderImportError, match="必須是目錄中的數字 ID"):
        import_reader_html(page, "../outside", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_import_accepts_bounded_validated_data_url_font(tmp_path):
    page = _saved_reader(tmp_path)
    css = page.parent / "chapter_files" / "reader.css"
    encoded = base64.b64encode(FONT_BYTES).decode("ascii")
    css.write_text(
        f"@font-face {{font-family:'MappedFont';src:url(data:font/woff2;base64,{encoded});}}"
        "#reader-content {font-family:'MappedFont';}", encoding="utf-8"
    )
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert (preview.preview_path.parent / "fonts" / f"{preview.font_sha256}.woff2").read_bytes() == FONT_BYTES


def test_import_rejects_font_rules_for_a_different_scope(tmp_path):
    page = _saved_reader(tmp_path)
    source = page.read_text(encoding="utf-8").replace(
        "<p>原字元", '<p class="mapped">原字元'
    )
    page.write_text(source, encoding="utf-8")
    css = page.parent / "chapter_files" / "reader.css"
    css.write_text(css.read_text(encoding="utf-8") +
                   "#reader-content .mapped {font-family:'WrongFont';}",
                   encoding="utf-8")
    with pytest.raises(ReaderImportError, match="章節段落使用不同字型"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_unused_font_face_is_not_treated_as_an_applied_reader_font(tmp_path):
    page = _saved_reader(tmp_path)
    css = page.parent / "chapter_files" / "reader.css"
    css.write_text("@font-face {font-family:'UnusedFont';src:url('font.woff2');}", encoding="utf-8")
    with pytest.raises(ReaderImportError, match="無法確認正文實際使用的字型"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


@pytest.mark.parametrize("declaration", ["MappedFont, serif", "'MappedFont', serif"])
def test_import_resolves_first_family_from_fallback_list(tmp_path, declaration):
    page = _saved_reader(tmp_path)
    css = page.parent / "chapter_files" / "reader.css"
    css.write_text(
        f"@font-face {{font-family:'MappedFont';src:url('font.woff2');}}"
        f"#reader-content {{font-family:{declaration};}}", encoding="utf-8"
    )
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_family == "MappedFont"


@pytest.mark.parametrize("outer_style,extra_css", [
    ('style="font-family: MappedFont, serif"', ""),
    ('class="font-MappedFont"', ".font-MappedFont {font-family: MappedFont, serif;}"),
    ('class="muye-reader-box font-MappedFont"',
     ".font-MappedFont {font-family: MappedFont, serif;}"
     ".muye-reader-box * {font-family:inherit!important;}"),
])
def test_import_resolves_reader_font_inherited_from_outer_box(tmp_path, outer_style, extra_css):
    page = _saved_reader(tmp_path)
    css = page.parent / "chapter_files" / "reader.css"
    css.write_text(
        "@font-face {font-family:MappedFont;src:url('font.woff2');}" + extra_css,
        encoding="utf-8",
    )
    page.write_text(
        '<html><head><link rel="canonical" href="https://fanqienovel.com/reader/101">'
        '<link rel="stylesheet" href="chapter_files/reader.css"></head><body>'
        f'<div {outer_style}><div class="muye-reader-content"><p>原字元㐂</p>'
        '<p>第二段</p></div></div></body></html>', encoding="utf-8",
    )
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_family == "MappedFont"
    assert preview.paragraph_count == 2


def test_import_rejects_missing_or_mismatched_font_without_publishing_cache(tmp_path):
    missing = _saved_reader(tmp_path, font=False)
    with pytest.raises(ReaderImportError, match="找不到瀏覽器保存的 CSS 或字型資源"):
        import_reader_html(missing, "999", "101", "第一章", tmp_path / "preview")
    (missing.parent / "chapter_files" / "font.woff2").write_bytes(b"not-a-font")
    with pytest.raises(ReaderImportError, match="副檔名不符"):
        import_reader_html(missing, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_import_rejects_remote_font_and_preserves_existing_data(tmp_path):
    page = _saved_reader(tmp_path)
    css_path = tmp_path / "chapter_files" / "reader.css"
    css_path.write_text("@font-face {font-family:'MappedFont';src:url('https://cdn.invalid/font.woff2');}"
                        "#reader-content {font-family:'MappedFont';}", encoding="utf-8")
    destination = tmp_path / "preview" / "999" / "101"
    destination.mkdir(parents=True)
    marker = destination / "keep.txt"
    marker.write_text("user data", encoding="utf-8")
    with pytest.raises(ReaderImportError, match="仍是遠端資源"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert marker.read_text(encoding="utf-8") == "user data"


def test_import_uses_exact_named_local_asset_for_remote_font_declaration(tmp_path):
    page = _saved_reader(tmp_path)
    css_path = tmp_path / "chapter_files" / "reader.css"
    css_path.write_text(
        "@font-face {font-family:'MappedFont';"
        "src:url('https://lf6-awef.bytetos.com/obj/awesome-font/c/abcdef0123456789.woff2') format('woff2');}"
        "#reader-content {font-family:'MappedFont';}", encoding="utf-8",
    )
    (tmp_path / "chapter_files" / "abcdef0123456789.woff2").write_bytes(FONT_BYTES)
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_sha256 == hashlib.sha256(FONT_BYTES).hexdigest()
    assert b"bytetos.com" not in preview.preview_path.read_bytes()


def test_import_rejects_wrong_named_or_invalid_local_remote_font(tmp_path):
    page = _saved_reader(tmp_path)
    css_path = tmp_path / "chapter_files" / "reader.css"
    css_path.write_text(
        "@font-face {font-family:'MappedFont';"
        "src:url('https://lf6-awef.bytetos.com/obj/awesome-font/c/abcdef0123456789.woff2');}"
        "#reader-content {font-family:'MappedFont';}", encoding="utf-8",
    )
    with pytest.raises(ReaderImportError, match="沒有同名字型檔"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    (tmp_path / "chapter_files" / "abcdef0123456789.woff2").write_bytes(b"wrong font")
    with pytest.raises(ReaderImportError, match="副檔名不符"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_import_rejects_unsafe_next_chapter_link(tmp_path):
    page = _saved_reader(tmp_path)
    with pytest.raises(ReaderImportError, match="下一章 itemId"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview",
                           next_item_id='102" onclick="run()')
    assert not (tmp_path / "preview").exists()


def test_import_selects_normal_face_when_bold_face_appears_first(tmp_path):
    page = _saved_reader(tmp_path)
    assets = tmp_path / "chapter_files"
    normal = b"wOF2normal-font"
    bold = b"wOF2bold-font"
    (assets / "normal.woff2").write_bytes(normal)
    (assets / "bold.woff2").write_bytes(bold)
    (assets / "reader.css").write_text(
        "@font-face {font-family:MappedFont;font-weight:700;src:url('bold.woff2');}"
        "@font-face {font-family:MappedFont;font-weight:400;src:url('normal.woff2');}"
        "#reader-content {font-family:MappedFont;}", encoding="utf-8",
    )
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_sha256 == hashlib.sha256(normal).hexdigest()


def test_import_selects_face_matching_reader_weight_and_style(tmp_path):
    page = _saved_reader(tmp_path)
    assets = tmp_path / "chapter_files"
    normal = b"wOF2normal-font"
    medium_italic = b"wOF2medium-italic-font"
    (assets / "normal.woff2").write_bytes(normal)
    (assets / "medium.woff2").write_bytes(medium_italic)
    (assets / "reader.css").write_text(
        "@font-face {font-family:MappedFont;font-weight:400;src:url('normal.woff2');}"
        "@font-face {font-family:MappedFont;font-weight:500;font-style:italic;src:url('medium.woff2');}"
        "#reader-content {font-family:MappedFont;font-weight:500;font-style:italic;}",
        encoding="utf-8",
    )
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_sha256 == hashlib.sha256(medium_italic).hexdigest()
    document = preview.preview_path.read_text(encoding="utf-8")
    assert "font:italic 500 18px/2" in document
    assert '"italic 500 18px "' in document
    manifest = json.loads((preview.preview_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["font_weight"] == 500 and manifest["font_style"] == "italic"
    assert manifest["preview_sha256"] == hashlib.sha256(preview.preview_path.read_bytes()).hexdigest()


def test_import_rejects_missing_matching_weight_face(tmp_path):
    page = _saved_reader(tmp_path)
    css = tmp_path / "chapter_files" / "reader.css"
    css.write_text("@font-face {font-family:MappedFont;font-weight:400;src:url('font.woff2');}"
                   "#reader-content {font-family:MappedFont;font-weight:500;}", encoding="utf-8")
    with pytest.raises(ReaderImportError, match="字重／樣式相符"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")


def test_import_rejects_mixed_paragraph_weights(tmp_path):
    page = _saved_reader(tmp_path)
    source = page.read_text(encoding="utf-8").replace("<p>第二段", '<p style="font-weight:700">第二段')
    page.write_text(source, encoding="utf-8")
    with pytest.raises(ReaderImportError, match="不同字重"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")


def test_import_respects_css_specificity_for_reader_weight(tmp_path):
    page = _saved_reader(tmp_path)
    assets = tmp_path / "chapter_files"
    medium = b"wOF2medium-font"
    (assets / "medium.woff2").write_bytes(medium)
    (assets / "reader.css").write_text(
        "@font-face {font-family:MappedFont;font-weight:400;src:url('font.woff2');}"
        "@font-face {font-family:MappedFont;font-weight:500;src:url('medium.woff2');}"
        "#reader-content {font-family:MappedFont;}"
        "#reader-content p {font-weight:500;} p {font-weight:400;}", encoding="utf-8")
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_sha256 == hashlib.sha256(medium).hexdigest()


def test_import_respects_important_over_specificity_and_inline_style(tmp_path):
    page = _saved_reader(tmp_path)
    assets = tmp_path / "chapter_files"
    medium = b"wOF2medium-font"
    (assets / "medium.woff2").write_bytes(medium)
    (assets / "reader.css").write_text(
        "@font-face {font-family:MappedFont;font-weight:400;src:url('font.woff2');}"
        "@font-face {font-family:MappedFont;font-weight:500;src:url('medium.woff2');}"
        "#reader-content {font-family:MappedFont;}"
        "#reader-content p {font-weight:400;} p {font-weight:500!important;}", encoding="utf-8")
    source = page.read_text(encoding="utf-8").replace("<p>", '<p style="font-weight:400">')
    page.write_text(source, encoding="utf-8")
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_sha256 == hashlib.sha256(medium).hexdigest()


def test_import_resolves_font_shorthand_weight_and_style(tmp_path):
    page = _saved_reader(tmp_path)
    assets = tmp_path / "chapter_files"
    italic = b"wOF2italic-medium-font"
    (assets / "italic.woff2").write_bytes(italic)
    (assets / "reader.css").write_text(
        "@font-face {font-family:MappedFont;font-weight:400;src:url('font.woff2');}"
        "@font-face {font-family:MappedFont;font-weight:500;font-style:italic;src:url('italic.woff2');}"
        "#reader-content {font-family:MappedFont;font:italic 500 18px MappedFont;}"
        ".unrelated {font:var(--unknown);}", encoding="utf-8")
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.font_sha256 == hashlib.sha256(italic).hexdigest()


def test_import_rejects_unresolved_active_font_shorthand(tmp_path):
    page = _saved_reader(tmp_path)
    css = tmp_path / "chapter_files" / "reader.css"
    css.write_text("@font-face {font-family:MappedFont;src:url('font.woff2');}"
                   "#reader-content {font-family:MappedFont;font:var(--unknown);}",
                   encoding="utf-8")
    with pytest.raises(ReaderImportError, match="font 簡寫"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")


def test_import_rejects_font_family_that_could_break_generated_style(tmp_path):
    page = _saved_reader(tmp_path, family="MappedFont</style>")
    with pytest.raises(ReaderImportError, match="不安全字元"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


@pytest.mark.parametrize("url", [
    "https://evil.example/reader/101",
    "http://fanqienovel.com/reader/101",
    "https://fanqienovel.com/reader/102",
])
def test_import_rejects_wrong_canonical_origin_or_item(tmp_path, url):
    page = _saved_reader(tmp_path)
    page.write_text(page.read_text(encoding="utf-8").replace(
        "https://fanqienovel.com/reader/101", url), encoding="utf-8")
    with pytest.raises(ReaderImportError, match="來源或章節 itemId"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_import_rejects_unsafe_local_resource_escape(tmp_path):
    page = _saved_reader(tmp_path)
    css_path = tmp_path / "chapter_files" / "reader.css"
    css_path.write_text("@font-face {font-family:'MappedFont';src:url('../outside.woff2');}"
                        "#reader-content {font-family:'MappedFont';}", encoding="utf-8")
    (tmp_path / "outside.woff2").write_bytes(FONT_BYTES)
    with pytest.raises(ReaderImportError, match="對應的瀏覽器保存資源資料夾"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_error_page_and_original_json_cache_are_not_misused_or_overwritten(tmp_path):
    preview_root = tmp_path / "preview" / "fanqie"
    raw_cache = preview_root / "999" / "101.json"
    raw_cache.parent.mkdir(parents=True)
    raw_cache.write_text('{"source_response_text":"keep raw"}', encoding="utf-8")
    error_page = tmp_path / "challenge.html"
    error_page.write_text("<html><body>verification required</body></html>", encoding="utf-8")
    with pytest.raises(ReaderImportError, match="itemId"):
        import_reader_html(error_page, "999", "101", "第一章", preview_root)
    assert raw_cache.read_text(encoding="utf-8") == '{"source_response_text":"keep raw"}'
    assert not (tmp_path / "cache").exists()


def test_challenge_page_with_matching_chapter_id_never_enters_preview(tmp_path):
    page = _saved_reader(tmp_path)
    page.write_text(page.read_text(encoding="utf-8").replace(
        "<p>原字元", '<div id="bdturing-verify"></div><p>原字元'), encoding="utf-8")
    with pytest.raises(ReaderImportError, match="人機驗證要求"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert not (tmp_path / "preview").exists()


def test_gate_phrases_in_prose_or_script_do_not_reject_public_reader(tmp_path):
    page = _saved_reader(tmp_path)
    source = page.read_text(encoding="utf-8")
    source = source.replace("原字元㐂", "他說请先登录，又提到购买本章與bdturing-verify")
    source = source.replace("window.secret = 'ignored'", "window.secret = '人机验证'")
    page.write_text(source, encoding="utf-8")
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.paragraph_count == 2


def test_inert_template_gate_markup_does_not_block_public_reader(tmp_path):
    page = _saved_reader(tmp_path)
    source = page.read_text(encoding="utf-8")
    source = source.replace("</body>",
                            '<template><div id="login-dialog"></div></template>'
                            '<div hidden><div id="bdturing-verify"></div></div></body>')
    page.write_text(source, encoding="utf-8")
    preview = import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
    assert preview.paragraph_count == 2


def test_reader_body_containing_only_gate_notice_is_rejected(tmp_path):
    page = _saved_reader(tmp_path)
    source = page.read_text(encoding="utf-8")
    first = source.index("<p>")
    second_end = source.index("</p>", source.index("</p>", first) + 4) + 4
    source = source[:first] + "<p>请先登录</p>" + source[second_end:]
    page.write_text(source, encoding="utf-8")
    with pytest.raises(ReaderImportError, match="人機驗證要求"):
        import_reader_html(page, "999", "101", "第一章", tmp_path / "preview")
