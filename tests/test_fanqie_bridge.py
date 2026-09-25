"""Fanqie full-text provider tests.  No third-party EXE, network or real story text."""
import json
import socket
import time
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import downloader_task
import fanqie_bridge as fb
from downloader_task import Cancelled
from fetcher import FetchError
from sites.fanqie import AccessVerificationRequired, FanqieAdapter
from test_fanqie_download import BOOK_ID, BOOK_URL, PUBLIC, RAW, _directory, _item, _meta, _reader

RULE = "-" * 40
SEPARATOR = "=" * 40


def tomato_txt(book_id, chapters, volume=True):
    """A synthetic export shaped like Tomato's TXT: header, rule, then chapters."""
    parts = [f"合成書\n作者：測試\nbook_id={book_id}\n簡介：\n　　合成簡介\n\n{SEPARATOR}\n\n"]
    if volume:
        parts.append("【第一卷：合成卷】\n\n")
    for title, paragraphs in chapters:
        parts.append(f"{title}\n\n" + "\n\n".join("　　" + text for text in paragraphs)
                     + f"\n\n\n{RULE}\n\n")
    return "".join(parts)


def chapters_of(*specs):
    return [fb.ProviderChapter(item_id, title, order) for item_id, title, order in specs]


# --- parsing ---------------------------------------------------------------

def test_parse_tomato_txt_splits_chapters_and_drops_rules_and_indent():
    text = tomato_txt(BOOK_ID, [("第1章 甲", ["第一段", "【叮！系統提示】"]), ("第2章 乙", ["第二段"])])
    bodies = fb.parse_tomato_txt(text, chapters_of(("1", "第1章 甲", 1), ("2", "第2章 乙", 2)))
    assert bodies == {"1": "第一段\n\n【叮！系統提示】", "2": "第二段"}


def test_parse_tomato_txt_drops_volume_heading_between_chapters_only():
    text = tomato_txt(BOOK_ID, [("第1章 甲", ["甲文"])], volume=True)
    text += "【第二卷：合成卷二】\n\n第2章 乙\n\n　　乙文\n\n" + RULE + "\n"
    bodies = fb.parse_tomato_txt(text, chapters_of(("1", "第1章 甲", 1), ("2", "第2章 乙", 2)))
    assert bodies == {"1": "甲文", "2": "乙文"}


def test_parse_tomato_txt_fails_closed():
    good = tomato_txt(BOOK_ID, [("第1章 甲", ["甲文"]), ("第2章 乙", ["乙文"])])
    with pytest.raises(fb.ProviderError, match="找不到章節標題"):
        fb.parse_tomato_txt(good, chapters_of(("9", "第9章 丙", 9)))
    with pytest.raises(fb.ProviderError, match="找不到章節標題"):  # out of order
        fb.parse_tomato_txt(good, chapters_of(("2", "第2章 乙", 2), ("1", "第1章 甲", 1)))
    with pytest.raises(fb.ProviderError, match="沒有正文"):
        fb.parse_tomato_txt(tomato_txt(BOOK_ID, [("第1章 甲", [])]), chapters_of(("1", "第1章 甲", 1)))
    with pytest.raises(fb.ProviderError, match="分隔線"):
        fb.parse_tomato_txt("第1章 甲\n\n甲文\n", chapters_of(("1", "第1章 甲", 1)))


# --- fake Tomato server ----------------------------------------------------

