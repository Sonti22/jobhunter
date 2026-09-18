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


@pytest.mark.parametrize("company,text,site", [
    ("scaleai", "Know your rights: https://www.eeoc.gov/poster More: https://scale.com/careers", "https://scale.com"),
    ("Abnormalsecurity", "Logo https://assets.contentstack.io/v3/a.png", ""),
    ("payabl.", "Visit https://payabl.com/about", "https://payabl.com"),
    ("Capstoneinvestmentadvisors", "See https://www.capstoneco.com", "https://capstoneco.com"),
    ("Doctolib", "Apply on https://careers.doctolib.com/jobs/1", "https://careers.doctolib.com"),
    ("intercom", "Try our product https://fin.ai today", ""),
])
def test_company_site_must_match_company_name(company, text, site):
    """Сухой прогон 18.09: eeoc.gov и CDN с картинками принимались за сайт работодателя."""
    assert people.site_of(text, company=company) == site


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


def test_github_skips_recruiters_service_mailboxes_and_stops_on_rate_limit(monkeypatch):
    """Сухой прогон 18.09: три адресата из трёх — кадровые агентства, один — support@."""
    monkeypatch.setattr(people.time, "sleep", lambda *a: None)
    users = {
        "agency": {"login": "organichire", "email": "hello@organichire.co",
                   "bio": "We're hiring! Recruiting agency for Python devs"},
        "support": {"login": "corp", "email": "support@corp.dev", "bio": "CTO. We're hiring"},
        "hr": {"login": "kate", "email": "kate@corp.dev", "bio": "HR at Corp, hiring backend"},
        "good": {"login": "good", "name": "Lee", "email": "lee@corp.dev", "bio": "CTO. We're hiring"},
    }
    found = people.github_people(limit=10, fetcher=_github(users), queries=("hiring in:bio",))
    assert [c.email for c in found] == ["lee@corp.dev"]

    calls = []

    def limited(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/search/users":
            return httpx.Response(200, json={"items": [{"login": "u%d" % i} for i in range(20)]})
        return httpx.Response(403, json={"message": "API rate limit exceeded"})
    f = people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(limited)), throttle=0)
    assert people.github_people(limit=10, fetcher=f) == []
    assert len(calls) == 2                               # поиск + первый отказ, дальше не ходим


def _org_api(org: dict, members: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/users":
            return httpx.Response(200, json={"items": [{"login": org["login"]}]})
        if path == "/orgs/" + org["login"]:
            return httpx.Response(200, json=org)
        if path.endswith("/public_members"):
            return httpx.Response(200, json=[{"login": k} for k in members])
        login = path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=members[login]) if login in members else httpx.Response(404)
    return people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), throttle=0)


def test_github_org_gives_leaders_of_the_target_company_only(monkeypatch):
    monkeypatch.setattr(people.time, "sleep", lambda *a: None)
    members = {
        "cto": {"login": "cto", "name": "Mia Wong", "email": "mia@acme.io", "bio": "CTO at Acme"},
        "dev": {"login": "dev", "email": "dev@gmail.com", "bio": "I like Rust"},
        "lead": {"login": "lead", "name": "Raj", "email": "raj@gmail.com",
                 "bio": "Engineering at Acme — we're hiring!"},
        "quiet": {"login": "quiet", "email": None, "bio": "CEO"},
        # живая проверка 18.09: инженер с таким био размечался как основатель компании
        "eng": {"login": "eng", "name": "Charles", "email": "me@charles.dev", "company": "@Acme",
                "bio": "Minecraft OG, Engineer, Founder. Building self-driving products @Acme"},
        "gone": {"login": "gone", "email": "old@acme.io", "bio": "Former CTO at Acme, now sailing"},
    }
    f = _org_api({"login": "acme", "blog": "https://www.acme.io"}, members)
    found = people.github_org_people("Acme", "https://acme.io", f)
    assert [(c.email, c.kind, c.company) for c in found] == \
        [("mia@acme.io", "exec", "Acme"), ("raj@gmail.com", "referral", "Acme")]
    # организация с чужим сайтом — не наша компания, её участников не трогаем
    f = _org_api({"login": "acme", "blog": "https://acme-tools.org"}, members)
    assert people.github_org_people("Acme", "https://acme.io", f) == []


