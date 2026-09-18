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

Источники (первые три — без ключей и подписок):
  сайт компании   страницы команды, руководства, контактов, прессы и Impressum;
                  адреса страниц берём из sitemap.xml, а не угадываем. robots.txt
                  уважается, не больше четырёх страниц;
  Hacker News     комментарии с адресом на домене компании (официальный поиск
                  hn.algolia.com) — автор сам опубликовал адрес для откликов;
  GitHub          публичный профиль, где человек сам открыл email: участники
                  организации компании и те, кто пишет в био, что нанимает;
  Tavily          поиск по вебу (1000 запросов в месяц бесплатно, без карты):
                  только подсказывает страницу, адрес обязан стоять на ней.

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
         "/about-us", "/company", "/impressum", "/imprint", "/jobs", "/press")
MAX_PAGES = 4
MAX_ATTEMPTS = 10
SITEMAP_PAGES = 6         # сколько адресов страниц берём из карты сайта
SITEMAP_FILES = 3         # сколько файлов карты читаем (индекс + вложенные)
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
    r"engineering\s+manager|managing\s+director|gesch[äa]ftsf[üu]hrer(?:in)?)\b|"
    r"генеральн\w+\s+директор|основател\w+|техническ\w+\s+директор|"
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

    def get(self, url: str, kinds: tuple = ("html",)) -> str:
        if not self.allowed(url):
            return ""
        time.sleep(self.throttle)
        try:
            r = self.http.get(url)
        except Exception:                                  # noqa: BLE001
            return ""
        ctype = (r.headers.get("content-type") or kinds[0]).lower()
        if r.status_code != 200 or not any(k in ctype for k in kinds):
            return ""
        return r.text[:MAX_BYTES]

    def sitemaps(self, site: str) -> list:
        """Карты сайта: те, что владелец сам указал в robots.txt, иначе /sitemap.xml."""
        root = site.rstrip("/")
        self.allowed(root + "/")                           # читает robots.txt, если ещё не читали
        parts = urlparse(root)
        rp = self._robots.get("%s://%s" % (parts.scheme, parts.netloc))
        listed = (rp.site_maps() or []) if rp is not None else []
        return list(listed)[:SITEMAP_FILES] or [root + "/sitemap.xml"]


# Страницы, где компании публикуют людей и адреса; порядок — от самых полезных.
_PEOPLE_PATH = (
    re.compile(r"/(?:team|people|leadership|management|founders?|our-team|who-we-are|команда|руководство)(?:/|$)", re.I),
    re.compile(r"/(?:about|about-us|company|о-компании|o-kompanii|about_us)(?:/|$)", re.I),
    re.compile(r"/(?:contacts?|contact-us|impressum|imprint|legal-notice|контакты|kontakty)(?:/|$)", re.I),
    re.compile(r"/(?:press|media|newsroom|careers?|jobs|join(?:-us)?|vacancies|вакансии)(?:/|$)", re.I),
)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.I)
# Вложенные карты блога, товаров и тегов — тысячи адресов и ни одного человека.
_SITEMAP_NOISE = re.compile(r"post|blog|product|tag|categor|news|article|image|video|author", re.I)


def sitemap_pages(site: str, fetcher) -> list:
    """Адреса страниц с людьми и контактами из карты сайта — вместо угадывания путей."""
    root_org = org_domain("x@" + (urlparse(site).hostname or ""))
    listed = getattr(fetcher, "sitemaps", None)
    queue = list(listed(site)) if listed else [site.rstrip("/") + "/sitemap.xml"]
    ranked: dict = {}
    read = 0
    while queue and read < SITEMAP_FILES:
        body = fetcher.get(queue.pop(0), kinds=("xml", "text/plain"))
        read += 1
        if not body:
            continue
        locs = [_html.unescape(u) for u in _LOC.findall(body)]
        if "<sitemapindex" in body.lower():
            nested = [u for u in locs if not _SITEMAP_NOISE.search(urlparse(u).path)]
            queue = nested[:SITEMAP_FILES] + queue
            continue
        for u in locs:
            p = urlparse(u)
            path = p.path.rstrip("/")
            if org_domain("x@" + (p.hostname or "")) != root_org or path.count("/") > 3:
                continue
            for rank, rx in enumerate(_PEOPLE_PATH):
                if rx.search(path + "/"):
                    ranked.setdefault(u, (rank, path.count("/"), len(path)))
                    break
    return sorted(ranked, key=lambda u: ranked[u])[:SITEMAP_PAGES]


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


