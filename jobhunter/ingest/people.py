"""Адресаты прямых писем: руководители компаний и те, кто может зареферить.

Только адреса, опубликованные самим человеком или компанией, и у каждого —
URL, где он опубликован. Так решил владелец 18.09, и на этом держится
репутация ящика: прогрев почты встаёт при отбивках, а выдуманный адрес — это
отбивка. Поэтому здесь НЕТ и не должно появиться:

  - подбора адресов по шаблону (first.last@company);
  - проверки адресов зондированием почтовых серверов;
  - источников за логином (LinkedIn) и покупных баз;
  - расшифровки адресов, которые сайт спрятал от роботов (Cloudflare
    email-protection) — человек явно не хотел, чтобы адрес собирали.

Источники:
  сайт компании   страницы /team, /about, /leadership, /contact, /careers, /press —
                  с уважением к robots.txt, не больше четырёх страниц;
  GitHub          публичный профиль, где человек сам открыл email и пишет
                  в био, что нанимает.

    python -m jobhunter.ingest.people --site https://example.com
"""
from __future__ import annotations

import argparse
import html as _html
import re
import sys
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx

from ..convo.mailmatch import FREEMAIL, org_domain
from .base import _EMAIL, _valid_email, is_hiring_mailbox

UA = "jobhunter/0.1 (personal job search)"
PAGES = ("", "/team", "/about", "/leadership", "/contact", "/careers",
         "/about-us", "/company", "/jobs", "/press")
MAX_PAGES = 4
MAX_ATTEMPTS = 8
MAX_BYTES = 600_000

EXEC, HIRING, GENERAL, REFERRAL = "exec", "hiring", "general", "referral"
_RANK = {EXEC: 0, HIRING: 1, REFERRAL: 1, GENERAL: 2}