def test_web_search_only_points_at_pages_address_must_be_on_the_page():
    interview = """<p>Interview with Anna Lee, CEO of Acme. Reach her at anna@acme.io</p>
    <p>Editor: bob@techblog.com. Acme support: help@acme.io, intern tom@acme.io</p>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            assert request.method == "POST" and request.headers["Authorization"] == "Bearer key"
            assert b"@acme.io" in request.content
            return httpx.Response(200, json={"results": [
                {"url": "https://techblog.com/anna"}, {"url": "https://linkedin.com/in/anna"},
                {"url": "https://empty.com/x"}]})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "techblog.com":
            return httpx.Response(200, text=interview, headers={"content-type": "text/html"})
        assert request.url.host != "linkedin.com"          # за логином — не ходим вовсе
        return httpx.Response(200, text="<p>nothing</p>", headers={"content-type": "text/html"})
    f = people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), throttle=0)
    found = people.search_people("Acme", "https://acme.io", "key", f)
    assert [(c.email, c.kind, c.source_url) for c in found] == \
        [("anna@acme.io", "exec", "https://techblog.com/anna")]
    assert people.search_people("Acme", "https://acme.io", "", f) == []      # без ключа молчит


SITEMAP_INDEX = """<?xml version="1.0"?><sitemapindex>
<sitemap><loc>https://acme.io/sitemap-posts.xml</loc></sitemap>
<sitemap><loc>https://acme.io/sitemap-pages.xml</loc></sitemap></sitemapindex>"""
SITEMAP_PAGES = """<?xml version="1.0"?><urlset>
<url><loc>https://acme.io/pricing</loc></url>
<url><loc>https://acme.io/en/company/leadership/</loc></url>
<url><loc>https://acme.io/blog/2024/our-team-offsite/photos/day-one</loc></url>
<url><loc>https://cdn.other.com/team</loc></url>
<url><loc>https://acme.io/legal/impressum</loc></url></urlset>"""


def test_sitemap_shows_where_the_people_pages_really_are():
    """Сухой прогон 18.09: восемь угаданных путей на сайт — и ни одной страницы с людьми."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        xml = {"/sitemap.xml": SITEMAP_INDEX, "/sitemap-pages.xml": SITEMAP_PAGES}
        if path == "/robots.txt":
            return httpx.Response(404)
        if path in xml:
            return httpx.Response(200, text=xml[path], headers={"content-type": "application/xml"})
        assert path != "/sitemap-posts.xml"                  # карту блога не читаем
        if path == "/en/company/leadership/":
            return httpx.Response(200, text=TEAM_PAGE, headers={"content-type": "text/html"})
        return httpx.Response(404)
    f = people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), throttle=0)
    assert people.sitemap_pages("https://acme.io", f) == \
        ["https://acme.io/en/company/leadership/", "https://acme.io/legal/impressum"]
    found = people.find_company_contacts("https://acme.io", "Acme", f)
    assert found[0].email == "anna@acme.io"
    assert found[0].source_url == "https://acme.io/en/company/leadership/"


def _hn(hits: list, items: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hn.algolia.com"
        if request.url.path.endswith("/search_by_date"):
            assert request.url.params["tags"] == "comment"
            assert "created_at_i>" in request.url.params["numericFilters"]
            return httpx.Response(200, json={"hits": hits})
        return httpx.Response(200, json=(items or {}).get(request.url.path.rsplit("/", 1)[-1], {}))
    return people.Fetcher(http=httpx.Client(transport=httpx.MockTransport(handler)), throttle=0)


def test_hn_comment_gives_address_the_author_left_for_applicants():
    hits = [
        {"objectID": "101", "author": "mia", "story_title": "Ask HN: Who is hiring? (August 2026)",
         "comment_text": "Acme | Backend | Remote<p>We&#x27;re hiring. Email me: mia@acme.io"},
        {"objectID": "102", "author": "rnd", "story_title": "Show HN: a thing",
         "comment_text": "I once got spam from tom@acme.io and sales@acme.io, write hello@acme.io"},
        {"objectID": "103", "author": "cto", "story_title": "Some thread",
         "comment_text": "I&#x27;m the CTO of Acme — reach me at lee@acme.io or lee at gmail"},
    ]
    found = people.hn_people("Acme", "https://www.acme.io", _hn(hits))
    got = {c.email: (c.kind, c.source_url) for c in found}
    assert got == {"lee@acme.io": ("exec", "https://news.ycombinator.com/item?id=103"),
                   "mia@acme.io": ("referral", "https://news.ycombinator.com/item?id=101")}
    assert people.hn_people("Acme", "", _hn(hits)) == []              # без сайта домен не известен
    # перепроверка перед отправкой: адрес всё ещё стоит в комментарии
    live = _hn([], {"101": {"text": "Email me: mia@acme.io"}, "103": {"text": "[removed]"}})
    assert people.page_publishes("mia@acme.io", "https://news.ycombinator.com/item?id=101", live)
    assert not people.page_publishes("lee@acme.io", "https://news.ycombinator.com/item?id=103", live)


def test_crawl_goes_to_root_domain_not_careers_subdomain():
    fetcher, calls = _fetcher({"/team": TEAM_PAGE})
    people.find_company_contacts("https://careers.acme.io", "Acme", fetcher)
    assert calls and all("//acme.io/" in u for u in calls)


def test_provenance_recheck(monkeypatch):
    fetcher, _ = _fetcher({"/team": TEAM_PAGE})
    assert people.page_publishes("anna@acme.io", "https://acme.io/team", fetcher)
    assert not people.page_publishes("ghost@acme.io", "https://acme.io/team", fetcher)
    gh = _github({"ann": {"login": "ann", "email": "ann@startup.dev", "bio": "hiring"}})
    assert people.page_publishes("ann@startup.dev", "https://api.github.com/users/ann", gh)
    assert not people.page_publishes("other@startup.dev", "https://api.github.com/users/ann", gh)