def contacts_from_html(page: str, page_url: str, company: str = "", org: str = "",
                       fallback_kind: str = "") -> list:
    """Опубликованные на странице адреса компании, пригодные для отклика.

    org — домен компании, когда страница чужая (интервью, сайт конференции).
    fallback_kind — вид для именного адреса без должности рядом: в посте о найме
    автор оставил адрес именно для откликов, на странице команды — нет.
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
        kind = kind or fallback_kind
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
    # Сначала страницы, которые сайт сам перечислил в sitemap, потом привычные пути.
    unique: dict = {}
    for url in sitemap_pages(site, fetcher) + [urljoin(site.rstrip("/") + "/", p.lstrip("/"))
                                               for p in PAGES]:
        unique.setdefault(url.rstrip("/").lower(), url)    # /team и /team/ — одна страница
    for url in list(unique.values())[:MAX_ATTEMPTS]:
        if fetched >= MAX_PAGES:
            break
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
    if source_url.startswith(HN_ITEM):
        item = _json_get(HN_API + "/items/" + source_url[len(HN_ITEM):], fetcher)
        return email.lower() in _html.unescape(item.get("text") or "").lower()
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


# То же по названию компании и домену: «Bighire.io», «Talento IT» (сухой прогон 18.09).
# К био не применяется — «we hire remotely» пишет и обычный основатель.
_RECRUITER_NAME = re.compile(r"hire|hiring|talent|staff|recruit|jobs?\b|career|hunt|кадр|персонал", re.I)


def _company_name(raw: str) -> str:
    """Поле company из профиля: «@acme», «Acme | Beta», а иногда ссылка на linktr.ee."""
    name = (raw or "").strip().lstrip("@").strip()
    if "://" in name or name.lower().startswith("www."):
        name = urlparse(name if "://" in name else "https://" + name).path.strip("/").split("/")[-1]
    return name.split("|")[0].strip()[:60]


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


_FORMER = re.compile(r"\b(?:ex|former(?:ly)?|previously|prev\.?|past|бывш\w*|экс)\W*$", re.I)


def _role_at(bio: str, *names: str) -> str:
    """Должность, которую человек занимает ИМЕННО в этой компании: «CTO at Acme».

    Живая проверка 18.09: инженер PostHog с био «Engineer, Founder. Building
    products @PostHog» размечался как основатель PostHog. Слово «Founder» в био —
    ещё не должность в компании, куда мы пишем.
    """
    keys = [k for k in (re.sub(r"[^a-z0-9а-яё]+", "", (n or "").lower()) for n in names)
            if len(k) >= 3]
    for m in _EXEC_ROLE.finditer(bio or ""):
        if _FORMER.search(bio[max(0, m.start() - 14): m.start()]):
            continue
        after = re.sub(r"[^a-z0-9а-яё]+", "", bio[m.end(): m.end() + 30].lower())
        if any(k in after for k in keys):
            return m.group(0)
    return ""


def _person_contact(u: dict, *, company: str = "", need_hiring: bool = True,
                    aliases: tuple = ()) -> Contact | None:
    """Профиль GitHub → адресат: email открыт самим человеком, ящик не служебный, не рекрутёр."""
    from ..outreach.mailer import _mailbox_ok

    email = (u.get("email") or "").strip().lower()
    bio = u.get("bio") or ""
    if not email or not _valid_email(email) or not is_hiring_mailbox(email) or not _mailbox_ok(email):
        return None
    if _RECRUITER.search(" ".join([bio, u.get("login") or "", u.get("company") or "",
                                   u.get("name") or "", email.rsplit("@", 1)[-1]])):
        return None
    if _RECRUITER_NAME.search(" ".join([u.get("company") or "", email.rsplit("@", 1)[-1]])):
        return None
    if need_hiring:
        # Человек сам пишет, что нанимает: должность — его собственные слова о себе.
        found = _EXEC_ROLE.search(bio)
        role = found.group(0) if found else ""
        if not _HIRING_BIO.search(bio):
            return None
    else:
        # Участник организации целевой компании: должность должна быть в ней самой.
        role = _role_at(bio, company, *aliases)
        if not role and not _HIRING_BIO.search(bio):
            return None                   # рядовой участник организации — не адресат
    login = u.get("login") or ""
    return Contact(
        email=email, kind=EXEC if role else REFERRAL,
        source_url="https://api.github.com/users/" + login,
        company=company or _company_name(u.get("company") or ""),
        person=(u.get("name") or login).strip(),
        person_role=role, note=bio[:200])


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
            c = _person_contact(u, company=company, need_hiring=False, aliases=(login,))
            if c:
                out.append(c)
        break                                              # организация найдена — другие не смотрим
    return sorted(out, key=lambda c: (_RANK[c.kind], c.email))


# ── Hacker News: автор комментария сам оставил адрес на домене компании ──

HN_API = "https://hn.algolia.com/api/v1"
HN_ITEM = "https://news.ycombinator.com/item?id="
HN_MAX_AGE_DAYS = 540      # адрес из комментария старше полутора лет мог умереть → отбивка
_HIRING_TEXT = re.compile(r"\bhiring\b|\bapply\b|\bresume\b|\bcv\b|\bemail\s+me\b|\breach\s+out\b|"
                          r"\bopen\s+roles?\b|\bwe(?:'re| are)\s+looking\b", re.I)


def _json_get(url: str, fetcher: Fetcher | None = None, params: dict | None = None) -> dict:
    try:
        r = (fetcher or Fetcher()).http.get(url, params=params)
    except Exception:                                      # noqa: BLE001
        return {}
    try:
        data = r.json() if r.status_code == 200 else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def hn_people(company: str, site: str, fetcher: Fetcher | None = None, limit: int = 3) -> list:
    """Комментарии HN с адресом на домене компании. Ключ не нужен — это открытый поиск HN.

    Именной адрес без должности принимается, только если комментарий про найм:
    тогда автор оставил его именно для откликов.
    """
    domain = org_domain("x@" + (urlparse(site).hostname or "")) if site else ""
    if not domain:
        return []
    since = int(time.time()) - HN_MAX_AGE_DAYS * 86400
    data = _json_get(HN_API + "/search_by_date", fetcher,
                     {"query": '"@%s"' % domain, "tags": "comment", "hitsPerPage": "20",
                      "numericFilters": "created_at_i>%d" % since})
    out: dict = {}
    for hit in data.get("hits") or []:
        text = hit.get("comment_text") or ""
        hiring = bool(_HIRING_TEXT.search(_TAG.sub(" ", text))) or \
            "who is hiring" in (hit.get("story_title") or "").lower()
        for c in contacts_from_html(text, HN_ITEM + str(hit.get("objectID") or ""), company,
                                    org=domain, fallback_kind=REFERRAL if hiring else ""):
            if c.kind != GENERAL:
                c.person = c.person or (hit.get("author") or "")
                out.setdefault(c.email, c)                 # свежие идут первыми — их и оставляем
    return sorted(out.values(), key=lambda c: (_RANK[c.kind], c.email))[:limit]


# ── Поиск по вебу через официальный API: адрес опубликован вне сайта компании ──

TAVILY_URL = "https://api.tavily.com/search"
SEARCH_PAGES = 4


def search_people(company: str, site: str, api_key: str, fetcher: Fetcher | None = None) -> list:
    """Страницы с опубликованным адресом руководителя: интервью, доклады, пресс-релизы.

    Поисковик только подсказывает страницу. Адрес берётся лишь тогда, когда он
    реально стоит на ней, принадлежит домену компании и рядом указана должность.
    Tavily: 1000 запросов в месяц бесплатно, без карты; один поиск — один кредит.
    """
    domain = org_domain("x@" + (urlparse(site).hostname or "")) if site else ""
    if not api_key or not domain:
        return []
    fetcher = fetcher or Fetcher()
    try:
        r = fetcher.http.post(
            TAVILY_URL, headers={"Authorization": "Bearer " + api_key},
            json={"query": '"@%s" email CEO OR founder OR CTO OR "head of engineering"' % domain,
                  "search_depth": "basic", "max_results": 8})
    except Exception:                                      # noqa: BLE001
        return []
    if r.status_code != 200:
        return []
    out: dict = {}
    for hit in (r.json().get("results") or [])[:SEARCH_PAGES]:
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