# Чужие площадки: ссылка на них в тексте вакансии — не сайт компании.
_NOT_COMPANY = re.compile(
    r"(?:^|\.)(?:ashbyhq|greenhouse|lever|workable|weworkremotely|jobicy|remoteok|himalayas|"
    r"arbeitnow|workingnomads|euremote|4dayweek|themuse|careered|hh|habr|linkedin|twitter|x|"
    r"facebook|instagram|youtube|youtu|github|gitlab|t|telegram|google|goo|notion|typeform|"
    r"calendly|medium|ycombinator|wellfound|angel|glassdoor|indeed|apple|bit|tinyurl|forms|"
    r"docs|airtable|crunchbase|reddit|discord|slack|zoom|teamtailor|recruitee|smartrecruiters|"
    r"bamboohr|breezy|personio|jobvite|myworkdayjobs|icims)\.[a-z.]+$", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

_EXEC_LOCAL = re.compile(r"^(?:ceo|cto|founders?|cofounders?|chief)\b", re.I)
_HIRING_LOCAL = re.compile(r"^(?:hr|jobs?|careers?|recruit\w*|talent|people|hiring|vacanc\w*|"
                           r"resume|cv|join|work)\b", re.I)
_GENERAL_LOCAL = re.compile(r"^(?:hello|hi|hey|team|contact|info|mail|say)\b", re.I)
_EXEC_ROLE = re.compile(
    r"\b(?:co-?founder|founder|ceo|chief\s+executive(?:\s+officer)?|cto|chief\s+technology\s+officer|"
    r"vp\s+(?:of\s+)?engineering|head\s+of\s+engineering|director\s+of\s+engineering|"
    r"engineering\s+manager)\b|генеральн\w+\s+директор|основател\w+|техническ\w+\s+директор|"
    r"руководител\w+\s+разработк\w+", re.I)
_HIRING_BIO = re.compile(r"\bhiring\b|нанимаем|ищем\s+в\s+команду", re.I)
_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
_MAILTO = re.compile(r"href=[\"']mailto:([^\"'?>\s]+)", re.I)


@dataclass
class Contact:
    email: str
    kind: str                 # exec | hiring | general | referral
    source_url: str           # где адрес опубликован — без этого контакта нет
    company: str = ""
    person: str = ""
    person_role: str = ""
    note: str = ""


# Хостинг картинок и файлов, госсайты и вузы: ссылки на них есть в каждой второй
# вакансии (логотип, сноска про EEO), но сайтом работодателя они не являются.
_NOT_A_SITE = re.compile(
    r"(?:contentstack|cloudfront|amazonaws|googleapis|googleusercontent|cloudinary|imgix|"
    r"akamai|fastly|cdn|static|assets|images?|media)\.|\.(?:gov|edu|mil)(?:\.[a-z]{2})?$", re.I)


def _belongs(host: str, company: str) -> bool:
    """Домен перекликается с названием компании: payabl.com ↔ «payabl.», capstoneco ↔ «Capstone…».

    Без этого сайтом Scale AI считался eeoc.gov из юридической сноски, а
    Abnormal Security — CDN с картинками (сухой прогон 18.09).
    """
    name = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    label = re.sub(r"[^a-z0-9]", "", (org_domain("x@" + host) or host).split(".")[0])
    if len(name) < 3 or len(label) < 3:
        return False
    n = min(len(name), len(label), 5)
    return name.startswith(label[:n]) or label.startswith(name[:n]) or label in name or name in label


def site_of(*texts: str, company: str = "") -> str:
    """Сайт компании из текста вакансии.

    С company — первая ссылка, чей домен перекликается с названием компании;
    без него — первая ссылка не на борд, ATS, соцсеть или хостинг файлов.
    """
    for text in texts:
        for m in _URL.finditer(text or ""):
            host = (urlparse(m.group(0).rstrip(".,;:")).hostname or "").lower()
            if not host or "." not in host or _NOT_COMPANY.search(host) or _NOT_A_SITE.search(host):
                continue
            if host.startswith("www."):
                host = host[4:]
            if company and not _belongs(host, company):
                continue
            return "https://" + host
    return ""


class Fetcher:
    """HTTP с уважением к robots.txt, паузой и пределом размера."""

    def __init__(self, http: httpx.Client | None = None, throttle: float = 1.5):
        self.http = http or httpx.Client(timeout=20.0, trust_env=False, follow_redirects=True,
                                         headers={"User-Agent": UA})
        self.throttle = throttle
        self._robots: dict = {}

    def allowed(self, url: str) -> bool:
        parts = urlparse(url)
        root = "%s://%s" % (parts.scheme, parts.netloc)
        rp = self._robots.get(root)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = self.http.get(root + "/robots.txt")
                # Нет robots.txt — обход разрешён; сервер недоступен — не лезем.
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                if r.status_code >= 500:
                    rp.parse(["User-agent: *", "Disallow: /"])
            except Exception:                              # noqa: BLE001
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots[root] = rp
        return rp.can_fetch(UA, url)

    def get(self, url: str) -> str:
        if not self.allowed(url):
            return ""
        time.sleep(self.throttle)
        try:
            r = self.http.get(url)
        except Exception:                                  # noqa: BLE001
            return ""
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or "html"):
            return ""
        return r.text[:MAX_BYTES]


def _classify(local: str, context: str) -> tuple:
    """(вид, должность) по локальной части адреса и тексту вокруг него."""
    if _EXEC_LOCAL.match(local):
        low = local.lower()
        return EXEC, "CEO" if low.startswith("ceo") else "CTO" if low.startswith("cto") else "founder"
    if _HIRING_LOCAL.match(local):
        return HIRING, ""
    roles = list(dict.fromkeys(m.group(0) for m in _EXEC_ROLE.finditer(context or "")))
    if roles:
        return EXEC, " & ".join(roles[:2])
    if _GENERAL_LOCAL.match(local):
        return GENERAL, ""
    return "", ""                     # адрес рядового сотрудника — не контакт для отклика


