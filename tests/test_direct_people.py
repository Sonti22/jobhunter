"""Адресаты прямых писем: только опубликованные адреса, у каждого — источник."""
import httpx
import pytest

from jobhunter.ingest import people

TEAM_PAGE = """<html><body>
<h1>Our team</h1>
<div class="card"><h3>Anna Lee</h3><p>Co-founder & CEO</p>
  <a href="mailto:anna@acme.io">anna@acme.io</a></div>
<div class="card"><h3>Bob Ray</h3><p>Office manager</p><a href="mailto:bob@acme.io">bob@acme.io</a></div>
<p>Want to join? Write to <a href="mailto:jobs@acme.io?subject=Hi">jobs@acme.io</a></p>
<footer>Privacy questions: privacy@acme.io · Support: support@acme.io ·
 Built with wix: hello@wix.com · press@gmail.com
 <a href="/cdn-cgi/l/email-protection#1b7a7f76727">[email&#160;protected]</a></footer>
</body></html>"""


def _fetcher(pages: dict, robots: str = ""):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200 if robots else 404, text=robots)
        if path in pages:
            return httpx.Response(200, text=pages[path], headers={"content-type": "text/html"})
        return httpx.Response(404, text="nope")
    http = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": people.UA})
    return people.Fetcher(http=http, throttle=0), calls


def test_page_gives_only_company_hiring_and_exec_addresses():
    found = people.contacts_from_html(TEAM_PAGE, "https://acme.io/team", "Acme")
    got = {c.email: c.kind for c in found}
    assert got == {"anna@acme.io": "exec", "jobs@acme.io": "hiring"}
    # рядовой сотрудник, служебные ящики, чужой домен, freemail и спрятанный адрес — мимо
    assert found[0].email == "anna@acme.io" and "CEO" in found[0].person_role
    assert all(c.source_url == "https://acme.io/team" for c in found)


def test_crawl_respects_robots_and_page_budget():
    fetcher, calls = _fetcher({"/": "<p>hi</p>", "/team": TEAM_PAGE, "/about": "<p>x</p>"},
                              robots="User-agent: *\nDisallow: /team\n")
    found = people.find_company_contacts("https://acme.io", "Acme", fetcher)
    assert found == []                                   # /team закрыт — адресов нет
    assert not any(u.endswith("/team") for u in calls)
    fetcher, calls = _fetcher({"/": "<p>hi</p>", "/team": TEAM_PAGE})
    found = people.find_company_contacts("https://acme.io", "Acme", fetcher)
    assert found[0].kind == "exec"
    assert len([u for u in calls if "robots" not in u]) <= people.MAX_ATTEMPTS


def test_unreachable_robots_means_do_not_crawl():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        return httpx.Response(200, text=TEAM_PAGE, headers={"content-type": "text/html"})
    f = people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), throttle=0)
    assert people.find_company_contacts("https://acme.io", "Acme", f) == []


@pytest.mark.parametrize("text,site", [
    ("Apply at https://jobs.ashbyhq.com/stream/1 — more on https://www.getstream.io/careers",
     "https://getstream.io"),
    ("https://t.me/foo https://linkedin.com/company/x", ""),
    ("We are Acme (https://acme.io). Apply: https://boards.greenhouse.io/acme", "https://acme.io"),
])
def test_company_site_is_not_a_job_board(text, site):
    assert people.site_of(text) == site


def _github(users: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/users":
            return httpx.Response(200, json={"items": [{"login": k} for k in users]})
        login = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=users[login]) if login in users else httpx.Response(404)
    return people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), throttle=0)


def test_github_takes_only_self_published_email_with_hiring_bio(monkeypatch):
    monkeypatch.setattr(people.time, "sleep", lambda *a: None)
    users = {
        "founder1": {"login": "founder1", "name": "Ann", "email": "ann@startup.dev",
                     "company": "@startup", "bio": "Founder & CTO at Startup. We're hiring Python engineers"},
        "hidden": {"login": "hidden", "email": None, "bio": "CTO, hiring"},
        "nohire": {"login": "nohire", "email": "x@corp.com", "bio": "Backend dev"},
        "eng": {"login": "eng", "name": "Raj", "email": "raj@gmail.com", "company": "BigCo",
                "bio": "Staff engineer @BigCo — my team is hiring!"},
    }
    found = people.github_people(limit=10, fetcher=_github(users), queries=("hiring in:bio",))
    got = {c.email: (c.kind, c.company) for c in found}
    assert got == {"ann@startup.dev": ("exec", "startup"), "raj@gmail.com": ("referral", "BigCo")}
    assert found[0].source_url == "https://api.github.com/users/founder1"


def test_provenance_recheck(monkeypatch):
    fetcher, _ = _fetcher({"/team": TEAM_PAGE})
    assert people.page_publishes("anna@acme.io", "https://acme.io/team", fetcher)
    assert not people.page_publishes("ghost@acme.io", "https://acme.io/team", fetcher)
    gh = _github({"ann": {"login": "ann", "email": "ann@startup.dev", "bio": "hiring"}})
    assert people.page_publishes("ann@startup.dev", "https://api.github.com/users/ann", gh)
    assert not people.page_publishes("other@startup.dev", "https://api.github.com/users/ann", gh)
