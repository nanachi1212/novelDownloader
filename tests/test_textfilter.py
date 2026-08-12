import sys

import textfilter


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