def contacts_from_html(page: str, page_url: str, company: str = "", org: str = "") -> list:
    """Опубликованные на странице адреса компании, пригодные для отклика.

    org — домен компании, когда страница чужая (интервью, сайт конференции).
    """
    from ..outreach.mailer import _mailbox_ok

    site_org = org or org_domain("x@" + (urlparse(page_url).hostname or ""))
    text = _html.unescape(_TAG.sub(" ", page or ""))
    found: dict = {}
    spots = [(m.group(1), m.start()) for m in _MAILTO.finditer(page or "")]
    spots += [(m.group(0), -1) for m in _EMAIL.finditer(text)]
    for raw, _pos in spots:
        addr = _html.unescape(raw).strip().rstrip(".,;:").lower()
        if addr in found or not _valid_email(addr):
            continue
        if not is_hiring_mailbox(addr) or not _mailbox_ok(addr):
            continue
        domain = addr.rsplit("@", 1)[-1]
        # Чужой домен на странице — это подрядчик, виджет или пример; личную
        # почту берём только там, где её публикует сам человек (GitHub).
        if domain in FREEMAIL or org_domain(addr) != site_org:
            continue
        # Должность относится к адресу, только если стоит в ЕГО карточке: текст
        # между предыдущим адресом и этим, плюс короткий хвост до следующего.
        # С широким окном офис-менеджер получал «CEO» от соседа по странице.
        i = text.lower().find(addr)
        context = ""
        if i >= 0:
            before = text[max(0, i - 140): i]
            prev = list(_EMAIL.finditer(before))
            if prev:
                before = before[prev[-1].end():]
            after = text[i + len(addr): i + len(addr) + 60]
            nxt = _EMAIL.search(after)
            context = before + " " + (after[:nxt.start()] if nxt else after)
        kind, role = _classify(addr.split("@")[0], context)
        if not kind:
            continue
        found[addr] = Contact(email=addr, kind=kind, source_url=page_url, company=company,
                              person_role=role, note=re.sub(r"\s+", " ", context)[:200])
    return sorted(found.values(), key=lambda c: (_RANK[c.kind], c.email))


def find_company_contacts(site: str, company: str = "", fetcher: Fetcher | None = None) -> list:
    """До четырёх страниц сайта. Возвращает контакты, лучший — первым."""
    fetcher = fetcher or Fetcher()
    out: dict = {}
    fetched = 0
    # Ссылка в вакансии часто ведёт на поддомен (careers.doctolib.com), а страницы
    # команды и контактов живут на основном домене — обходим его.
    root = org_domain("x@" + (urlparse(site).hostname or ""))
    if root and root != (urlparse(site).hostname or ""):
        site = "https://" + root
    for path in PAGES[:MAX_ATTEMPTS]:
        if fetched >= MAX_PAGES:
            break
        url = urljoin(site.rstrip("/") + "/", path.lstrip("/"))
        page = fetcher.get(url)
        if not page:
            continue
        fetched += 1
        for c in contacts_from_html(page, url, company):
            out.setdefault(c.email, c)
        if any(c.kind == EXEC for c in out.values()):
            break                                           # лучше уже не найдём
    return sorted(out.values(), key=lambda c: (_RANK[c.kind], c.email))


def page_publishes(email: str, source_url: str, fetcher: Fetcher | None = None) -> bool:
    """Перепроверка перед отправкой: адрес по-прежнему опубликован по этому URL."""
    if source_url.startswith("https://api.github.com/users/"):
        return email.lower() == (github_user(source_url.rsplit("/", 1)[-1],
                                             fetcher=fetcher).get("email") or "").lower()
    page = (fetcher or Fetcher()).get(source_url)
    return bool(page) and email.lower() in _html.unescape(page).lower()


# ── GitHub: человек сам открыл email и пишет, что нанимает ──

GITHUB_QUERIES = ("hiring in:bio python", "hiring in:bio backend",
                  "hiring in:bio founder", "hiring in:bio cto")


def _gh_headers() -> dict:
    from ..config import get_settings
    token = get_settings().github_token
    head = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        head["Authorization"] = "Bearer " + token
    return head


