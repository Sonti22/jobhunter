"""Автопополнение реестра ATS-досок из уже собранных вакансий.

Реестр в ats.py пополнялся только руками, при том что вакансии из HN,
бордов и каналов регулярно ссылаются на greenhouse/lever/ashby/workable
доски компаний, которых в реестре нет: на момент написания в базе лежали
ссылки на ~20 непокрытых досок. Их вакансии проходили мимо системы.

Паттерн скопирован с автопоиска телеграм-каналов (discover.py), потому что
он уже доказал себя: кандидат → проверка живым запросом → ЯВНОЕ включение
владельцем → подмешивание в сбор. Автовключения нет намеренно: каждая
доска — это десятки вакансий в скоринг ежедневно, и расширять поток должен
человек, а не regex.

    python -m jobhunter.ingest.ats_discover --list     # что нашлось
    python -m jobhunter.ingest.ats_discover --verify   # проверить кандидатов
    python -m jobhunter.ingest.ats_discover --apply    # включить прошедших
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter

from sqlalchemy import select

from ..db import session_scope
from ..models import AtsCandidate, Job, utcnow

# Формы ссылок всех четырёх провайдеров. Токен — сегмент после домена;
# служебные пути (blog, embed без for=) отсекаются негативным списком ниже.
TOKEN_RES = [
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/([\w-]+)", re.I)),
    ("greenhouse", re.compile(
        r"greenhouse\.io/embed/job_board\?[^\"'\s]*for=([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)", re.I)),
]

# Сегменты, которые выглядят как токен, но им не являются.
NOT_TOKENS = {"embed", "api", "blog", "about", "privacy", "terms", "jobs",
              "j", "careers", "www"}

# Мягкий потолок работающего реестра: каждая доска — это сетевой запрос и
# десятки вакансий в скоринг за прогон. Превышение — повод для уведомления
# владельцу, а не для тихого роста.
SOFT_CAP_ENABLED = 30


def _extract(text: str):
    for provider, rx in TOKEN_RES:
        for m in rx.finditer(text or ""):
            token = m.group(1).lower()
            if token and token not in NOT_TOKENS:
                yield provider, token


def harvest() -> dict:
    """Проход по собранным вакансиям: новые (provider, token) → кандидаты.

    Только regex по базе, без единого сетевого запроса — можно звать в
    хвосте каждого сбора.
    """
    from .ats import REGISTRY
    known = {(p, t) for p, t, _ in REGISTRY}
    stats = Counter()
    with session_scope() as sess:
        existing = {(c.provider, c.token): c
                    for c in sess.scalars(select(AtsCandidate)).all()}
        for job in sess.scalars(select(Job)).all():
            blob = " ".join([
                job.contact_url or "",
                " ".join(str(x) for x in (job.all_links_json or [])),
                (job.description_raw or "")[:4000]])
            for provider, token in _extract(blob):
                if (provider, token) in known:
                    continue
                stats["found"] += 1
                cand = existing.get((provider, token))
                if cand:
                    cand.last_seen_at = utcnow()
                    continue
                cand = AtsCandidate(provider=provider, token=token,
                                    company_name=(job.company_name or "")[:80],
                                    found_via=(job.external_uuid or "")[:120])
                sess.add(cand)
                sess.flush()
                existing[(provider, token)] = cand
                stats["new"] += 1
    return dict(stats)


def verify(limit: int = 10, throttle: float = 1.5) -> dict:
    """Живой запрос к непроверенным кандидатам через провайдеры ats.py.

    _fetch — метод ATSSource (у него свой httpx-клиент и ретраи), поэтому
    заводим источник с пустым реестром только ради провайдеров.
    """
    from .ats import RELEVANT, ATSSource
    src = ATSSource(registry=[], throttle=throttle)
    stats = Counter()
    with session_scope() as sess:
        ids = [c.id for c in sess.scalars(
            select(AtsCandidate)
            .where(AtsCandidate.checked_at.is_(None))
            .order_by(AtsCandidate.last_seen_at.desc())
            .limit(limit)).all()]

    try:
        for cid in ids:
            with session_scope() as sess:
                c = sess.get(AtsCandidate, cid)
                try:
                    jobs = src._fetch(c.provider, c.token,
                                      c.company_name or c.token)
                except Exception as e:
                    c.reason = "%s: %s" % (type(e).__name__, str(e)[:120])
                    c.checked_at = utcnow()
                    stats["error"] += 1
                    continue
                c.jobs_seen = len(jobs)
                c.relevant_jobs = sum(
                    1 for j in jobs if RELEVANT.search(j.get("title", "")))
                # Пустой список неотличим от сетевого сбоя: _get внутри
                # провайдеров глотает исключения и отдаёт None → генератор
                # молчит. Такой исход НЕ финален — checked_at не ставим,
                # кандидат перепроверится следующим прогоном, а не
                # хоронится навсегда из-за одного таймаута.
                if c.jobs_seen == 0:
                    c.reason = "фид пуст или недоступен — перепроверю"
                    stats["retry_later"] += 1
                    continue
                # Мегаборды (сотни вакансий) включать только руками: одна
                # такая доска ежедневно заливает скоринг сильнее, чем все
                # остальные источники вместе.
                if c.jobs_seen > 300:
                    c.passed = False
                    c.reason = ("мегаборд: %d вакансий (%d профильных) — "
                                "включать вручную" % (c.jobs_seen,
                                                      c.relevant_jobs))
                else:
                    c.passed = c.relevant_jobs >= 1
                    c.reason = ("%d вакансий, %d профильных"
                                % (c.jobs_seen, c.relevant_jobs)) if c.passed \
                        else ("нет профильных (%d вакансий)" % c.jobs_seen)
                c.checked_at = utcnow()
                stats["passed" if c.passed else "rejected"] += 1
            time.sleep(throttle)
    finally:
        close = getattr(src, "close", None)
        if close:
            close()
    return dict(stats)


def apply_passed() -> int:
    """Включает прошедших проверку кандидатов в сбор."""
    from .ats import REGISTRY
    n = 0
    with session_scope() as sess:
        enabled_now = sess.scalars(select(AtsCandidate).where(
            AtsCandidate.enabled.is_(True))).all()
        for c in sess.scalars(select(AtsCandidate).where(
                AtsCandidate.passed.is_(True),
                AtsCandidate.enabled.is_(False))).all():
            c.enabled = True
            c.enabled_at = utcnow()
            n += 1
        # Рабочий реестр = статический список ats.py + включённые кандидаты;
        # считать потолок только по кандидатам значило занижать его вдвое.
        total = len(REGISTRY) + len(enabled_now) + n
    if total > SOFT_CAP_ENABLED:
        from .. import notify
        notify.push("warn",
                    "⚠️ Включено %d ATS-досок (мягкий потолок %d) — время "
                    "прополоть: python -m jobhunter.ingest.ats_discover --list"
                    % (total, SOFT_CAP_ENABLED),
                    dedup="ats_cap:%d" % total)
    return n


def enabled_boards() -> list:
    """(provider, token, company) включённых кандидатов — для ATSSource.

    Имя компании — из токена, а не из company_name кандидата: harvest пишет
    туда компанию ВАКАНСИИ-находки, и все вакансии доски pear-vc подписались
    бы «Phaselaw» — именем фирмы, в чьём объявлении встретилась ссылка.
    """
    with session_scope() as sess:
        return [(c.provider, c.token,
                 c.token.replace("-", " ").replace("_", " ").title())
                for c in sess.scalars(select(AtsCandidate).where(
                    AtsCandidate.enabled.is_(True))).all()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Автопоиск ATS-досок")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    print("harvest:", harvest())
    if args.verify:
        print("verify:", verify(limit=args.limit))
    if args.apply:
        print("включено:", apply_passed())
    if args.list or not (args.verify or args.apply):
        with session_scope() as sess:
            for c in sess.scalars(select(AtsCandidate)
                                  .order_by(AtsCandidate.provider,
                                            AtsCandidate.token)).all():
                mark = "✓" if c.enabled else ("+" if c.passed else
                                              ("?" if not c.checked_at else "-"))
                print(" %s %-10s %-24s %s" % (mark, c.provider, c.token,
                                              c.reason or "не проверен"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
