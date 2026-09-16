"""Публичные job-API без авторизации.

Проверено живыми запросами 2026-08-05.

  Trudvsem  — государственный портал «Работа России». Открытые данные,
              у ~50% вакансий есть email работодателя. Единственный источник
              с прямыми контактами, к которому нет вопросов по правилам.
  Workable  — кросс-компанийный поиск: не нужно знать токен доски, ищет
              сразу по всем клиентам. Отклик через форму.
  The Muse  — много Senior Product Manager. Форма.
  Arbeitnow — Германия/UK, remote. Форма.
  Remotive  — remote-разработка. Форма.

    python -m jobhunter.ingest.jobapis --check
    python -m jobhunter.ingest.jobapis --only trudvsem
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from collections.abc import Iterator

import httpx

from ..models import ContactKind
from .base import RawJob, parse_ts
from .reddit import RedditHiringSource

UA = {"User-Agent": "jobhunter/0.1 (personal job search)",
      "Accept": "application/json"}
_TAG = re.compile(r"<[^>]+>")

# Запросы под профиль: Technical PM / backend / platform.
QUERIES = ["technical product manager", "product manager", "backend engineer",
           "platform engineer", "python developer"]
RU_QUERIES = ["продакт менеджер", "product manager", "python", "backend",
              "технический руководитель"]


def _strip(x: str) -> str:
    t = html.unescape(x or "")
    t = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", t, flags=re.I)
    t = _TAG.sub("", t)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(t)).strip()


def _tag_of(title: str) -> str:
    t = (title or "").lower()
    for name, pat in [("Product Manager", r"product\s*manager|product\s*owner|продакт"),
                      ("Project Manager", r"project\s*manager|проджект"),
                      ("DS / ML", r"\bml\b|machine learning|data scien|нейросет"),
                      ("DevOps", r"devops|\bsre\b|platform|инфраструктур"),
                      ("Python", r"\bpython\b|django|fastapi"),
                      ("Backend", r"back-?end|бэкенд|бекенд|сервер"),
                      ("QA Auto", r"\bqa\b|автотест|test engineer|аqa"),
                      ("Data", r"data engineer|аналитик данных")]:
        if re.search(pat, t):
            return name
    return "IT"


class _Base:
    throttle = 1.5

    def __init__(self):
        self.http = httpx.Client(timeout=30.0, trust_env=False,
                                 follow_redirects=True, headers=UA)

    def _get(self, url: str):
        time.sleep(self.throttle)
        for attempt in range(3):
            try:
                r = self.http.get(url)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (400, 404):
                    return None
            except Exception:
                pass
            time.sleep(1.5 * (attempt + 1))
        return None


# Релевантность до yield: без неё единственный ручной прогон этого модуля
# в августе засорил базу («Werkstudent Marketing», «Бухгалтер»). Для
# англоязычных источников — фильтр boards (EN-токены), для Трудвсем — свой:
# boards-токены английские и убили бы русскую выдачу целиком.
import re as _re

# PM-роли включены: они есть в QUERIES этого же модуля, а фильтр их резал —
# запрошенное у API тут же выбрасывалось.
RU_RELEVANT = _re.compile(
    r"(python|бэкенд|бекенд|back-?end|разработ\w+|devops|sre|"
    r"архитект\w+|инженер[ -]?программ\w+|тимлид|техлид|"
    r"team ?lead|tech ?lead|"
    r"продакт|product\s*(?:manager|owner)|менеджер\s+продукт\w*|"
    r"руководитель\s+продукт\w*|владелец\s+продукт\w*)", _re.I)

# Основа «данн*» — только по заголовку: в теле каждой вакансии Трудвсем
# лежит канцелярское «согласие на обработку персональных данных», и по нему
# бухгалтерия проходила фильтр как «инженер данных».
RU_RELEVANT_TITLE_ONLY = _re.compile(
    r"(инженер\w*\s+данн\w+|аналитик\w*\s+данн\w+|data\s+engineer)", _re.I)


def _ru_relevant(title: str, body: str = "") -> bool:
    from .boards import EXCLUDE
    blob = "%s %s" % (title or "", (body or "")[:1500])
    if EXCLUDE.search(blob):
        return False
    return bool(RU_RELEVANT.search(blob)
                or RU_RELEVANT_TITLE_ONLY.search(title or ""))


def _en_relevant(title: str, body: str = "") -> bool:
    from .boards import _relevant
    return _relevant(title, body)


class TrudvsemSource(_Base):
    """«Работа России». Открытые данные, у половины вакансий — email."""
    name = "trudvsem"
    REGIONS = [("7700000000000", "Москва"), ("7800000000000", "Санкт-Петербург")]

    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        made = 0
        for region, rname in self.REGIONS:
            for q in RU_QUERIES:
                for offset in (0, 20):
                    url = ("https://opendata.trudvsem.ru/api/v1/vacancies/region/%s"
                           "?text=%s&limit=20&offset=%d"
                           % (region, httpx.URL(path=q).path.lstrip("/"), offset))
                    data = self._get(url)
                    items = (((data or {}).get("results") or {}).get("vacancies")) or []
                    if not items:
                        break
                    for it in items:
                        v = it.get("vacancy") or {}
                        comp = v.get("company") or {}
                        email = ""
                        for c in (v.get("contact_list") or []):
                            if c.get("contact_type") == "email" and c.get("contact_value"):
                                email = c["contact_value"].strip()
                                break
                        if not email:
                            email = (comp.get("email") or "").strip()
                        title = v.get("job-name") or ""
                        body = "\n\n".join(x for x in [
                            v.get("duty") or "", v.get("requirement", {}).get("education", "")
                            if isinstance(v.get("requirement"), dict) else "",
                            str(v.get("requirement") or "")] if x)
                        if not _ru_relevant(title, body):
                            continue
                        sal = ""
                        if v.get("salary_min") or v.get("salary_max"):
                            sal = "%s–%s %s" % (v.get("salary_min") or "",
                                                v.get("salary_max") or "",
                                                v.get("currency") or "RUB")
                        yield RawJob(
                            source="trudvsem",
                            external_uuid="trudvsem:%s" % v.get("id"),
                            title=title, company=comp.get("name", ""),
                            tag=_tag_of(title), content=_strip(body),
                            mode="full", salary_raw=sal,
                            posted_at=parse_ts(v.get("creation-date")),
                            contact_kind=(ContactKind.EMAIL.value if email
                                          else ContactKind.EXTERNAL_URL.value),
                            contact_email=email,
                            contact_url=("mailto:" + email) if email else (v.get("vac_url") or ""),
                            all_links=[{"key": "other_apply", "value": v.get("vac_url") or ""}],
                            raw={"region": rname, "person": v.get("contact_person", "")},
                        )
                        made += 1
                        if limit and made >= limit:
                            return


class WorkableSource(_Base):
    """Кросс-компанийный поиск Workable: токен доски знать не нужно."""
    name = "workable"

    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        made = 0
        for q in QUERIES:
            token, pages = None, 0
            while pages < 3:
                url = ("https://jobs.workable.com/api/v1/jobs?query=%s"
                       "&workplace=remote&day_range=30&limit=20"
                       % httpx.URL(path=q).path.lstrip("/"))
                if token:
                    url += "&pageToken=" + token
                data = self._get(url)
                jobs = (data or {}).get("jobs") or []
                if not jobs:
                    break
                for j in jobs:
                    comp = j.get("company") or {}
                    body = "\n\n".join(_strip(j.get(k) or "") for k in
                                       ("description", "requirementsSection", "benefitsSection"))
                    yield RawJob(
                        source="workable",
                        external_uuid="workable:%s" % j.get("id"),
                        title=j.get("title", ""), company=comp.get("title", ""),
                        tag=_tag_of(j.get("title", "")), content=body, mode="full",
                        posted_at=parse_ts(j.get("published") or j.get("createdAt")),
                        contact_kind=ContactKind.EXTERNAL_URL.value,
                        contact_url=j.get("url", ""),
                        all_links=[{"key": "other_apply", "value": j.get("url", "")}],
                        raw={"workplace": j.get("workplace"),
                             "locations": j.get("locations")},
                    )
                    made += 1
                    if limit and made >= limit:
                        return
                token = (data or {}).get("nextPageToken")
                pages += 1
                if not token:
                    break


class MuseSource(_Base):
    """The Muse: много Senior Product Manager."""
    name = "muse"

    def iter_jobs(self, limit: int | None = None) -> Iterator[RawJob]:
        made = 0
        for cat in ("Product%20Management", "Software%20Engineering"):
            for page in range(2):
                data = self._get("https://www.themuse.com/api/public/jobs"
                                 "?page=%d&category=%s&level=Senior%%20Level" % (page, cat))
                for j in (data or {}).get("results", []) or []:
                    comp = (j.get("company") or {}).get("name", "")
                    url = ((j.get("refs") or {}).get("landing_page")) or ""
                    if not _en_relevant(j.get("name", ""), j.get("contents") or ""):
                        continue
                    yield RawJob(
                        source="muse",
                        external_uuid="muse:%s" % j.get("id"),
                        title=j.get("name", ""), company=comp,
                        tag=_tag_of(j.get("name", "")),
                        content=_strip(j.get("contents") or ""), mode="full",
                        posted_at=parse_ts(j.get("publication_date")),
                        contact_kind=ContactKind.EXTERNAL_URL.value,
                        contact_url=url,
                        all_links=[{"key": "other_apply", "value": url}],
                        raw={"locations": [l.get("name") for l in (j.get("locations") or [])]},
                    )
                    made += 1
                    if limit and made >= limit:
                        return


# Arbeitnow и Remotive здесь были и УДАЛЕНЫ: их полные дубли живут в
# boards.py с фильтром релевантности, а версии отсюда шли без фильтра и
# под тем же именем источника — мусор вроде «Werkstudent Marketing»
# сливался в отчётах с чистой выдачей boards.
SOURCES = {
    "trudvsem": TrudvsemSource,
    "workable": WorkableSource,
    "muse": MuseSource,
    # Единственный здесь с авторизацией: без REDDIT_CLIENT_ID/SECRET в .env
    # отдаёт пустоту и не мешает остальным (решение владельца 16.09).
    "reddit": RedditHiringSource,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Публичные job-API")
    ap.add_argument("--only", choices=sorted(SOURCES))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    names = [args.only] if args.only else list(SOURCES)

    if args.check:
        print("%-11s %8s %9s" % ("источник", "вакансий", "с email"))
        print("-" * 32)
        for n in names:
            try:
                rows = list(SOURCES[n]().iter_jobs(limit=15))
                em = sum(1 for r in rows if r.contact_email)
                print("%-11s %8d %9d" % (n, len(rows), em))
            except Exception as e:
                print("%-11s ОШИБКА %s" % (n, str(e)[:40]))
        return 0

    from .base import save_jobs
    total = {}
    for n in names:
        print("\n[%s]" % n)
        try:
            total[n] = save_jobs(SOURCES[n]().iter_jobs(limit=args.limit), verbose=False)
        except Exception as e:
            print("  ОШИБКА: %s" % str(e)[:120])
            continue
        st = total[n]
        print("  видел %d, новых %d, дублей %d, с контактом %d"
              % (st["seen"], st["new"], st["dupes"], st["with_contact"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
