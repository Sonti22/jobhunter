"""Публичные job-борды: JSON-API, RSS и один HTML-листинг с прямыми email.

Все источники проверены живыми запросами 2026-08-25: каждый отдал 200 без
авторизации и ключа, robots.txt каждого разрешает используемый путь.
Источники, где сбор требует входа или запрещён правилами (hh.ru, djinni,
getmatch, justjoin, nofluffjobs, dou.ua), сюда сознательно не включены —
пусть охват будет меньше, чем повод для блокировки аккаунта.

Что здесь есть и зачем:

  remoteok        99 вакансий за запрос, в описаниях встречаются email HR
  arbeitnow       Германия/ЕС, много вакансий с визовой поддержкой
  himalayas       ~100k вакансий, курсорная пагинация, чистые данные
  jobicy          фильтры по гео и индустрии — сразу Европа + инженерия
  remotive        remote-разработка (лимит источника: 4 обращения в сутки)
  workingnomads   дешёвый доп-поток remote, часто ведёт на ATS компании
  wwr             We Work Remotely RSS, изредка попадаются HR-адреса
  cryptojobs      web3, но много Python/Go бэкенда с полной удалёнкой
  habr            русскоязычные вакансии как лиды (контакты добираются позже)
  euremote        ЕС и релокация
  ergodotisi      Кипр: единственный найденный борд, где email работодателя
                  видны прямо в листинге без авторизации

Про контакты. У большинства бордов отклик идёт через их же форму, прямых
адресов нет — такие вакансии всё равно полезны: они попадают в список для
ручного отклика (export_manual.py) и подсказывают компании, чьи ATS-фиды
потом читает ats.py. Прямые контакты дают только ergodotisi, remoteok и
частично wwr — их и стоит ждать в первую очередь.

    python -m jobhunter.ingest.boards --check          # что живо сейчас
    python -m jobhunter.ingest.boards --only ergodotisi
    python -m jobhunter.ingest.boards                  # собрать всё
"""
from __future__ import annotations

import argparse
import html as _html
import re
import sys
import time
from collections.abc import Iterator

import httpx

from ..models import ContactKind
from .base import RawJob, extract_email, parse_ts

# Обычный браузерный UA: у remoteok перед API стоит Cloudflare, и на
# «библиотечный» User-Agent он отвечает 403.
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/140.0.0.0 Safari/537.36"),
      "Accept": "application/json, text/xml;q=0.9, */*;q=0.8",
      "Accept-Language": "en,ru;q=0.9"}

TAG = re.compile(r"<[^>]+>")
CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

# Что нас интересует: профиль владельца — Python/бэкенд/платформа/данные.
KEYWORDS = re.compile(
    r"\b(python|django|fastapi|flask|backend|back-end|golang|\bgo\b|"
    r"platform|infrastructure|devops|sre|data engineer|ml engineer|"
    r"tech lead|team lead|software architect|full[- ]?stack)\b", re.I)

# Явно не наше: чтобы не тащить 100k вакансий целиком.
EXCLUDE = re.compile(
    r"\b(sales|marketing|copywriter|designer|recruiter|customer support|"
    r"account manager|seo|content writer|teacher|nurse|driver)\b", re.I)


def _text(x: str) -> str:
    t = CDATA.sub(r"\1", x or "")
    t = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", t, flags=re.I)
    t = TAG.sub("", t)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(t)).strip()


def _relevant(title: str, body: str = "") -> bool:
    if EXCLUDE.search(title or ""):
        return False
    return bool(KEYWORDS.search(title or "") or KEYWORDS.search(body[:1500] or ""))


def _mk(source: str, uid: str, title: str, company: str, body: str,
        url: str, posted: int = 0, salary: str = "", tag: str = "") -> RawJob:
    """Общая сборка RawJob: контакт — email, если он реально есть в тексте."""
    email = extract_email(body) or ""
    kind = ContactKind.EMAIL.value if email else ContactKind.EXTERNAL_URL.value
    return RawJob(source=source, external_uuid="%s:%s" % (source, uid),
                  title=(title or "").strip()[:200], company=(company or "").strip(),
                  tag=tag or "", content=body[:12000], posted_at=posted,
                  salary_raw=salary, contact_kind=kind, contact_email=email,
                  contact_url=url, all_links=[url] if url else [],
                  raw={"source": source})


