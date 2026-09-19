"""Прямые письма: руководителям компаний и тем, кто может зареферить.

Решения владельца (18.09): адресаты — все, включая CEO корпораций; адреса —
только опубликованные; 10 писем в день; отправка автоматическая.

Раз письма никто не просматривает, между «нашли адрес» и «ушло» стоят гейты,
и заявка одобряется, только если пройдены ВСЕ:
  - у адреса есть URL-источник, и адрес там по-прежнему опубликован;
  - компания профильная, в неё не писали за последние 30 дней, адресат один;
  - письмо прошло гейт правды и проверку читаемости и не повторяет соседние;
  - резюме на языке письма собрано и читается парсером.
Не прошло хоть одно — заявка остаётся в PENDING_APPROVAL с причиной и сама
никуда не уйдёт.

Прямое письмо — обычная заявка с Job.source = "direct:exec" | "direct:referral":
её шлёт штатный mailer под общим дневным потолком почты, ответ приходит
карточкой в бот, отбивка закрывает адрес. У канала свой потолок и свой
предохранитель: две отбивки за день ставят его на паузу до решения владельца,
общая почта при этом работает дальше.

    python -m jobhunter.outreach.direct --status
    python -m jobhunter.outreach.direct --discover --dry
    python -m jobhunter.outreach.direct --run
    python -m jobhunter.outreach.direct --pause "причина" | --resume
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from ..config import get_settings
from ..convo.mailmatch import FREEMAIL, org_domain
from ..db import session_scope
from ..ingest import people
from ..models import Application, ContactKind, Employer, Job, SendLog, Status, utcnow
from ..textutil import max_similarity, norm_keep_digits

log = logging.getLogger("direct")

PREFIX = "direct:"
STATE_KEY = "direct_channel"
SIMILARITY_MAX = 0.75
DEFAULT_ROLE_EN, DEFAULT_ROLE_RU = "Backend / Platform Engineer", "Backend-разработчик"
# Контекст для резюме, когда у компании нет вакансии в базе: нейтральный,
# только то, что и так составляет профиль владельца.
DEFAULT_JD = "Backend engineer. Python, FastAPI, PostgreSQL, Docker, REST API, microservices. Remote."


def is_direct(job) -> bool:
    return bool(job is not None and (job.source or "").startswith(PREFIX))


def _day_start() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


# ─────────────────────────────────────────── потолок и предохранитель ──

def _logs(sess, since: datetime | None = None) -> list:
    """[(result, attempted_at)] по прямым письмам, новые в конце."""
    q = (select(SendLog.result, SendLog.attempted_at)
         .join(Application, SendLog.application_id == Application.id)
         .join(Job, Application.job_id == Job.id)
         .where(Job.source.like(PREFIX + "%"), SendLog.result.in_(("ok", "bounce")))
         .order_by(SendLog.id))
    if since is not None:
        q = q.where(SendLog.attempted_at >= since)
    return [(r, at) for r, at in sess.execute(q).all()]


def sent_today(sess) -> int:
    return sum(1 for r, _ in _logs(sess, _day_start()) if r == "ok")


def paused_reason(sess) -> str:
    from ..observability import RuntimeState
    row = sess.get(RuntimeState, STATE_KEY)
    return (row.error or "пауза") if row is not None and row.status == "paused" else ""


def pause(reason: str) -> None:
    from ..observability import record
    record(STATE_KEY, "paused", error=reason)


def resume() -> None:
    from ..observability import record
    record(STATE_KEY, "ok")


def guard(sess) -> str:
    """Причина паузы канала; сам ставит паузу при отбивках. Пусто — можно слать."""
    reason = paused_reason(sess)
    if reason:
        return reason
    s = get_settings()
    today = sum(1 for r, _ in _logs(sess, _day_start()) if r == "bounce")
    last = _logs(sess)[-20:]
    rate = sum(1 for r, _ in last if r == "bounce") / len(last) if len(last) >= 10 else 0.0
    why = ""
    if today >= s.direct_bounce_stop:
        why = "отбивок прямых писем сегодня: %d" % today
    elif rate > 0.10:
        why = "отбивок %.0f%% на последних %d прямых письмах" % (rate * 100, len(last))
    if why:
        from .. import notify
        from ..observability import RuntimeState
        row = sess.get(RuntimeState, STATE_KEY)
        if row is None:
            row = RuntimeState(key=STATE_KEY)
            sess.add(row)
        row.status, row.error, row.finished_at = "paused", why[:300], utcnow()
        notify.push("error",
                    "⏸ Прямые письма поставлены на паузу: %s.\nОбщая почта работает. "
                    "Посмотри адреса в экране «Прямые письма» и возобнови канал, когда "
                    "будешь уверен." % why,
                    dedup="direct_pause:%s" % utcnow().date().isoformat(), sess=sess)
    return why


def daily_limit(sess) -> int:
    """Первые дни — половинный темп: качество адресов ещё не проверено делом."""
    s = get_settings()
    today = _day_start().date()
    active_days = {at.date() for r, at in _logs(sess) if r == "ok" and at.date() < today}
    return s.direct_rampup_limit if len(active_days) < s.direct_rampup_days else s.direct_daily_limit


def room(sess) -> int:
    """Сколько прямых писем ещё можно отправить сегодня. Зовёт mailer.pick_batch."""
    if not get_settings().direct_enabled or guard(sess):
        return 0
    return max(0, daily_limit(sess) - sent_today(sess))


# ───────────────────────────────────────────────────────── адресаты ──

def _contacted_domains(sess) -> set:
    """Домены и freemail-адреса, куда уже писали в пределах кулдауна или нельзя писать."""
    edge = utcnow() - timedelta(days=get_settings().direct_company_cooldown_days)
    out = set()
    for peer, in sess.execute(select(SendLog.peer_id).where(
            SendLog.result == "ok", SendLog.attempted_at >= edge, SendLog.peer_id.contains("@"),
            ~SendLog.peer_id.startswith("@"))).all():
        out.add(_company_key(peer))
    for handle, in sess.execute(select(Employer.handle_norm).where(
            Employer.do_not_contact.is_(True), Employer.handle_norm.contains("@"))).all():
        out.add(_company_key(handle))
    # Уже заведённые прямые заявки (в очереди или закрытые) — второй раз не заводим.
    for url, in sess.execute(select(Job.contact_url).where(Job.source.like(PREFIX + "%"))).all():
        out.add(_company_key(url))
    out.discard("")
    return out


def _company_key(addr: str) -> str:
    addr = (addr or "").replace("mailto:", "").strip().lower()
    if "@" not in addr:
        return ""
    domain = addr.rsplit("@", 1)[-1]
    return addr if domain in FREEMAIL else (org_domain(addr) or domain)


def company_targets(sess, limit: int = 30) -> list:
    """Компании с живой профильной вакансией, на которую нельзя откликнуться почтой."""
    from ..match import workformat
    from ..match.role import classify
    from ..match.scorer import score_job

    edge = utcnow() - timedelta(days=21)
    rows = sess.scalars(
        select(Job).where(Job.is_closed.is_(False), Job.last_seen_at >= edge,
                          Job.contact_kind.in_((ContactKind.EXTERNAL_URL.value,
                                                ContactKind.UNKNOWN.value)),
                          ~Job.source.like(PREFIX + "%"))
        .order_by(Job.id.desc()).limit(600)).all()
    seen, out = set(), []
    for job in rows:
        links = " ".join(str(x.get("value", "") if isinstance(x, dict) else x)
                         for x in (job.all_links_json or []))
        # Без названия компании ссылку не с чем сверить — такую вакансию пропускаем.
        if not (job.company_name or "").strip():
            continue
        site = people.site_of(job.description_raw or "", links, company=job.company_name)
        key = org_domain("x@" + site.split("//", 1)[-1]) if site else ""
        if not key or key in seen:
            continue
        score = score_job(job.title, job.tag, job.description_raw, source=job.source)
        if not score.recommend or not classify(job.title, job.tag, job.description_raw).supported:
            continue
        if workformat.detect(job.title, job.tag, job.description_raw or "",
                             source=job.source) == workformat.ONSITE:
            continue
        seen.add(key)
        out.append({"company": job.company_name or key, "site": site, "key": key,
                    "role": job.title or "", "jd_text": job.description_raw or "",
                    "job_id": job.id, "score": float(score.total)})
        if len(out) >= limit:
            break
    return out


def _context(contact, role: str, linked: list) -> str:
    """Описание заявки: откуда адрес и почему пишем. Хранится в Job.description_raw."""
    lines = ["Прямое письмо (без вакансии).",
             "Компания: %s" % (contact.company or "—"),
             "Адресат: %s%s" % (contact.person or "—",
                                (" — " + contact.person_role) if contact.person_role else ""),
             "Вид контакта: %s" % contact.kind,
             "Адрес опубликован здесь: %s" % contact.source_url]
    if role:
        lines.append("Открытая роль компании: %s" % role)
    if linked:
        lines.append("Связанные вакансии в базе: %s" % ", ".join("#%d" % i for i in linked))
    return "\n".join(lines)


def _create(sess, contact, *, role: str = "", jd_text: str = "", linked: list | None = None,
            score: float = 60.0, where: str = "profile") -> int:
    """Завести Job + Application под адресата. 0 — адресат не подошёл."""
    email = contact.email.lower()
    if sess.scalar(select(Job.id).where(Job.external_uuid == PREFIX + email)):
        return 0
    emp = sess.scalar(select(Employer).where(Employer.handle_norm == email))
    if emp is not None and emp.do_not_contact:
        return 0
    if emp is None:
        emp = Employer(handle_norm=email, handle_kind=ContactKind.EMAIL.value,
                       display_name=contact.company or "")
        sess.add(emp)
        sess.flush()
    kind = "referral" if contact.kind == people.REFERRAL else "exec"
    # Название часто приходит логином GitHub («inato»): в теме письма строчная
    # буква выглядит как рассылка. Бренд целиком не угадать, первую букву — можно.
    company = (contact.company or "").strip()
    if company and company == company.lower() and company[0].isalpha():
        company = company[0].upper() + company[1:]
    contact.company = company
    job = Job(external_uuid=PREFIX + email, source=PREFIX + kind, title=role or "",
              title_norm=norm_keep_digits(role or ""), company_name=contact.company or "",
              description_raw=_context(contact, role, linked or []),
              contact_kind=ContactKind.EMAIL.value, contact_url=email, posted_at=0, mode="full",
              raw_json={"person": contact.person, "person_role": contact.person_role,
                        "contact_kind": contact.kind, "email_source_url": contact.source_url,
                        "linked_job_ids": linked or [], "jd_text": (jd_text or "")[:6000],
                        "where": where},
              last_seen_at=utcnow())
    sess.add(job)
    sess.flush()
    app = Application(job_id=job.id, employer_id=emp.id, status=Status.DISCOVERED.value,
                      score=float(score))
    sess.add(app)
    sess.flush()
    return app.id


def discover(limit: int = 10, fetcher=None, dry: bool = False) -> dict:
    """Найти до limit новых адресатов: сайты компаний, затем GitHub."""
    fetcher = fetcher or people.Fetcher()
    search_key = get_settings().tavily_api_key
    found: list = []
    stats = {"companies": 0, "sites_without_contact": 0, "created": 0, "github": 0}
    with session_scope() as sess:
        taken = _contacted_domains(sess)
        targets = [t for t in company_targets(sess, limit=limit * 4) if t["key"] not in taken]
    for t in targets:
        if stats["created"] >= limit:
            break
        stats["companies"] += 1
        contacts = people.find_company_contacts(t["site"], t["company"], fetcher)
        # Сайт не назвал человека — идём от дешёвого к дорогому: комментарии Hacker News
        # (один запрос, без ключа), участники GitHub-организации компании, поиск по вебу.
        named = (people.EXEC, people.HIRING, people.REFERRAL)
        if not any(c.kind in named for c in contacts):
            contacts = contacts + people.hn_people(t["company"], t["site"], fetcher)
        if not any(c.kind in named for c in contacts):
            contacts = contacts + people.github_org_people(t["company"], t["site"], fetcher)
        if not any(c.kind == people.EXEC for c in contacts) and search_key:
            contacts = contacts + people.search_people(t["company"], t["site"], search_key, fetcher)
        contacts = sorted(contacts, key=lambda c: (people._RANK[c.kind], c.email))
        if not contacts:
            stats["sites_without_contact"] += 1
            continue
        best = contacts[0]                                  # один человек на компанию
        found.append((t["company"], best.kind, best.email, best.source_url))
        if dry:
            stats["created"] += 1
            continue
        with session_scope() as sess:
            if _company_key(best.email) in _contacted_domains(sess):
                continue
            where = "hn" if best.source_url.startswith(people.HN_ITEM) else "profile"
            if _create(sess, best, role=t["role"], jd_text=t["jd_text"], linked=[t["job_id"]],
                       score=t["score"], where=where):
                stats["created"] += 1
    left = limit - stats["created"]
    if left > 0:
        for c in people.github_people(limit=left, fetcher=fetcher):
            found.append((c.company or c.person, c.kind, c.email, c.source_url))
            if dry:
                stats["github"] += 1
                continue
            with session_scope() as sess:
                if _company_key(c.email) in _contacted_domains(sess):
                    continue
                if _create(sess, c, where="profile"):
                    stats["github"] += 1
    return dict(stats, found=found)


# ─────────────────────────────────────── подготовка и автоодобрение ──

def _hold(app, reason: str) -> str:
    """Оставить заявку владельцу: причина видна в боте, сама не уйдёт."""
    app.review_note = ("прямое письмо: " + reason)[:200]
    return "удержано: " + reason


def prepare_one(app_id: int, fetcher=None) -> str:
    """Письмо, резюме, гейты и — если всё пройдено — одобрение. Короткий итог строкой."""
    from ..pipeline import _uid
    from ..tailor.direct_letter import compose, lang_for
    from ..tailor.render import render_cv, verify_parsable
    from ..tailor.select import tailor

    s = get_settings()
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id) if app else None
        if app is None or job is None or not is_direct(job) \
                or app.status != Status.DISCOVERED.value:
            return "не подходит"
        meta = dict(job.raw_json or {})
        email, source_url = (job.contact_url or "").lower(), meta.get("email_source_url", "")
        company, role, jd_text = job.company_name or "", job.title or "", meta.get("jd_text", "")
        kind = "referral" if job.source.endswith("referral") else "exec"
        seed_base = job.external_uuid

    # Источник адреса проверяется заново прямо перед одобрением: за день адрес
    # могли убрать со страницы, и писать на него после этого не стоит.
    if not source_url or not people.page_publishes(email, source_url, fetcher):
        with session_scope() as sess:
            sess.get(Application, app_id).advance(
                Status.WITHDRAWN, reason="адрес больше не опубликован по ссылке-источнику")
        return "закрыто: адрес не подтверждён источником"

    lang = lang_for(email, jd_text, meta.get("person", ""))
    with session_scope() as sess:
        corpus = [b for b, in sess.execute(
            select(Application.message_body).join(Job, Application.job_id == Job.id)
            .where(Job.source.like(PREFIX + "%"), Application.message_body != "",
                   Application.id != app_id)
            .order_by(Application.id.desc()).limit(30)).all()]
    letter = None
    for n in range(5):                     # другие варианты фраз, если вышло под копирку
        cand = compose(kind, company=company, role=role, person=meta.get("person", ""),
                       jd_text=jd_text, lang=lang, seed="%s:%d" % (seed_base, n),
                       where=meta.get("where", "profile"))
        letter = cand
        if cand.ok and max_similarity(cand.text, corpus) < SIMILARITY_MAX:
            break
    assert letter is not None
    res = tailor(role or (DEFAULT_ROLE_EN if lang == "en" else DEFAULT_ROLE_RU), "",
                 jd_text or DEFAULT_JD, lang=lang)

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        app.cv_lang, app.message_body = lang, letter.text
        if not letter.ok:
            app.gate_passed = False
            app.transition(Status.GATE_FAILED)
            app.gate_failures_json = [{"rule": f.rule_id, "term": f.offending}
                                      for f in letter.gate.hard] or \
                                     [{"rule": "message.quality", "detail": letter.problem}]
            return "гейт письма не пройден"
        if not res.ok or res.lang != lang:
            app.gate_passed = False
            app.transition(Status.GATE_FAILED)
            return "гейт резюме не пройден"
        cv_path, cv_hash = render_cv(res.render, s.cv_out,
                                     filename_hint="Hakobyan_%s_%s" % (res.cv_slug, _uid(seed_base)),
                                     unique_seed=seed_base)
        parsed = verify_parsable(cv_path, res.render)
        if lang == "ru" and s.base_cv_path and Path(s.base_cv_path).is_file():
            cv_path = str(Path(s.base_cv_path))            # как в pipeline: RU — резюме владельца
            cv_hash = hashlib.sha256(Path(cv_path).read_bytes()).hexdigest()
        app.cv_path, app.cv_sha256, app.gate_passed = cv_path, cv_hash, True
        app.transition(Status.PENDING_APPROVAL, gate_result=res.gate)
        if not parsed["ok"]:
            return _hold(app, "резюме не читается парсером")
        if max_similarity(letter.text, corpus) >= SIMILARITY_MAX:
            return _hold(app, "письмо слишком похоже на соседние")
        emp = sess.get(Employer, app.employer_id) if app.employer_id else None
        if emp is not None and emp.do_not_contact:
            return _hold(app, "контакт отмечен «не писать»")
        # hello@/info@ читает поддержка, а не тот, кто нанимает: без владельца не уходит.
        if meta.get("contact_kind") == people.GENERAL:
            return _hold(app, "общий ящик компании — решает владелец")
        app.review_note = ""
        app.transition(Status.APPROVED)
        app.approved_at = utcnow()
        return "одобрено"


def run(fetcher=None) -> dict:
    """Шаг автопилота: пополнить очередь на день вперёд и подготовить письма."""
    s = get_settings()
    if not s.direct_enabled:
        return {"skipped": "DIRECT_ENABLED=false"}
    with session_scope() as sess:
        reason = guard(sess)
        if reason:
            return {"blocked": reason}
        queued = int(sess.scalar(
            select(func.count(Application.id)).join(Job, Application.job_id == Job.id)
            .where(Job.source.like(PREFIX + "%"), Application.status == Status.APPROVED.value,
                   Application.sent_at.is_(None))) or 0)
        want = max(0, s.direct_daily_limit * 2 - queued)
    stats = discover(limit=want, fetcher=fetcher) if want else {"created": 0, "github": 0}
    stats.pop("found", None)
    with session_scope() as sess:
        todo = [a for a, in sess.execute(
            select(Application.id).join(Job, Application.job_id == Job.id)
            .where(Job.source.like(PREFIX + "%"),
                   Application.status == Status.DISCOVERED.value)).all()]
    results: dict = {}
    for app_id in todo:
        try:
            verdict = prepare_one(app_id, fetcher=fetcher).split(":")[0]
        except Exception as exc:                            # noqa: BLE001
            log.warning("прямое письмо #%d: %s: %s", app_id, type(exc).__name__, str(exc)[:160])
            verdict = "ошибка"
        results[verdict] = results.get(verdict, 0) + 1
    return dict(stats, queued_before=queued, prepared=results)


def held(limit: int = 8) -> list:
    """Письма, которые гейты не пропустили: ждут решения владельца."""
    with session_scope() as sess:
        rows = sess.execute(
            select(Application, Job).join(Job, Application.job_id == Job.id)
            .where(Job.source.like(PREFIX + "%"),
                   Application.status.in_((Status.PENDING_APPROVAL.value, Status.GATE_FAILED.value)))
            .order_by(Application.id.desc()).limit(limit)).all()
        return [{"id": a.id, "company": j.company_name or "—", "email": j.contact_url,
                 "reason": (a.review_note or "гейт не пройден").replace("прямое письмо: ", ""),
                 "can_approve": a.status == Status.PENDING_APPROVAL.value and bool(a.gate_passed)}
                for a, j in rows]


def upcoming(limit: int = 8) -> list:
    with session_scope() as sess:
        rows = sess.execute(
            select(Application, Job).join(Job, Application.job_id == Job.id)
            .where(Job.source.like(PREFIX + "%"), Application.status == Status.APPROVED.value,
                   Application.sent_at.is_(None))
            .order_by(Application.score.desc()).limit(limit)).all()
        return [{"id": a.id, "company": j.company_name or "—", "email": j.contact_url,
                 "who": (j.raw_json or {}).get("person_role", ""),
                 "source": (j.raw_json or {}).get("email_source_url", "")} for a, j in rows]


def funnel() -> dict:
    """{вид: {sent, replied, bounced}} — для решения «оставить или расширить» через 2 недели."""
    out: dict = {}
    with session_scope() as sess:
        rows = sess.execute(select(Application, Job).join(Job, Application.job_id == Job.id)
                            .where(Job.source.like(PREFIX + "%"),
                                   Application.sent_at.is_not(None))).all()
        bounced = {i for i, in sess.execute(select(SendLog.application_id)
                                            .where(SendLog.result == "bounce")).all()}
        for a, j in rows:
            row = out.setdefault(j.source[len(PREFIX):], {"sent": 0, "replied": 0, "bounced": 0})
            row["sent"] += 1
            row["replied"] += 1 if a.first_reply_at else 0
            row["bounced"] += 1 if a.id in bounced else 0
    return out


def approve_held(app_id: int) -> str:
    """Владелец сам пропускает удержанное письмо (гейт правды при этом должен быть пройден)."""
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id) if app else None
        if app is None or not is_direct(job):
            return "не найдено"
        if app.status != Status.PENDING_APPROVAL.value or not app.gate_passed:
            return "нельзя одобрить: гейт правды не пройден"
        app.review_note = ""
        app.transition(Status.APPROVED)
        app.approved_at = utcnow()
    return "одобрено"


def block_company(app_id: int) -> str:
    """«Не писать этой компании»: адрес в стоп-лист, неотправленные письма сняты."""
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id) if app else None
        if app is None or job is None or not is_direct(job):
            return "не найдено"
        key = _company_key(job.contact_url)
        n = 0
        for a, j in sess.execute(select(Application, Job).join(Job, Application.job_id == Job.id)
                                 .where(Job.source.like(PREFIX + "%"))).all():
            if _company_key(j.contact_url) != key:
                continue
            emp = sess.get(Employer, a.employer_id) if a.employer_id else None
            if emp is not None:
                emp.do_not_contact = True
            if not a.sent_at and a.advance(Status.WITHDRAWN, reason="владелец: не писать этой компании"):
                n += 1
    return "компания в стоп-листе, снято писем: %d" % n


def is_paused() -> bool:
    with session_scope() as sess:
        return bool(paused_reason(sess))


def status_text() -> str:
    s = get_settings()
    with session_scope() as sess:
        reason = paused_reason(sess)
        counts = dict(sess.execute(
            select(Application.status, func.count()).join(Job, Application.job_id == Job.id)
            .where(Job.source.like(PREFIX + "%")).group_by(Application.status)).all())
        logs = _logs(sess)
        lines = ["Прямые письма: %s%s" % ("включены" if s.direct_enabled else "ВЫКЛЮЧЕНЫ (DIRECT_ENABLED)",
                                           (" · ПАУЗА — " + reason) if reason else ""),
                 "Сегодня: %d из %d" % (sent_today(sess), daily_limit(sess)),
                 "Всего отправлено: %d · отбивок: %d" % (
                     sum(1 for r, _ in logs if r == "ok"), sum(1 for r, _ in logs if r == "bounce")),
                 "Заявки: %s" % (", ".join("%s %d" % kv for kv in sorted(counts.items())) or "нет")]
    return "\n".join(lines)


def digest_text() -> str:
    """Кому сегодня ушли прямые письма — сводка для бота. Пусто, если никому."""
    with session_scope() as sess:
        rows = sess.execute(
            select(Application, Job).join(Job, Application.job_id == Job.id)
            .join(SendLog, SendLog.application_id == Application.id)
            .where(Job.source.like(PREFIX + "%"), SendLog.result == "ok",
                   SendLog.attempted_at >= _day_start()).order_by(SendLog.id)).all()
        if not rows:
            return ""
        lines = ["✉️ Прямые письма сегодня: %d" % len(rows), ""]
        for app, job in rows:
            meta = job.raw_json or {}
            who = " — ".join(x for x in (meta.get("person", ""), meta.get("person_role", "")) if x)
            lines.append("#%d %s%s" % (app.id, job.company_name or "—", (" · " + who) if who else ""))
            lines.append("   %s" % job.contact_url)
            lines.append("   источник: %s" % meta.get("email_source_url", "—"))
        lines += ["", "Остановить канал: экран «Прямые письма» → Пауза."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Прямые письма руководителям и рефералам")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--dry", action="store_true", help="с --discover: только показать, ничего не заводить")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--run", action="store_true", help="найти, подготовить и одобрить (как шаг автопилота)")
    ap.add_argument("--pause", metavar="ПРИЧИНА")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--digest", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # по строке на запрос — шум
    if args.pause:
        pause(args.pause)
    if args.resume:
        resume()
    if args.discover:
        st = discover(limit=args.limit, dry=args.dry)
        for company, kind, email, src in st.pop("found", []):
            print("%-8s %-28s %-34s %s" % (kind, (company or "")[:28], email, src))
        print(st)
    if args.run:
        print(run())
    if args.digest:
        print(digest_text() or "сегодня прямых писем не было")
    if args.status or not (args.discover or args.run or args.digest):
        print(status_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
