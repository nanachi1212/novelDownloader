"""Cloudflare-bypass 抓取層:curl_cffi session、重試、編碼處理、限速、429 退避。"""
import random
import re
import threading
import time
from urllib.parse import urljoin

from curl_cffi import requests

META_CHARSET = re.compile(rb'charset=["\']?\s*([\w-]+)', re.I)
ENCODING_ALIAS = {"gb2312": "gbk", "gb-2312": "gbk", "big5": "big5hkscs"}
JS_REDIRECT = re.compile(
    r"window\.location\.href\s*=\s*(['\"])(?P<url>[^'\"]+)\1", re.I
)
MAX_JS_REDIRECTS = 5
MAX_BACKOFF_DELAY = 60.0      # 429 退避上限,避免一次卡住數分鐘
MAX_RATE_LIMIT_HITS = 6       # 單次請求最多容忍幾次 429
HUMAN_CHECK = re.compile(r"human verification|g-recaptcha|hcaptcha", re.I)


class Throttle:
    """同一本書的所有章節 worker 共用的退避狀態;一個 worker 被限速,全部一起放慢。"""

    def __init__(self, delay):
        self.lock = threading.Lock()
        self.base = delay
        self.delay = delay

    def back_off(self):
        with self.lock:
            self.delay = min(self.delay * 2, MAX_BACKOFF_DELAY)
            return self.delay

    def relax(self):
        """成功後緩慢回復,否則整本書會一直卡在最高退避值。"""
        with self.lock:
            if self.delay > self.base:
                self.delay = max(self.base, self.delay * 0.9)


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, encoding="utf-8", delay=2.0, impersonate="chrome131", headers=None,
                 timeout=20, throttle=None):
        # encoding=None 表示依回應自動偵測(HTTP 標頭 → meta charset → utf-8/gbk 試錯)
        self.session = requests.Session(impersonate=impersonate)
        self.encoding = encoding
        self.delay = delay
        self.last_url = None  # 自動當下一次請求的 Referer
        self.throttle = throttle or Throttle(delay)  # 429 退避狀態,可跨 worker 共用
        self.extra_headers = headers or {}
        self.timeout = max(1, float(timeout))

    @property
    def current_delay(self):
        return self.throttle.delay

    @current_delay.setter
    def current_delay(self, value):
        self.throttle.delay = value

    def get(self, url, referer=None, retries=5):
        headers = {
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        ref = referer or self.last_url
        if ref:
            headers["Referer"] = ref
        headers.update({k: v for k, v in self.extra_headers.items() if v})

        last_err = None
        attempt = 0
        rate_limit_hits = 0
        while attempt < retries:
            attempt += 1
            try:
                current_url = url
                for redirect_count in range(MAX_JS_REDIRECTS + 1):
                    r = self.session.get(current_url, headers=headers, timeout=self.timeout)
                    text = self._decode(r)

                    # Some reader sites return a tiny HTML page whose only useful action is
                    # a JavaScript redirect. Follow a short, bounded chain in the same session
                    # so cookies, rotating reader domains, and Referer keep working.
                    redirect = JS_REDIRECT.search(text) if len(text) < 10_000 else None
                    if r.status_code == 200 and redirect:
                        if redirect_count == MAX_JS_REDIRECTS:
                            raise FetchError(
                                f"JavaScript 跳轉超過 {MAX_JS_REDIRECTS} 次: {url}"
                            )
                        next_url = urljoin(current_url, redirect.group("url"))
                        if next_url == current_url:
                            raise FetchError(f"JavaScript 跳轉指向自身: {current_url}")
                        headers["Referer"] = current_url
                        current_url = next_url
                        continue
                    break

                if r.status_code == 429:
                    # 速率限制:整本書共用的延遲加倍後重試,且不算進一般重試次數
                    wait = self.throttle.back_off()
                    rate_limit_hits += 1
                    attempt -= 1
                    last_err = f"HTTP 429 速率限制,退避到 {wait:.1f}s"
                    if rate_limit_hits >= MAX_RATE_LIMIT_HITS:
                        break
                    time.sleep(wait)
                    continue

                if r.status_code == 200 and "Just a moment" not in text:
                    self.last_url = current_url
                    self.throttle.relax()
                    return text

                if r.status_code == 403 and HUMAN_CHECK.search(text):
                    # reCAPTCHA 這類人機驗證重試無用,直接結束並提示匯入 Cookie
                    last_err = ("HTTP 403 網站要求人機驗證,請先在瀏覽器完成驗證,"
                                "再用「網站 Cookie」把該網域的 Cookie 匯入後重試")
                    break

                if "Just a moment" in text:
                    last_err = f"HTTP {r.status_code}: 被 Cloudflare 挑戰"
                else:
                    last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)

            time.sleep(1.5 * attempt)

        raise FetchError(f"抓取失敗 {url}: {last_err}")

    def _decode(self, r):
        enc = self.encoding
        if not enc:
            m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", "") or "", re.I)
            if m:
                enc = m.group(1)
            else:
                m = META_CHARSET.search(r.content[:2048])
                if m:
                    enc = m.group(1).decode("ascii", "ignore")
        if enc:
            enc = ENCODING_ALIAS.get(enc.lower(), enc)
            try:
                return r.content.decode(enc, errors="replace")
            except LookupError:
                pass
        try:
            return r.content.decode("utf-8")
        except UnicodeDecodeError:
            return r.content.decode("gbk", errors="replace")

    def polite_sleep(self):
        # 隨機延遲,加入抖動避免同步請求
        delay = random.uniform(self.current_delay * 0.9, self.current_delay * 1.3)
        time.sleep(delay)