def _client() -> httpx.Client:
    # trust_env=False: системный socks-прокси в окружении ломает httpx и не
    # нужен ни одному из этих хостов.
    return httpx.Client(headers=UA, timeout=40.0, trust_env=False,
                        follow_redirects=True)


# ────────────────────────────────────────────────────────── JSON-API ──

def remoteok(http: httpx.Client) -> Iterator[RawJob]:
    r = http.get("https://remoteok.com/api")
    r.raise_for_status()
    for row in r.json():
        if not isinstance(row, dict) or not row.get("id"):
            continue                       # первый элемент — легальная памятка
        body = _text(row.get("description", ""))
        title = row.get("position") or row.get("title") or ""
        if not _relevant(title, body):
            continue
        salary = ""
        if row.get("salary_min"):
            salary = "%s-%s %s" % (row.get("salary_min"), row.get("salary_max", ""),
                                   row.get("currency", "USD"))
        yield _mk("remoteok", str(row["id"]), title, row.get("company", ""),
                  body, row.get("url", ""), int(row.get("epoch") or 0), salary,
                  ", ".join(row.get("tags", [])[:6]))


def arbeitnow(http: httpx.Client, pages: int = 2) -> Iterator[RawJob]:
    for page in range(1, pages + 1):
        r = http.get("https://www.arbeitnow.com/api/job-board-api",
                     params={"page": page})
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            break
        for row in rows:
            body = _text(row.get("description", ""))
            if not _relevant(row.get("title", ""), body):
                continue
            yield _mk("arbeitnow", row.get("slug", ""), row.get("title", ""),
                      row.get("company_name", ""), body, row.get("url", ""),
                      int(row.get("created_at") or 0), "",
                      ", ".join(row.get("tags", [])[:6]))
        time.sleep(1.5)


def himalayas(http: httpx.Client, pages: int = 2) -> Iterator[RawJob]:
    cursor = None
    for _ in range(pages):
        params = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        r = http.get("https://himalayas.app/jobs/api", params=params)
        r.raise_for_status()
        data = r.json()
        for row in data.get("jobs", []):
            body = _text(row.get("description") or row.get("excerpt", ""))
            if not _relevant(row.get("title", ""), body):
                continue
            salary = ""
            if row.get("minSalary"):
                salary = "%s-%s %s" % (row.get("minSalary"), row.get("maxSalary", ""),
                                       row.get("currency", ""))
            yield _mk("himalayas", str(row.get("guid") or row.get("title", "")),
                      row.get("title", ""), row.get("companyName", ""), body,
                      row.get("applicationLink") or row.get("url", ""),
                      int(row.get("pubDate") or 0), salary)
        cursor = data.get("nextCursor")
        if not cursor:
            break
        time.sleep(1.5)


def jobicy(http: httpx.Client) -> Iterator[RawJob]:
    for geo in ("europe", "anywhere"):
        r = http.get("https://jobicy.com/api/v2/remote-jobs",
                     params={"count": 50, "geo": geo, "industry": "engineering"})
        r.raise_for_status()
        for row in r.json().get("jobs", []):
            body = _text(row.get("jobDescription") or row.get("jobExcerpt", ""))
            if not _relevant(row.get("jobTitle", ""), body):
                continue
            yield _mk("jobicy", str(row.get("id", "")), row.get("jobTitle", ""),
                      row.get("companyName", ""), body, row.get("url", ""),
                      parse_ts(row.get("pubDate", "")), row.get("annualSalaryMin", ""),
                      row.get("jobGeo", ""))
        time.sleep(2.0)


def remotive(http: httpx.Client) -> Iterator[RawJob]:
    # Владелец API просит не чаще 4 обращений в сутки — один запрос на прогон.
    r = http.get("https://remotive.com/api/remote-jobs",
                 params={"limit": 100, "category": "software-dev"})
    r.raise_for_status()
    for row in r.json().get("jobs", []):
        body = _text(row.get("description", ""))
        if not _relevant(row.get("title", ""), body):
            continue
        yield _mk("remotive", str(row.get("id", "")), row.get("title", ""),
                  row.get("company_name", ""), body, row.get("url", ""),
                  parse_ts(row.get("publication_date", "")),
                  row.get("salary", ""), row.get("job_type", ""))


