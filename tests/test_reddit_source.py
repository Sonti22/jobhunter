"""Reddit hiring-посты через официальный API и живучесть IMAP при обрывах сети."""
import json
import socket

import httpx
import pytest

LISTING = {"data": {"children": [
    {"data": {"id": "a1", "title": "[Hiring] Senior Python Backend Engineer (remote)",
              "link_flair_text": "Hiring", "created_utc": 1789000000,
              "permalink": "/r/forhire/comments/a1/x/",
              "selftext": "We're hiring a backend engineer. Python, FastAPI, Postgres. "
                          "Remote, full-time. Send your CV to jobs@acme.dev"}},
    {"data": {"id": "a2", "title": "[For Hire] Python developer looking for remote work",
              "link_flair_text": "For Hire", "created_utc": 1789000001,
              "permalink": "/r/forhire/comments/a2/x/",
              "selftext": "Open to work. Contact me at dev@example.com"}},
    {"data": {"id": "a3", "title": "[Hiring] DevOps engineer, Kubernetes",
              "link_flair_text": "Hiring", "created_utc": 1789000002,
              "permalink": "/r/forhire/comments/a3/x/",
              "selftext": "DM me for details. Remote."}},
    {"data": {"id": "a4", "title": "[Hiring] Backend dev",
              "link_flair_text": "Hiring", "created_utc": 1789000003,
              "permalink": "/r/forhire/comments/a4/x/",
              "selftext": "Looking for a new job, open to work. reach me at me@seeker.io"}},
    {"data": {"id": "a5", "title": "[Hiring] Registered Nurse, night shifts",
              "link_flair_text": "Hiring", "created_utc": 1789000004,
              "permalink": "/r/forhire/comments/a5/x/",
              "selftext": "Apply: hr@clinic.com"}},
    {"data": {"id": "a6", "title": "Hiring: Python engineer (no tag, flair only)",
              "link_flair_text": "Hiring - Remote", "created_utc": 1789000005,
              "permalink": "/r/forhire/comments/a6/x/",
              "selftext": "Python + Django. Email talent@startup.io"}},
]}}


@pytest.fixture()
def creds(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "cid")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "sec")
    monkeypatch.setenv("REDDIT_SUBREDDITS", "forhire,pythonjobs")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    from jobhunter.ingest import reddit
    monkeypatch.setattr(reddit.time, "sleep", lambda *a: None)
    yield
    get_settings.cache_clear()


def _client(calls, second_sub_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/api/v1/access_token":
            assert request.headers["user-agent"].startswith("windows:jobhunter")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "bearer tok"
        if request.url.path == "/r/forhire/search":
            assert request.url.params["restrict_sr"] == "1"
            return httpx.Response(200, json=LISTING)
        return httpx.Response(second_sub_status, headers={"retry-after": "1"},
                              json={"data": {"children": []}})
    return httpx.Client(transport=httpx.MockTransport(handler),
                        headers={"User-Agent": "windows:jobhunter:0.1 (test)"})


def test_only_hiring_posts_with_email_become_jobs(creds):
    from jobhunter.ingest.reddit import RedditHiringSource
    calls = []
    jobs = list(RedditHiringSource(http=_client(calls)).iter_jobs())
    assert [j.external_uuid for j in jobs] == ["reddit:a1", "reddit:a6"]
    j = jobs[0]
    assert (j.title, j.contact_kind, j.contact_email) == \
        ("Senior Python Backend Engineer (remote)", "email", "jobs@acme.dev")
    assert j.posted_at == 1789000000 and j.source == "reddit"
    assert j.all_links[0]["value"] == "https://www.reddit.com/r/forhire/comments/a1/x/"
    assert j.has_direct_contact
    assert [c[1] for c in calls] == ["/api/v1/access_token", "/r/forhire/search", "/r/pythonjobs/search"]


def test_rate_limited_subreddit_is_skipped_not_fatal(creds):
    from jobhunter.ingest.reddit import RedditHiringSource
    calls = []
    jobs = list(RedditHiringSource(http=_client(calls, second_sub_status=429)).iter_jobs())
    assert len(jobs) == 2
    assert sum(1 for c in calls if c[1] == "/r/pythonjobs/search") == 2   # один повтор


def test_without_credentials_source_is_silent(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    try:
        from jobhunter.ingest.jobapis import SOURCES
        from jobhunter.ingest.reddit import RedditHiringSource
        assert SOURCES["reddit"] is RedditHiringSource
        calls = []
        src = RedditHiringSource(http=_client(calls))
        assert not src.enabled and list(src.iter_jobs()) == [] and calls == []
    finally:
        get_settings.cache_clear()


def test_for_hire_tag_marks_a_seeker():
    from jobhunter.ingest.postkind import is_seeker_post
    assert is_seeker_post("[For Hire] Python developer, 5 years, remote only\nStack: Django")
    assert not is_seeker_post("[Hiring] Python developer for our team\nSend CV to jobs@x.io")


# ── IMAP: короткие обрывы сети переживаются внутри прохода ──

def test_imap_connect_retries_transient_errors(monkeypatch):
    from jobhunter.convo import imapbox
    monkeypatch.setenv("SMTP_USER", "u@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "p")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    attempts, slept = [], []
    monkeypatch.setattr(imapbox.time, "sleep", slept.append)

    class Conn:
        def login(self, *a):
            return "OK", []

    def fake_ssl(*a, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise socket.gaierror(-3, "Temporary failure in name resolution")
        if len(attempts) == 2:
            raise TimeoutError("_ssl.c:993: The handshake operation timed out")
        return Conn()
    monkeypatch.setattr(imapbox.imaplib, "IMAP4_SSL", fake_ssl)
    try:
        assert isinstance(imapbox.connect(), Conn)
        assert len(attempts) == 3 and slept == [20.0, 40.0]
    finally:
        get_settings.cache_clear()


def test_imap_login_error_is_not_retried(monkeypatch):
    from jobhunter.convo import imapbox
    monkeypatch.setenv("SMTP_USER", "u@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "p")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    attempts = []
    monkeypatch.setattr(imapbox.time, "sleep", lambda *a: pytest.fail("не ждать"))

    def fake_ssl(*a, **kw):
        attempts.append(1)
        raise imapbox.imaplib.IMAP4.error("[ALERT] Web login required")
    monkeypatch.setattr(imapbox.imaplib, "IMAP4_SSL", fake_ssl)
    try:
        with pytest.raises(imapbox.MailboxError, match="Web login"):
            imapbox.connect()
        assert len(attempts) == 1
    finally:
        get_settings.cache_clear()


def test_listing_shape_survives_json_roundtrip():
    """Страховка от опечатки в фикстуре: структура ровно как у Reddit."""
    data = json.loads(json.dumps(LISTING))
    assert all("title" in c["data"] and "selftext" in c["data"] for c in data["data"]["children"])