class FakeTomato:
    """Stands in for `TomatoNovelDownloader --server`; records how it was driven."""

    def __init__(self, book_titles, ask_format=True, fail_message=None, offer=("txt", "epub"), prewarm=False):
        self.prewarm = prewarm
        self.book_titles = book_titles  # {order: (title, [paragraphs])}
        self.ask_format = ask_format
        self.fail_message = fail_message
        self.offer = offer
        self.jobs, self.formats, self.cancelled, self.spawns = {}, [], [], []
        self.servers = []

    def spawn(self, argv, cwd, env, **_kwargs):
        host, port = env["TOMATO_WEB_ADDR"].split(":")
        library = cwd
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _reply(self, payload, status=200):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _payload(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/api/status":
                    return self._reply({"prewarm_in_progress": fake.prewarm, "prewarm_error": None,
                                        "save_dir": library})
                job_id = int(parse_qs(url.query)["id"][0])
                job = fake.jobs[job_id]
                job["polls"] += 1
                item = {"id": job_id, "book_id": job["book_id"], "format_options": None,
                        "message": None, "progress": {"saved_chapters": 1, "chapter_total": 2},
                        "state": "running"}
                if job["polls"] >= 2:
                    if fake.fail_message:
                        item.update(state="failed", message=fake.fail_message)
                    elif fake.ask_format and not job["format"]:
                        item.update(format_options=[{"label": name, "value": name} for name in fake.offer],
                                    message="等待选择输出格式")
                    else:
                        item["state"] = "done"
                        fake.write_output(library, job)
                self._reply({"items": [item]})

            def do_POST(self):
                url, payload = urlparse(self.path), self._payload()
                if url.path == "/api/jobs":
                    job_id = len(fake.jobs) + 1
                    fake.jobs[job_id] = {"book_id": payload["book_id"], "range": (
                        payload["range_start"], payload["range_end"]), "polls": 0, "format": None}
                    return self._reply({"id": job_id, "book_id": payload["book_id"], "state": "queued"})
                _api, _jobs, job_id, action = url.path.strip("/").split("/")
                if action == "format":
                    fake.jobs[int(job_id)]["format"] = payload["value"]
                    fake.formats.append(payload["value"])
                else:
                    fake.cancelled.append(int(job_id))
                self._reply({"ok": True})

        server = ThreadingHTTPServer((host, int(port)), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        self.spawns.append((argv, cwd, dict(env)))
        return FakeProcess(server)

    def write_output(self, library, job):
        start, end = job["range"]
        text = tomato_txt(job["book_id"], [self.book_titles[order] for order in range(start, end + 1)])
        (Path(library) / "合成書.txt").write_text(text, encoding="utf-8")


class FakeProcess:
    def __init__(self, server):
        self.server, self.code = server, None

    def poll(self):
        return self.code

    def terminate(self):
        if self.code is None:
            self.server.shutdown()
            self.server.server_close()
            self.code = 1

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.terminate()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "Tomato.exe"
    path.write_bytes(b"not a real program")
    return path


BOOK = {order: (f"第{order}章 標題{order}", [f"正文{order}甲", f"正文{order}乙"]) for order in range(1, 30)}


def make_provider(tmp_path, exe, fake, **kwargs):
    return fb.TomatoBridgeProvider(
        exe, root=tmp_path / "bridge", port=free_port(), port_scan=5, poll_interval=0.01,
        spawn=fake.spawn, **kwargs)


def test_bridge_runs_range_selects_txt_and_returns_bodies(tmp_path, exe):
    fake = FakeTomato(BOOK)
    provider = make_provider(tmp_path, exe, fake)
    try:
        bodies = provider.fetch_chapters(BOOK_ID, chapters_of(("11", "第11章 標題11", 11), ("12", "第12章 標題12", 12)))
    finally:
        provider.close()
    assert bodies == {"11": "正文11甲\n\n正文11乙", "12": "正文12甲\n\n正文12乙"}
    assert fake.jobs[1]["range"] == (11, 12)
    assert fake.formats == ["txt"]
    argv, cwd, env = fake.spawns[0]
    assert argv[1:3] == ["--server", "--data-dir"] and argv[3] == str(provider.data_dir)
    assert env["TOMATO_WEB_ADDR"].startswith("127.0.0.1:") and "TOMATO_WEB_PASSWORD" not in env
    assert cwd == str(provider.library_dir)
    config = (provider.data_dir / "config.yml").read_text(encoding="utf-8")
    assert "novel_format: txt" in config and "ask_format_after_download: true" in config
    assert list(provider.library_dir.glob("*.txt")) == []  # consumed output is removed
    assert fake.servers[0].socket.fileno() == -1  # our process was terminated by close()


def test_bridge_keeps_other_config_keys_and_patches_its_own(tmp_path, exe):
    fake = FakeTomato(BOOK)
    provider = make_provider(tmp_path, exe, fake)
    provider.data_dir.mkdir(parents=True)
    (provider.data_dir / "config.yml").write_text(
        "max_workers: 1\nnovel_format: epub\nask_format_after_download: false\n", encoding="utf-8")
    try:
        provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)))
    finally:
        provider.close()
    config = (provider.data_dir / "config.yml").read_text(encoding="utf-8").splitlines()
    assert "max_workers: 1" in config
    assert "novel_format: txt" in config and "novel_format: epub" not in config
    assert "ask_format_after_download: true" in config


