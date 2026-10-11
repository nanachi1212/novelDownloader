from fetcher import FetchError, Fetcher


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.content = body.encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        if headers:
            self.headers.update(headers)


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        return next(self.responses)


def test_fetcher_follows_bounded_javascript_redirect_chain():
    fetcher = Fetcher()
    fetcher.session = FakeSession([
        FakeResponse("<script>window.location.href = '/step-2';</script>"),
        FakeResponse('<script>window.location.href = "https://reader.example/chapter";</script>'),
        FakeResponse("<html><body>最終正文</body></html>"),
    ])

    assert fetcher.get("https://catalog.example/step-1") == "<html><body>最終正文</body></html>"
    assert [call[0] for call in fetcher.session.calls] == [
        "https://catalog.example/step-1",
        "https://catalog.example/step-2",
        "https://reader.example/chapter",
    ]
    assert fetcher.session.calls[1][1]["Referer"] == "https://catalog.example/step-1"
    assert fetcher.session.calls[2][1]["Referer"] == "https://catalog.example/step-2"
    assert fetcher.last_url == "https://reader.example/chapter"


def test_fetcher_rejects_self_redirect_without_waiting(monkeypatch):
    fetcher = Fetcher()
    fetcher.session = FakeSession([
        FakeResponse("<script>window.location.href = '/same';</script>") for _ in range(5)
    ])
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)

    try:
        fetcher.get("https://catalog.example/same")
    except FetchError as error:
        assert "指向自身" in str(error)
    else:
        raise AssertionError("self redirect should fail")


def test_fetcher_uses_configured_timeout():
    fetcher = Fetcher(timeout=47)
    fetcher.session = FakeSession([FakeResponse("ok")])
    assert fetcher.get("https://example.test") == "ok"
    assert fetcher.session.calls[0][2] == 47



def test_rate_limit_backoff_is_shared_and_does_not_burn_retries(monkeypatch):
    """429 由整本書共用的 Throttle 退避，且不吃掉一般重試次數。"""
    from fetcher import Throttle

    slept = []
    monkeypatch.setattr("fetcher.time.sleep", lambda seconds: slept.append(seconds))
    throttle = Throttle(2.0)
    worker_a = Fetcher(delay=2.0, throttle=throttle)
    worker_b = Fetcher(delay=2.0, throttle=throttle)
    worker_a.session = FakeSession(
        [FakeResponse("slow down", status=429)] * 3 + [FakeResponse("正文")]
    )

    # retries=2 時，舊版會在第 2 次 429 就放棄；現在 429 不計入重試次數。
    assert worker_a.get("https://example.test/ch1", retries=2) == "正文"
    assert slept == [4.0, 8.0, 16.0]
    # 另一個 worker 看到的是同一個退避值（成功一次後已開始回復 16 → 14.4），
    # 不會繼續用原本的 2 秒全速請求。
    assert worker_b.current_delay == worker_a.current_delay == 14.4


