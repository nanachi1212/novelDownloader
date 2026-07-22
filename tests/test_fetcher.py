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
        self.calls.append((url, dict(headers)))
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