def test_bridge_reuses_one_process_and_splits_non_consecutive_ranges(tmp_path, exe):
    fake = FakeTomato(BOOK)
    provider = make_provider(tmp_path, exe, fake)
    try:
        provider.fetch_chapters(BOOK_ID, chapters_of(("3", "第3章 標題3", 3), ("9", "第9章 標題9", 9),
                                                     ("4", "第4章 標題4", 4)))
        provider.fetch_chapters(BOOK_ID, chapters_of(("20", "第20章 標題20", 20)))
    finally:
        provider.close()
    assert [job["range"] for job in fake.jobs.values()] == [(3, 4), (9, 9), (20, 20)]
    assert len(fake.spawns) == 1


def test_bridge_never_touches_an_instance_it_did_not_start(tmp_path, exe):
    fake = FakeTomato(BOOK)
    port = free_port()
    with socket.socket() as squatter:  # stands in for the user's own Tomato
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        provider = fb.TomatoBridgeProvider(exe, root=tmp_path / "bridge", port=port, port_scan=5,
                                           poll_interval=0.01, spawn=fake.spawn)
        try:
            provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)))
        finally:
            provider.close()
        assert fake.spawns[0][2]["TOMATO_WEB_ADDR"] != f"127.0.0.1:{port}"
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass  # the user's listener is still there


def test_bridge_reports_failed_job_and_missing_txt_option(tmp_path, exe):
    fake = FakeTomato(BOOK, fail_message="账号异常")
    provider = make_provider(tmp_path, exe, fake)
    try:
        with pytest.raises(fb.ProviderError, match="账号异常"):
            provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)))
    finally:
        provider.close()
    fake = FakeTomato(BOOK, offer=("epub", "pdf"))
    provider = make_provider(tmp_path, exe, fake)
    try:
        with pytest.raises(fb.ProviderError, match="沒有 txt"):
            provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)))
        assert fake.cancelled == [1]
    finally:
        provider.close()


def test_bridge_cancel_stops_the_job(tmp_path, exe):
    fake = FakeTomato(BOOK)
    provider = make_provider(tmp_path, exe, fake)
    calls = []

    def cancel_after_first_poll():
        calls.append(1)
        return len(calls) > 3

    try:
        with pytest.raises(fb.ProviderCancelled):
            provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)),
                                    cancel_check=cancel_after_first_poll)
    finally:
        provider.close()
    assert fake.cancelled == [1]


def test_closing_while_the_bridge_is_still_starting_does_not_block(tmp_path, exe):
    fake = FakeTomato(BOOK, prewarm=True)  # never becomes ready
    provider = make_provider(tmp_path, exe, fake, startup_timeout=60)
    outcome = []

    def start():
        try:
            provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)))
        except fb.ProviderError as exc:
            outcome.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    deadline = time.monotonic() + 5
    while not fake.spawns and time.monotonic() < deadline:
        time.sleep(0.01)
    began = time.monotonic()
    provider.close()
    assert time.monotonic() - began < 2
    worker.join(5)
    assert not worker.is_alive() and isinstance(outcome[0], fb.ProviderCancelled)
    assert fake.servers[0].socket.fileno() == -1


def test_bridge_rejects_wrong_titles_and_bad_exe(tmp_path, exe):
    fake = FakeTomato(BOOK)
    provider = make_provider(tmp_path, exe, fake)
    try:
        with pytest.raises(fb.ProviderError, match="找不到章節標題"):
            provider.fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 別的標題", 1)))
    finally:
        provider.close()
    with pytest.raises(fb.ProviderError, match="找不到"):
        fb.TomatoBridgeProvider(tmp_path / "missing.exe", root=tmp_path / "b2", spawn=fake.spawn) \
            .fetch_chapters(BOOK_ID, chapters_of(("1", "第1章 標題1", 1)))
    with pytest.raises(fb.ProviderError, match="bookId"):
        provider.fetch_chapters("abc", chapters_of(("1", "第1章 標題1", 1)))