def workingnomads(http: httpx.Client) -> Iterator[RawJob]:
    r = http.get("https://www.workingnomads.com/api/exposed_jobs/")
    r.raise_for_status()
    for row in r.json():
        body = _text(row.get("description", ""))
        if not _relevant(row.get("title", ""), body):
            continue
        yield _mk("workingnomads", str(row.get("id") or row.get("slug", "")),
                  row.get("title", ""), row.get("company_name", ""), body,
                  row.get("url", ""), parse_ts(row.get("pub_date", "")), "",
                  row.get("category_name", ""))


# ───────────────────────────────────────────────────────────── RSS ──

ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.S | re.I)


def _rss_field(item: str, tag: str) -> str:
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), item, re.S | re.I)
    return _text(m.group(1)) if m else ""


def _rss(http: httpx.Client, source: str, url: str,
         company_tag: str = "") -> Iterator[RawJob]:
    r = http.get(url)
    r.raise_for_status()
    for item in ITEM_RE.findall(r.text):
        title = _rss_field(item, "title")
        body = (_rss_field(item, "description") + "\n"
                + _rss_field(item, "content:encoded"))
        if not _relevant(title, body):
            continue
        company = _rss_field(item, company_tag) if company_tag else ""
        if not company and ":" in title:
            company = title.split(":", 1)[0].strip()
        link = _rss_field(item, "link")
        guid = _rss_field(item, "guid") or link
        yield _mk(source, guid[-80:], title, company, body, link,
                  parse_ts(_rss_field(item, "pubDate")))


def wwr(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "wwr", "https://weworkremotely.com/remote-jobs.rss")


def cryptojobs(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "cryptojobs", "https://cryptocurrencyjobs.co/index.xml")


def habr(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "habr", "https://career.habr.com/vacancies/rss")


def euremote(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "euremote", "https://euremotejobs.com/feed/")


def remotepython(http: httpx.Client) -> Iterator[RawJob]:
    """remotepython.com — только удалённый Python, 100% профильно.
    Фид проверен 28.08.2026: RSS живой."""
    return _rss(http, "remotepython",
                "https://www.remotepython.com/latest/jobs/feed/")


def wwr_backend(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "wwr",
                "https://weworkremotely.com/categories/"
                "remote-back-end-programming-jobs.rss")


def wwr_fullstack(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "wwr",
                "https://weworkremotely.com/categories/"
                "remote-full-stack-programming-jobs.rss")


def wwr_devops(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "wwr",
                "https://weworkremotely.com/categories/"
                "remote-devops-sysadmin-jobs.rss")


def wwr_product(http: httpx.Client) -> Iterator[RawJob]:
    return _rss(http, "wwr",
                "https://weworkremotely.com/categories/remote-product-jobs.rss")


def fourdayweek(http: httpx.Client) -> Iterator[RawJob]:
    """4dayweek.io: удалёнка + сокращённые недели. Фид и robots проверены
    26.08.2026: /feed отдаёт 50 свежих item, robots.txt разрешает. Заголовки
    вида «Role at Company» — компания остаётся в title, скорингу хватает."""
    return _rss(http, "fourdayweek", "https://4dayweek.io/feed")


# ──────────────────────────────────────────── HTML с прямыми email ──

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Ссылка на вакансию: /en-CY/jobs/<slug>-<id>. Списочные ссылки с "?" — мимо.
ERG_LINK = re.compile(r'href="(?P<href>/[a-z]{2}-CY/jobs/[a-z0-9-]+-\d+)"', re.I)


ERG_QUERIES = ["python", "software", "developer", "backend", "devops", "data"]


