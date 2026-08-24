import sys

import textfilter


def test_repeated_fiction_phrase_is_not_removed():
    contents = [f"第{i}章\n\n片刻後。\n\n各章不同內容{i}" for i in range(5)]

    cleaned, removed = textfilter.drop_repeated(contents)

    assert all("片刻後。" in chapter for chapter in cleaned)
    assert "片刻後。" not in removed


def test_known_advertising_phrase_is_removed_after_two_chapters():
    contents = ["正文\n\n閱讀全文", "另一章\n\n閱讀全文", "三", "四", "五"]

    cleaned, removed = textfilter.drop_repeated(contents)

    assert "閱讀全文" in removed
    assert "閱讀全文" not in cleaned[0]
    assert "閱讀全文" not in cleaned[1]


def test_frozen_rules_fall_back_to_bundled_defaults(monkeypatch, tmp_path):
    app_dir = tmp_path / "app"
    bundle_dir = tmp_path / "bundle"
    app_dir.mkdir()
    bundle_dir.mkdir()
    (bundle_dir / "filter_rules.txt").write_text("內建廣告", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app_dir / "novelDownloader.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)

    assert textfilter.load_rules() == [("str", "內建廣告")]

    (app_dir / "filter_rules.txt").write_text("使用者規則", encoding="utf-8")
    assert textfilter.load_rules() == [("str", "使用者規則")]