# --- settings --------------------------------------------------------------

def test_settings_round_trip_and_validation(tmp_path, exe):
    assert fb.load_exe_path() == "" and fb.configured_provider() is None
    fb.save_exe_path(str(exe))
    provider = fb.configured_provider()
    assert isinstance(provider, fb.TomatoBridgeProvider) and provider.exe_path == str(exe)
    assert fb.configured_provider() is provider
    assert FanqieAdapter().default_full_text_provider() is provider
    fb.save_exe_path("")
    assert fb.load_exe_path() == "" and fb.configured_provider() is None
    (tmp_path / "tool.bat").write_text("", encoding="utf-8")
    for bad in ("relative.exe", str(tmp_path / "tool.bat"), str(tmp_path / "nope.exe")):
        with pytest.raises(fb.ProviderError):
            fb.save_exe_path(bad)
    fb.shutdown_providers()


# --- downloader pipeline ---------------------------------------------------

class ScriptedProvider(fb.FullTextProvider):
    name = "scripted"

    def __init__(self, bodies=None, error=None):
        self.bodies, self.error, self.calls = bodies or {}, error, []

    def fetch_chapters(self, book_id, chapters, cancel_check=None, progress=None):
        self.calls.append((book_id, [(c.item_id, c.title, c.order) for c in chapters]))
        if self.error:
            raise self.error
        if progress:
            progress("scripted progress")
        return {c.item_id: self.bodies[c.item_id] for c in chapters}


