"""用獨立的 Chrome 視窗讓使用者通過人機驗證,再經 DevTools Protocol 取回 Cookie。

新版 Chrome 的 Cookie 加密(app-bound encryption)需要系統管理員權限才能直接讀檔,
且人機驗證常用 session cookie,根本不會寫進磁碟。改成自己開一個乾淨的 Chrome、
連上它的 DevTools 埠拿 Cookie,兩個問題都繞開,也不用管理員權限。
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = (
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
    r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",
)
STARTUP_TIMEOUT = 20.0


class ChromeCookieError(RuntimeError):
    pass


def find_chrome():
    for pattern in CHROME_CANDIDATES:
        path = Path(os.path.expandvars(pattern))
        if path.exists():
            return path
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def cookies_to_header(cookies, host):
    """挑出屬於 host 的 Cookie,組成 request header 用的 `name=value; ...`。"""
    host = host.lower().lstrip(".")
    picked = {}
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lower().lstrip(".")
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        picked[cookie["name"]] = cookie.get("value", "")
    return "; ".join(f"{name}={value}" for name, value in picked.items())


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ChromeCookieSession:
    """開一個獨立 profile 的 Chrome;使用者驗證完後把 Cookie 抓回來。"""

    def __init__(self):
        self.process = None
        self.profile = None
        self.port = None

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None

    def open(self, url):
        self.close()
        browser = find_chrome()
        if browser is None:
            raise ChromeCookieError("找不到 Chrome 或 Edge,請改用手動貼上 Cookie")
        self.port = _free_port()
        self.profile = Path(tempfile.mkdtemp(prefix="novel_dl_chrome_"))
        self.process = subprocess.Popen([
            str(browser),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1000,800",
            url,
        ])
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                return self._debugger_url()
            except Exception:
                if not self.running:
                    break
                time.sleep(0.3)
        self.close()
        raise ChromeCookieError("瀏覽器沒有在時間內啟動,請再試一次或改用手動貼上 Cookie")

    def _debugger_url(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=2) as response:
            return json.load(response)["webSocketDebuggerUrl"]

    def cookie_header(self, host):
        if not self.running:
            raise ChromeCookieError("驗證視窗已關閉,請先按「開啟驗證視窗」")
        from websockets.sync.client import connect

        with connect(self._debugger_url(), max_size=None) as socket_:
            socket_.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            payload = json.loads(socket_.recv(timeout=10))
        cookies = payload.get("result", {}).get("cookies", [])
        return cookies_to_header(cookies, host)

    def close(self):
        if self.process is not None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                pass
            self.process = None
        if self.profile is not None:
            shutil.rmtree(self.profile, ignore_errors=True)  # 暫時 profile 不留在磁碟上
            self.profile = None
