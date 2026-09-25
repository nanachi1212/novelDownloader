"""Optional local full-text providers for Fanqie.

The Fanqie web reader may only expose a preview.  A provider is a local tool the
user installs and points novelDownloader at; it returns chapter bodies that are
then cached and written through the normal downloader pipeline.  Nothing from
any third-party tool is bundled or copied here.

``TomatoBridgeProvider`` drives a user-supplied TomatoNovelDownloader EXE
through its documented ``--server`` HTTP interface.  A future local provider
implements the same ``FullTextProvider`` interface.
"""
import atexit
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app_paths import prepare_app_data
from state_io import read_json, write_json

logger = logging.getLogger(__name__)

SETTINGS_FILE = "fanqie_bridge.json"
DEFAULT_PORT = 18423
PORT_SCAN = 10
TXT_SEPARATOR_RE = re.compile(r"^={20,}\s*$")
CHAPTER_RULE_RE = re.compile(r"^-{20,}\s*$")
VOLUME_TITLE_RE = re.compile(r"^【第[^】]{1,20}[卷部篇集][:：][^】]*】$")
BOOK_ID_LINE_RE = re.compile(r"^book_id=(\d+)\s*$", re.M)
FAILED_STATES = {"failed", "canceled", "cancelled", "error"}


class ProviderError(RuntimeError):
    """A full-text provider could not deliver the requested chapters."""


class ProviderCancelled(ProviderError):
    """The user cancelled while a provider was working."""


@dataclass(frozen=True)
class ProviderChapter:
    item_id: str
    title: str
    order: int  # 1-based position in the book's original directory


class FullTextProvider:
    """Interface shared by every local full-text provider."""

    name = "provider"

    def fetch_chapters(self, book_id, chapters, cancel_check=None, progress=None):
        """Return ``{item_id: body}`` for every ProviderChapter, or raise ProviderError.

        Bodies use the pipeline's cache form: paragraphs joined by a blank line,
        without indentation or the chapter title.
        """
        raise NotImplementedError

    def close(self):
        """Release anything the provider started."""


# --- settings -------------------------------------------------------------

def _settings_path():
    return prepare_app_data() / SETTINGS_FILE


def load_exe_path():
    data = read_json(_settings_path(), {})
    value = data.get("tomato_exe") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) else ""


def save_exe_path(path):
    """Persist the EXE path; an empty value disables the bridge."""
    path = str(path or "").strip()
    if path:
        validate_exe_path(path)
    write_json(_settings_path(), {"tomato_exe": path})


def validate_exe_path(path):
    exe = Path(str(path).strip().strip('"'))
    if not exe.is_absolute():
        raise ProviderError(f"Tomato EXE 路徑必須是完整路徑：{path}")
    if exe.suffix.lower() != ".exe":
        raise ProviderError(f"Tomato EXE 路徑必須指向 .exe 檔案：{path}")
    if not exe.is_file():
        raise ProviderError(f"找不到 Tomato EXE：{exe}")
    return exe


def configured_provider():
    """The provider chosen in settings, or None when the bridge is not configured."""
    path = load_exe_path()
    return get_tomato_provider(path) if path else None


# --- Tomato text parsing --------------------------------------------------

def _title_key(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text))).strip()


def txt_book_id(text):
    match = BOOK_ID_LINE_RE.search(text[:8192])
    return match.group(1) if match else None


def parse_tomato_txt(text, chapters):
    """Split a Tomato TXT export into per-chapter bodies, failing closed.

    Every requested chapter title must appear, in order, as its own line.
    """
    lines = text.splitlines()
    sep = next((i for i, line in enumerate(lines) if TXT_SEPARATOR_RE.match(line)), None)
    if sep is None:
        raise ProviderError("Tomato 輸出的 TXT 缺少標頭分隔線，格式不明，未保存。")
    starts, pos = [], sep + 1
    for chapter in chapters:
        wanted = _title_key(chapter.title)
        found = next((i for i in range(pos, len(lines)) if _title_key(lines[i]) == wanted), None)
        if found is None:
            raise ProviderError(f"Tomato 輸出找不到章節標題「{chapter.title}」，未保存。")
        starts.append(found)
        pos = found + 1
    result = {}
    for index, chapter in enumerate(chapters):
        last = index + 1 == len(chapters)
        block = lines[starts[index] + 1:len(lines) if last else starts[index + 1]]
        # Trailing blanks, Tomato's per-chapter rule and (before the next chapter) a
        # volume heading are layout, not story text; they may appear in either order.
        while block and (not block[-1].strip() or CHAPTER_RULE_RE.match(block[-1].strip())
                         or (not last and VOLUME_TITLE_RE.match(block[-1].strip()))):
            block.pop()
        body = "\n\n".join(line.strip() for line in block if line.strip())
        if not body:
            raise ProviderError(f"Tomato 輸出的章節「{chapter.title}」沒有正文，未保存。")
        result[chapter.item_id] = body
    return result