def ergodotisi(http: httpx.Client, pages: int = 1) -> Iterator[RawJob]:
    """Кипрский борд: email работодателя прямо в листинге, без входа.

    robots.txt закрывает только личные кабинеты (/employee-dashboard,
    /employer-dashboard) — публичный листинг читать разрешено.

    Разметка листинга — карточки без стабильных классов, поэтому режем HTML
    по ссылкам на вакансии: всё до следующей ссылки относится к текущей
    карточке, включая её email. Привязка к классам ломалась бы на каждом
    редизайне, привязка к ссылке — только если сменится схема URL.
    """
    seen = set()
    # Общий листинг Кипра — это бухгалтеры и администраторы; IT там единицы.
    # Поэтому идём через их же поиск по нашим ключевым словам.
    for query in ERG_QUERIES:
        params = {"q": query}
        r = http.get("https://www.ergodotisi.com/jobs", params=params)
        if r.status_code != 200:
            continue
        chunks = re.split(r'(?=href="/[a-z]{2}-CY/jobs/)', r.text, flags=re.I)
        for chunk in chunks:
            m = ERG_LINK.search(chunk)
            if not m:
                continue
            href = m.group("href")
            if href in seen:
                continue
            emails = [e for e in EMAIL_RE.findall(chunk[:4000])
                      if not e.lower().endswith((".png", ".jpg", ".svg"))
                      and "ergodotisi" not in e.lower()]
            if not emails:
                continue
            body = _text(chunk[:4000])
            # Заголовок восстанавливаем из slug: в карточке он размечен
            # по-разному, а slug стабилен и человекочитаем.
            slug = href.rsplit("/", 1)[-1]
            title = re.sub(r"-\d+$", "", slug).replace("-", " ").title()
            if not _relevant(title, body):
                continue
            seen.add(href)
            job = _mk("ergodotisi", slug[-60:], title, "", body,
                      "https://www.ergodotisi.com" + href)
            job.contact_email = emails[0]
            job.contact_kind = ContactKind.EMAIL.value
            yield job
        time.sleep(2.0)


SOURCES = {
    "remoteok": remoteok, "arbeitnow": arbeitnow, "himalayas": himalayas,
    "jobicy": jobicy, "remotive": remotive, "workingnomads": workingnomads,
    "wwr": wwr, "cryptojobs": cryptojobs, "habr": habr, "euremote": euremote,
    "fourdayweek": fourdayweek, "ergodotisi": ergodotisi,
    # Категорийные фиды WWR глубже общего remote-jobs.rss (проверено
    # 28.08.2026: fullstack 119 против ~40 в общем); имя источника у всех
    # "wwr" — дедуп по guid отсекает пересечение с общим фидом.
    "remotepython": remotepython, "wwr_backend": wwr_backend,
    "wwr_fullstack": wwr_fullstack, "wwr_devops": wwr_devops,
    "wwr_product": wwr_product,
}


class BoardsSource:
    """Адаптер под общий интерфейс сбора."""
    name = "boards"

    def __init__(self, only: list | None = None):
        self.only = [x for x in (only or []) if x in SOURCES] or list(SOURCES)

    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        n = 0
        with _client() as http:
            for name in self.only:
                try:
                    for job in SOURCES[name](http):
                        yield job
                        n += 1
                        if limit and n >= limit:
                            return
                except Exception as e:
                    print("  ! %s: %s: %s" % (name, type(e).__name__, str(e)[:80]))
                time.sleep(1.0)


def main() -> int:
    ap = argparse.ArgumentParser(description="Публичные job-борды")
    ap.add_argument("--check", action="store_true",
                    help="проверить доступность источников")
    ap.add_argument("--only", nargs="*", help="только эти источники")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.check:
        with _client() as http:
            for name, fn in SOURCES.items():
                t0 = time.time()
                try:
                    jobs = list(fn(http))
                    mails = sum(1 for j in jobs if j.contact_email)
                    print("  %-14s %3d подходящих, %2d с email  (%.1fс)"
                          % (name, len(jobs), mails, time.time() - t0))
                except Exception as e:
                    print("  %-14s ОШИБКА %s: %s"
                          % (name, type(e).__name__, str(e)[:60]))
                time.sleep(1.0)
        return 0

    from .base import save_jobs
    stats = save_jobs(BoardsSource(args.only).iter_jobs(args.limit or None))
    print("\nВсего: %(seen)d, новых %(new)d, дублей %(dupes)d, "
          "с контактом %(with_contact)d" % stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
