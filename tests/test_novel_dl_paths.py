import sys
import types

import app_paths
import novel_dl


def test_frozen_cli_default_output_uses_user_data_dir(monkeypatch, tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    local_app_data = tmp_path / "local-app-data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_dir / "novelDownloader.exe"))
    monkeypatch.delenv("NOVELDOWNLOADER_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setattr(novel_dl, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["novel_dl.py", "https://example.test/book"])

    captured = {}
    fake_downloader = types.ModuleType("downloader_task")
    fake_downloader.download_novel = lambda url, output_dir, **kwargs: captured.update(
        url=url, output_dir=output_dir
    )
    monkeypatch.setitem(sys.modules, "downloader_task", fake_downloader)

    novel_dl.main()

    expected = local_app_data / app_paths.APP_NAME / "Downloads"
    assert captured["output_dir"] == expected
    assert expected.is_relative_to(local_app_data)
    assert not (install_dir / "Downloads").exists()