def _same_path(left, right):
    if not left:
        return False
    return os.path.normcase(os.path.realpath(str(left))) == os.path.normcase(os.path.realpath(str(right)))


def _consecutive_runs(chapters):
    runs = []
    for chapter in sorted(chapters, key=lambda item: item.order):
        if runs and chapter.order == runs[-1][-1].order + 1:
            runs[-1].append(chapter)
        elif runs and chapter.order == runs[-1][-1].order:
            raise ProviderError(f"章節順序重複：{chapter.order}")
        else:
            runs.append([chapter])
    return runs


# --- Tomato bridge --------------------------------------------------------

class TomatoBridgeProvider(FullTextProvider):
    """Runs the user's TomatoNovelDownloader EXE as a private local server."""

    name = "tomato-bridge"

    def __init__(self, exe_path, root=None, port=DEFAULT_PORT, port_scan=PORT_SCAN,
                 startup_timeout=90.0, poll_interval=2.0, spawn=None):
        self.exe_path = str(exe_path)
        self.root = Path(root) if root else prepare_app_data() / "fanqie-bridge"
        self.port = port
        self.port_scan = port_scan
        self.startup_timeout = startup_timeout
        self.poll_interval = poll_interval
        self._spawn = spawn or subprocess.Popen
        self._proc = None
        self._base = None
        self._server_lock = threading.RLock()
        self._job_lock = threading.Lock()

    # paths
    @property
    def data_dir(self):
        return self.root / "data"

    @property
    def library_dir(self):
        return self.root / "library"

    # process lifecycle
    def _port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    def _pick_port(self):
        # Never take over an instance the user started themselves.
        for port in range(self.port, self.port + self.port_scan):
            if not self._port_in_use(port):
                return port
        raise ProviderError(
            f"本機 {self.port}~{self.port + self.port_scan - 1} 埠都已被占用，無法啟動 Tomato bridge。")

    def _write_config(self):
        """Seed only the keys the bridge depends on; keep anything else Tomato wrote."""
        wanted = {
            "novel_format": "txt",
            "ask_format_after_download": "true",
            "save_path": "'" + str(self.library_dir).replace("'", "''") + "'",
        }
        path = self.data_dir / "config.yml"
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except (OSError, UnicodeError) as exc:
            raise ProviderError(f"無法讀取 Tomato 設定檔 {path}：{exc}") from exc
        lines = text.splitlines()
        for key, value in wanted.items():
            pattern = re.compile(rf"^{re.escape(key)}\s*:")
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = f"{key}: {value}"
                    break
            else:
                lines.append(f"{key}: {value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ensure_running(self, cancel_check=None):
        with self._server_lock:
            if self._proc is not None and self._proc.poll() is None and self._base:
                return
            self._proc = self._base = None
            exe = validate_exe_path(self.exe_path)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.library_dir.mkdir(parents=True, exist_ok=True)
            self._write_config()
            port = self._pick_port()
            env = os.environ.copy()
            env.pop("TOMATO_WEB_PASSWORD", None)
            env["TOMATO_WEB_ADDR"] = f"127.0.0.1:{port}"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            with open(self.root / "bridge.log", "ab") as log:
                try:
                    proc = self._spawn(
                        [str(exe), "--server", "--data-dir", str(self.data_dir)],
                        cwd=str(self.library_dir), env=env, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
                except OSError as exc:
                    raise ProviderError(f"無法啟動 Tomato EXE：{exc}") from exc
            self._proc = proc
            base = f"http://127.0.0.1:{port}"
            try:
                self._wait_ready(proc, base, cancel_check)
            except BaseException:
                self._stop_process()
                raise
            self._base = base

    def _wait_ready(self, proc, base, cancel_check):
        deadline = time.monotonic() + self.startup_timeout
        while True:
            if cancel_check and cancel_check():
                raise ProviderCancelled("使用者中止，Tomato bridge 啟動已取消。")
            if proc.poll() is not None:
                raise ProviderError(f"Tomato EXE 啟動後立即結束（代碼 {proc.poll()}），詳見 {self.root / 'bridge.log'}")
            try:
                status = self._request("GET", "/api/status", base=base, timeout=3)
            except ProviderError:
                status = None
            if isinstance(status, dict):
                if status.get("prewarm_error"):
                    raise ProviderError(f"Tomato 預熱失敗：{status['prewarm_error']}")
                if not status.get("prewarm_in_progress"):
                    if not _same_path(status.get("save_dir"), self.library_dir):
                        raise ProviderError("連到的 Tomato 服務不是本程式啟動的實例（儲存位置不符），已停止。")
                    return
            if time.monotonic() > deadline:
                raise ProviderError(f"等待 Tomato 服務就緒逾時（{self.startup_timeout:.0f} 秒）。")
            time.sleep(0.5)

    def _stop_process(self):
        proc, self._proc, self._base = self._proc, None, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                logger.warning("Cannot stop Tomato bridge process", exc_info=True)

    def close(self):
        """Terminate only the process this provider started."""
        with self._server_lock:
            self._stop_process()

    # HTTP
    def _request(self, method, path, payload=None, base=None, timeout=15):
        url = (base or self._base) + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, OSError, UnicodeError) as exc:
            raise ProviderError(f"Tomato 服務沒有回應（{path}）：{exc}") from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise ProviderError(f"Tomato 服務回應不是有效 JSON（{path}）。") from exc

    # jobs
    def fetch_chapters(self, book_id, chapters, cancel_check=None, progress=None):
        book_id = str(book_id)
        if not re.fullmatch(r"\d+", book_id):
            raise ProviderError("bookId 格式錯誤。")
        chapters = list(chapters)
        if not chapters:
            return {}
        runs = _consecutive_runs(chapters)
        with self._job_lock:
            self._ensure_running(cancel_check)
            result = {}
            for run in runs:
                result.update(self._run_range(book_id, run, cancel_check, progress))
            return result

    def _clean_book_state(self, book_id):
        """Drop leftovers of earlier jobs so an export holds exactly this range."""
        library = self.library_dir.resolve()
        folder = (library / book_id).resolve()
        if folder.parent == library and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        for path in library.glob("*.txt"):
            try:
                if txt_book_id(path.read_text(encoding="utf-8", errors="replace")) == book_id:
                    path.unlink()
            except OSError:
                pass

    def _find_output(self, book_id):
        matches = []
        for path in self.library_dir.glob("*.txt"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if txt_book_id(text) == book_id:
                matches.append((path.stat().st_mtime, path, text))
        if not matches:
            raise ProviderError("Tomato 已完成但找不到對應的 TXT 輸出（可能輸出格式不是 txt）。")
        _mtime, path, text = max(matches, key=lambda entry: entry[0])
        return path, text

    def _run_range(self, book_id, run, cancel_check, progress):
        start, end = run[0].order, run[-1].order
        self._clean_book_state(book_id)
        created = self._request("POST", "/api/jobs", {
            "book_id": book_id, "range_start": start, "range_end": end})
        job_id = created.get("id") if isinstance(created, dict) else None
        if type(job_id) is not int:
            raise ProviderError("Tomato 建立任務的回應缺少任務編號。")
        if progress:
            progress(f"[Tomato] 已建立任務，第 {start}~{end} 章（{len(run)} 章）")
        deadline = time.monotonic() + 600 + 60 * len(run)
        format_sent = False
        while True:
            if cancel_check and cancel_check():
                self._cancel_job(job_id)
                raise ProviderCancelled("使用者中止，已取消 Tomato 任務。")
            reply = self._request("GET", f"/api/jobs?id={job_id}")
            items = reply.get("items") if isinstance(reply, dict) else None
            item = next((entry for entry in items or []
                         if isinstance(entry, dict) and entry.get("id") == job_id), None)
            if item is None:
                raise ProviderError("Tomato 任務清單找不到剛建立的任務。")
            state = str(item.get("state") or "").lower()
            options = item.get("format_options")
            if options and not format_sent:
                values = [entry.get("value") for entry in options if isinstance(entry, dict)]
                if "txt" not in values:
                    self._cancel_job(job_id)
                    raise ProviderError("Tomato 提供的輸出格式選項沒有 txt。")
                self._request("POST", f"/api/jobs/{job_id}/format", {"value": "txt"})
                format_sent = True
            if state == "done":
                break
            if state in FAILED_STATES:
                raise ProviderError(f"Tomato 任務失敗（{state}）：{item.get('message') or '未提供原因'}")
            if time.monotonic() > deadline:
                self._cancel_job(job_id)
                raise ProviderError("等待 Tomato 任務完成逾時，已取消。")
            if progress and isinstance(item.get("progress"), dict):
                saved, total = item["progress"].get("saved_chapters"), item["progress"].get("chapter_total")
                if saved is not None and total:
                    progress(f"[Tomato] 第 {start}~{end} 章：{saved}/{total}")
            time.sleep(self.poll_interval)
        path, text = self._find_output(book_id)
        bodies = parse_tomato_txt(text, run)
        try:
            path.unlink()
        except OSError:
            logger.warning("Cannot remove bridge output %s", path)
        return bodies

    def _cancel_job(self, job_id):
        try:
            self._request("POST", f"/api/jobs/{job_id}/cancel", {}, timeout=5)
        except ProviderError:
            logger.warning("Cannot cancel Tomato job %s", job_id)


# --- registry -------------------------------------------------------------

_providers = {}
_providers_lock = threading.Lock()


def get_tomato_provider(exe_path):
    """One provider (and at most one server process) per EXE path."""
    key = os.path.normcase(os.path.abspath(str(exe_path)))
    with _providers_lock:
        provider = _providers.get(key)
        if provider is None:
            provider = _providers[key] = TomatoBridgeProvider(exe_path)
        return provider


def shutdown_providers():
    with _providers_lock:
        providers = list(_providers.values())
        _providers.clear()
    for provider in providers:
        provider.close()


atexit.register(shutdown_providers)