def _two_chapter_directory(second_flags=None):
    second = {**_item("202", **(second_flags or {"isChapterLock": True})), "title": "第2章"}
    return _directory({**_item("101"), "title": "第1章"}, second)


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    state = {"reader_calls": [], "directory": _two_chapter_directory(), "reader": {}}

    class FakeFetcher:
        def __init__(self, **_kwargs):
            self.last_response_headers = {}
            self.last_status_code = 200

        def get(self, url, **_kwargs):
            if "/page/" in url:
                return _meta()
            if "/directory/detail" in url:
                return state["directory"]
            item_id = url.rsplit("/", 1)[-1]
            state["reader_calls"].append(item_id)
            return state["reader"].get(item_id) or _reader(item_id=item_id)

        def polite_sleep(self):
            pass

    monkeypatch.setattr(downloader_task, "Fetcher", FakeFetcher)
    monkeypatch.setattr(downloader_task, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(downloader_task, "load_rules", lambda _site: [])
    monkeypatch.setattr(downloader_task.time, "sleep", lambda _seconds: None)
    state["run"] = lambda **kw: downloader_task.download_novel(
        BOOK_URL, tmp_path / "out", delay=0, retries=2, **kw)
    return state


def test_without_provider_locked_chapters_still_fail_closed(pipeline):
    with pytest.raises(AccessVerificationRequired):
        pipeline["run"]()
    assert "202" not in pipeline["reader_calls"]


def test_provider_supplies_only_the_chapters_web_cannot_and_resume_skips_it(pipeline, tmp_path):
    provider = ScriptedProvider({"202": "補全甲\n\n補全乙"})
    messages = []
    output = pipeline["run"](full_text_provider=provider, callback=lambda *a: messages.append(a))
    text = output.read_text(encoding="utf-8")
    assert "人在这里" in text and "補全甲\n\n補全乙" in text
    assert text.index("人在这里") < text.index("補全甲")
    assert provider.calls == [(BOOK_ID, [("202", "第2章", 2)])]
    assert pipeline["reader_calls"] == ["101"]  # the locked chapter never hits the web reader
    assert (tmp_path / "cache" / BOOK_ID / "202.txt").read_text(encoding="utf-8") == "補全甲\n\n補全乙"
    assert any(a[0] == "provider" and "scripted progress" in a[3] for a in messages)
    again = pipeline["run"](full_text_provider=provider)
    assert again.read_text(encoding="utf-8") == text
    assert len(provider.calls) == 1  # cached chapters are not requested again
    epub = pipeline["run"](full_text_provider=provider, output_format="epub")
    assert epub.suffix == ".epub" and len(provider.calls) == 1


def test_web_preview_only_body_falls_back_to_provider_only_when_configured(pipeline):
    short = _reader(content="<p>短</p>", chapterWordNumber=2000, **PUBLIC)
    pipeline["directory"] = _directory({**_item("101"), "title": "第1章"})
    pipeline["reader"]["101"] = short
    with pytest.raises(FetchError, match="僅提供預覽"):
        pipeline["run"](end=1)
    provider = ScriptedProvider({"101": "完整正文"})
    before = len(pipeline["reader_calls"])
    output = pipeline["run"](end=1, full_text_provider=provider)
    assert "完整正文" in output.read_text(encoding="utf-8") and "短" not in output.read_text(encoding="utf-8")
    assert len(pipeline["reader_calls"]) == before + 1  # a confirmed preview is not retried
    assert provider.calls == [(BOOK_ID, [("101", "第1章", 1)])]


def test_deferred_preview_chapter_is_counted_once(pipeline, tmp_path):
    pipeline["directory"] = _directory({**_item("101"), "title": "第1章"})
    pipeline["reader"]["101"] = _reader(content="<p>短</p>", chapterWordNumber=2000, **PUBLIC)
    progress = []
    pipeline["run"](end=1, full_text_provider=ScriptedProvider({"101": "完整正文"}),
                    callback=lambda stage, current, total, msg: progress.append((stage, current, total)))
    assert all(current <= total for stage, current, total in progress if stage == "chapter")
    assert ("chapter", 1, 1) in progress and ("chapter", 2, 1) not in progress
    saved = json.loads((tmp_path / "cache" / BOOK_ID / "progress.json").read_text(encoding="utf-8"))
    assert saved["status"] == "done"


def test_preview_found_only_in_the_retry_pass_still_uses_the_provider(pipeline, tmp_path, monkeypatch):
    pipeline["directory"] = _directory({**_item("101"), "title": "第1章"})
    short = _reader(content="<p>短</p>", chapterWordNumber=2000, **PUBLIC)
    calls = []

    class FlakyFetcher:
        def __init__(self, **_kwargs):
            self.last_response_headers = {}
            self.last_status_code = 200

        def get(self, url, **_kwargs):
            if "/page/" in url:
                return _meta()
            if "/directory/detail" in url:
                return pipeline["directory"]
            calls.append(url)
            if len(calls) <= 2:  # every first-pass attempt fails transiently
                self.last_status_code = 502
                raise FetchError("HTTP 502")
            self.last_status_code = 200
            return short

        def polite_sleep(self):
            pass

    monkeypatch.setattr(downloader_task, "Fetcher", FlakyFetcher)
    provider = ScriptedProvider({"101": "完整正文"})
    progress = []
    output = pipeline["run"](end=1, full_text_provider=provider,
                             callback=lambda stage, current, total, msg: progress.append((stage, current, total)))
    assert "完整正文" in output.read_text(encoding="utf-8")
    assert provider.calls == [(BOOK_ID, [("101", "第1章", 1)])]
    assert all(current <= total for stage, current, total in progress if stage == "chapter")


def test_provider_errors_and_cancel_do_not_cache_or_finish(pipeline, tmp_path):
    cache_file = tmp_path / "cache" / BOOK_ID / "202.txt"
    with pytest.raises(fb.ProviderError, match="壞掉"):
        pipeline["run"](full_text_provider=ScriptedProvider(error=fb.ProviderError("壞掉了")))
    progress = json.loads((tmp_path / "cache" / BOOK_ID / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "error" and "壞掉" in progress["last_error"]
    assert not cache_file.exists()
    with pytest.raises(Cancelled):
        pipeline["run"](full_text_provider=ScriptedProvider(error=fb.ProviderCancelled("stop")))
    assert not cache_file.exists()
    with pytest.raises(fb.ProviderError, match="空正文"):
        pipeline["run"](full_text_provider=ScriptedProvider({"202": "  "}))
    assert not cache_file.exists()


def test_provider_is_ignored_for_other_sites():
    from sites.generic import GenericAdapter
    assert getattr(GenericAdapter(), "supports_full_text_provider", False) is False
    assert GenericAdapter().prefers_full_text_provider(None) is False
