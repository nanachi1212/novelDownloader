from fetcher import FetchError, Fetcher


class FakeResponse:
    def __init__(self, body, status=200):
        self.content = body.encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


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