RATE_LIMITED = "_rate_limited"

# Кадровые агентства и HR-аккаунты заводят профили с «hiring» в био ради сбора
# откликов. Сухой прогон 18.09: organichire, brovate — три адресата из трёх.
_RECRUITER = re.compile(
    r"recruit|staffing|talent\s+(?:acquisition|partner|sourc)|head\s?hunt|\bhr\b|"
    r"human\s+resources|outsourc|outstaff|agency|рекрут|кадров|подбор\s+персонал", re.I)


def _gh_get(url: str, fetcher: Fetcher | None = None, params: dict | None = None):
    """JSON ответа GitHub; {RATE_LIMITED: True} на 403/429 — дальше ходить бесполезно."""
    http = (fetcher or Fetcher()).http
    try:
        r = http.get(url, params=params, headers=_gh_headers())
    except Exception:                                      # noqa: BLE001
        return {}
    if r.status_code in (403, 429):
        return {RATE_LIMITED: True}
    return r.json() if r.status_code == 200 else {}


def github_user(login: str, fetcher: Fetcher | None = None) -> dict:
    return _gh_get("https://api.github.com/users/" + login, fetcher) or {}


def _person_contact(u: dict, *, company: str = "", need_hiring: bool = True) -> Contact | None:
    """Профиль GitHub → адресат: email открыт самим человеком, ящик не служебный, не рекрутёр."""
    from ..outreach.mailer import _mailbox_ok

    email = (u.get("email") or "").strip().lower()
    bio = u.get("bio") or ""
    if not email or not _valid_email(email) or not is_hiring_mailbox(email) or not _mailbox_ok(email):
        return None
    if _RECRUITER.search(" ".join([bio, u.get("login") or "", u.get("company") or "",
                                   u.get("name") or "", email.rsplit("@", 1)[-1]])):
        return None
    role = _EXEC_ROLE.search(bio)
    if need_hiring and not _HIRING_BIO.search(bio):
        return None
    if not need_hiring and not role and not _HIRING_BIO.search(bio):
        return None                       # рядовой участник организации — не адресат
    login = u.get("login") or ""
    return Contact(
        email=email, kind=EXEC if role else REFERRAL,
        source_url="https://api.github.com/users/" + login,
        company=company or (u.get("company") or "").lstrip("@").strip(),
        person=(u.get("name") or login).strip(),
        person_role=role.group(0) if role else "", note=bio[:200])


def github_people(limit: int = 10, fetcher: Fetcher | None = None,
                  queries: tuple = GITHUB_QUERIES) -> list:
    """Профили с открытым email и «hiring» в био. 403/429 — молча прекращаем."""
    fetcher = fetcher or Fetcher()
    out: list = []
    seen: set = set()
    for q in queries:
        if len(out) >= limit:
            break
        # Сортировка по подписчикам: свежие аккаунты (sort=joined) — почти сплошь агентства.
        found = _gh_get("https://api.github.com/search/users", fetcher,
                        {"q": q + " type:user", "per_page": "20", "sort": "followers"})
        if not found or found.get(RATE_LIMITED):
            break
        for item in (found.get("items") or []):
            login = item.get("login") or ""
            if not login or login in seen or len(out) >= limit:
                continue
            seen.add(login)
            time.sleep(fetcher.throttle)
            u = github_user(login, fetcher=fetcher)
            if u.get(RATE_LIMITED):
                return out                                 # лимит исчерпан — не долбим дальше
            c = _person_contact(u)
            if c:
                out.append(c)
    return out


# ── GitHub-организация целевой компании: её публичные участники ──

ORG_MEMBERS = 15          # сколько профилей участников смотрим на одну компанию