def test_rate_limit_gives_up_after_repeated_429(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([FakeResponse("slow down", status=429)] * 20)

    try:
        fetcher.get("https://example.test/ch1", retries=5)
    except FetchError as error:
        assert "429" in str(error)
    else:
        raise AssertionError("persistent 429 should fail")
    assert len(fetcher.session.calls) == 6  # MAX_RATE_LIMIT_HITS
    assert fetcher.current_delay <= 60.0    # 退避有上限


def test_human_verification_403_stops_immediately_with_cookie_hint(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    fetcher = Fetcher()
    fetcher.session = FakeSession([
        FakeResponse("<title>Please complete human verification to continue</title>", status=403)
    ] * 5)

    try:
        fetcher.get("https://69shuba.tw/indexlist/1/", retries=5)
    except FetchError as error:
        assert "人機驗證" in str(error) and "Cookie" in str(error)
    else:
        raise AssertionError("human verification should fail fast")
    assert len(fetcher.session.calls) == 1  # 不再無謂重試


def test_successful_fetch_relaxes_backoff(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([FakeResponse("slow", status=429), FakeResponse("正文")])

    assert fetcher.get("https://example.test/ch1") == "正文"
    assert 2.0 <= fetcher.current_delay < 4.0  # 成功後開始回復，不會永遠卡在高延遲


def test_soft_block_200_page_is_treated_as_rate_limit_and_not_returned(monkeypatch):
    """200 但頁面其實是「訪問過於頻繁」之類的軟封鎖頁,不能被當成正文回傳。"""
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([
        FakeResponse("訪問過於頻繁,請稍後再試"),
        FakeResponse("<html><body>真正的章節正文</body></html>"),
    ])

    assert fetcher.get("https://example.test/ch1") == "<html><body>真正的章節正文</body></html>"
    assert len(fetcher.session.calls) == 2


def test_soft_block_visible_text_ignores_inline_script_and_style(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    block = (
        "<html><head><style>" + "x" * 250 + "</style></head>"
        "<body><script>" + "const padding='" + "x" * 500 + "';</script>"
        "<p>請稍後再試</p></body></html>"
    )
    expected = "<html><body><p>真正的章節正文</p></body></html>"
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([FakeResponse(block), FakeResponse(expected)])

    assert fetcher.get("https://example.test/ch1") == expected
    assert len(fetcher.session.calls) == 2


def test_soft_block_keyword_inside_script_does_not_reject_visible_chapter(monkeypatch):
    html = "<script>const retry = '請稍後再試';</script><p>短章節正文。</p>"
    fetcher = Fetcher()
    fetcher.session = FakeSession([FakeResponse(html)])

    assert fetcher.get("https://example.test/ch1") == html
    assert len(fetcher.session.calls) == 1


def test_soft_block_matches_html_entity_encoded_visible_message(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    block = "<html><body><p>&#35831;&#31245;&#21518;&#20877;&#35797;</p></body></html>"
    expected = "<html><body><p>真正的章節正文</p></body></html>"
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([FakeResponse(block), FakeResponse(expected)])

    assert fetcher.get("https://example.test/ch1") == expected
    assert len(fetcher.session.calls) == 2


def test_soft_block_does_not_false_positive_on_short_real_content(monkeypatch):
    """單純字數少的正文不能被誤判成軟封鎖頁(必須同時命中關鍵字才算)。"""
    fetcher = Fetcher()
    fetcher.session = FakeSession([FakeResponse("楔子。")])
    assert fetcher.get("https://example.test/ch1") == "楔子。"


def test_503_is_treated_like_429_backoff(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([FakeResponse("service unavailable", status=503), FakeResponse("正文")])

    assert fetcher.get("https://example.test/ch1") == "正文"


def test_retry_after_seconds_header_extends_wait(monkeypatch):
    slept = []
    monkeypatch.setattr("fetcher.time.sleep", lambda seconds: slept.append(seconds))
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([
        FakeResponse("slow", status=429, headers={"Retry-After": "10"}),
        FakeResponse("正文"),
    ])

    assert fetcher.get("https://example.test/ch1") == "正文"
    assert slept == [10.0]


def test_retry_after_http_date_header_is_parsed(monkeypatch):
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    import datetime
    fixed_now = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    class FixedDatetime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr("fetcher.datetime.datetime", FixedDatetime)
    retry_at = "Thu, 01 Jan 2026 00:00:20 GMT"  # 固定 now 之後 20 秒
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([
        FakeResponse("slow", status=503, headers={"Retry-After": retry_at}),
        FakeResponse("正文"),
    ])

    assert fetcher.get("https://example.test/ch1") == "正文"
    assert fetcher.current_delay <= 20.0


def test_retry_after_is_clamped_to_max_backoff_delay(monkeypatch):
    from fetcher import MAX_BACKOFF_DELAY

    slept = []
    monkeypatch.setattr("fetcher.time.sleep", lambda seconds: slept.append(seconds))
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([
        FakeResponse("slow", status=429, headers={"Retry-After": "99999"}),
        FakeResponse("正文"),
    ])

    assert fetcher.get("https://example.test/ch1") == "正文"
    assert slept == [MAX_BACKOFF_DELAY]


def test_soft_block_keyword_inside_a_real_short_chapter_is_not_treated_as_block(monkeypatch):
    """合法的短章節正文剛好出現「請稍後再試」這種句子,不能被當成限速頁反覆重試
    (Codex review 抓到的 bug):可見文字很多就代表是真內容,不是只有一句提示的封鎖頁。
    """
    monkeypatch.setattr("fetcher.time.sleep", lambda _seconds: None)
    body = "<html><body><p>" + "他低聲說:「請稍後再試,我們還有很多事情要談。」" + "夜色漸深,街燈一盞盞亮起。" * 30 + "</p></body></html>"
    fetcher = Fetcher(delay=2.0)
    fetcher.session = FakeSession([FakeResponse(body)])

    assert fetcher.get("https://example.test/ch1") == body
    assert len(fetcher.session.calls) == 1
