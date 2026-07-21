"""Cloudflare-bypass 抓取層:curl_cffi session、重試、編碼處理、限速、429 退避。"""
import random
import time

from curl_cffi import requests


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, encoding="utf-8", delay=2.0, impersonate="chrome131"):
        self.session = requests.Session(impersonate=impersonate)
        self.encoding = encoding
        self.delay = delay
        self.last_url = None  # 自動當下一次請求的 Referer
        self.current_delay = delay  # 動態調整(429 時加倍)

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

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, headers=headers, timeout=20)
                text = r.content.decode(self.encoding, errors="replace")

                if r.status_code == 429:
                    # 速率限制:加倍延遲後重試
                    self.current_delay *= 2
                    last_err = f"HTTP 429 速率限制,退避到 {self.current_delay:.1f}s"
                    time.sleep(self.current_delay)
                    continue

                if r.status_code == 200 and "Just a moment" not in text:
                    self.last_url = url
                    return text

                if "Just a moment" in text:
                    last_err = f"HTTP {r.status_code}: 被 Cloudflare 挑戰"
                else:
                    last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)

            time.sleep(1.5 * attempt)

        raise FetchError(f"抓取失敗 {url}: {last_err}")

    def polite_sleep(self):
        # 隨機延遲,加入抖動避免同步請求
        delay = random.uniform(self.current_delay * 0.9, self.current_delay * 1.3)
        time.sleep(delay)