def github_org_people(company: str, site: str = "", fetcher: Fetcher | None = None,
                      limit: int = 3) -> list:
    """Адресаты внутри самой компании: публичные участники её GitHub-организации.

    Организация принимается, только если её сайт совпадает с сайтом компании либо
    логин перекликается с названием. Берём руководителей и тех, у кого в био «hiring»,
    с открытым email; рядовых участников не трогаем.
    """
    fetcher = fetcher or Fetcher()
    name = re.sub(r"[^A-Za-z0-9 .-]", " ", company or "").strip()
    if len(name) < 3:
        return []
    found = _gh_get("https://api.github.com/search/users", fetcher,
                    {"q": name + " type:org", "per_page": "5"})
    if not found or found.get(RATE_LIMITED):
        return []
    site_org = org_domain("x@" + (urlparse(site).hostname or "")) if site else ""
    out: list = []
    for item in (found.get("items") or [])[:3]:
        login = item.get("login") or ""
        time.sleep(fetcher.throttle)
        org = _gh_get("https://api.github.com/orgs/" + login, fetcher)
        if not org or org.get(RATE_LIMITED):
            return out
        blog = (org.get("blog") or "").strip()
        if blog and "//" not in blog:
            blog = "https://" + blog
        blog_org = org_domain("x@" + (urlparse(blog).hostname or "")) if blog else ""
        same_site = bool(site_org) and blog_org == site_org
        if not same_site and not (not site_org and _belongs(login + ".x", company)):
            continue
        members = _gh_get("https://api.github.com/orgs/%s/public_members" % login, fetcher,
                          {"per_page": str(ORG_MEMBERS)})
        if isinstance(members, dict):                      # лимит или ошибка
            return out
        for m in members[:ORG_MEMBERS]:
            if len(out) >= limit:
                return out
            time.sleep(fetcher.throttle)
            u = github_user(m.get("login") or "", fetcher=fetcher)
            if u.get(RATE_LIMITED):
                return out
            c = _person_contact(u, company=company, need_hiring=False)
            if c:
                out.append(c)
        break                                              # организация найдена — другие не смотрим
    return sorted(out, key=lambda c: (_RANK[c.kind], c.email))


# ── Поиск по вебу через официальный API: адрес опубликован вне сайта компании ──

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH_PAGES = 4


def search_people(company: str, site: str, api_key: str, fetcher: Fetcher | None = None) -> list:
    """Страницы с опубликованным адресом руководителя: интервью, доклады, пресс-релизы.

    Поисковик только подсказывает страницу. Адрес берётся лишь тогда, когда он
    реально стоит на ней, принадлежит домену компании и рядом указана должность.
    """
    domain = org_domain("x@" + (urlparse(site).hostname or "")) if site else ""
    if not api_key or not domain:
        return []
    fetcher = fetcher or Fetcher()
    try:
        r = fetcher.http.get(
            BRAVE_URL, params={"q": '"@%s" CEO OR founder OR CTO OR "head of engineering"' % domain,
                               "count": "10"},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"})
    except Exception:                                      # noqa: BLE001
        return []
    if r.status_code != 200:
        return []
    out: dict = {}
    for hit in (((r.json().get("web") or {}).get("results")) or [])[:SEARCH_PAGES]:
        url = hit.get("url") or ""
        host = (urlparse(url).hostname or "").lower()
        if not url.startswith("http") or _NOT_COMPANY.search(host):
            continue                                       # соцсети и борды — за логином
        page = fetcher.get(url)
        if not page:
            continue
        for c in contacts_from_html(page, url, company, org=domain):
            if c.kind == EXEC:
                out.setdefault(c.email, c)
    return sorted(out.values(), key=lambda c: c.email)


def main() -> int:
    ap = argparse.ArgumentParser(description="Опубликованные адреса для прямых писем")
    ap.add_argument("--site", help="сайт компании")
    ap.add_argument("--github", action="store_true", help="профили GitHub с hiring в био")
    args = ap.parse_args()
    rows = []
    if args.site:
        rows += find_company_contacts(args.site)
    if args.github:
        rows += github_people(limit=5)
    for c in rows:
        print("%-8s %-34s %s | %s %s" % (c.kind, c.email, c.source_url, c.person, c.person_role))
    print("найдено: %d" % len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
