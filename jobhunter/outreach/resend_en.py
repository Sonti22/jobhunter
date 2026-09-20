"""Повтор на английском: зарубежным компаниям, кому в августе ушло русское письмо.

26.08 десять компаний из HN «Who is hiring» получили письмо и резюме на
русском — язык тогда выбирался неверно. Из тех, кому HN-письмо ушло на
английском, ответил каждый пятый; из получивших русское — никто. Решение
владельца: написать им ещё раз, на английском, 3–5 писем в день, и честно
сказать в первой строке, что первое письмо ушло не на том языке.

Состояние хранится в score_breakdown_json["resend_en"] — без новых колонок:
  body, cv_path, prepared_at  — подготовлено (гейт правды пройден)
  attempt_at, message_id      — отправка начата (фиксируется ДО SMTP)
  sent_at                     — отправлено
  skipped / ambiguous         — повторять нельзя, причина рядом

    python -m jobhunter.outreach.resend_en --list
    python -m jobhunter.outreach.resend_en --prepare
    python -m jobhunter.outreach.resend_en --send
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, ContactKind, Employer, Job, Message, SendLog, Status, utcnow
from . import policy

MARK = "resend_en"
DAILY = 4
OPENER = "Hi! Writing again — my first note went out in Russian by mistake, sorry about that."
_RU_TLD = (".ru", ".by", ".kz", ".su", ".xn--p1ai")
_LIVE = (Status.AWAITING_REPLY.value, Status.FOLLOWED_UP.value, Status.NO_REPLY_CLOSED.value)


def _state(app) -> dict:
    return dict((app.score_breakdown_json or {}).get(MARK) or {})


def _save(app, **changes) -> None:
    data = dict(app.score_breakdown_json or {})
    data[MARK] = dict(_state(app), **changes)
    app.score_breakdown_json = data          # новый dict: иначе JSON-поле не помечается грязным


def _addr(job) -> str:
    return (job.contact_url or "").replace("mailto:", "").strip().lower()


def _blocked(sess, app, job) -> str:
    """Причина не писать повторно. Пусто — можно."""
    from ..ingest.postkind import is_seeker_post
    if job.source != "hn" or job.contact_kind != ContactKind.EMAIL.value:
        return "не HN-почта"
    if (app.cv_lang or "ru") != "ru" or not app.sent_at:
        return "русского письма не было"
    if app.first_reply_at or app.last_inbound_at:
        return "компания уже ответила"
    if app.status not in _LIVE:
        return "заявка закрыта: %s" % app.status
    addr = _addr(job)
    if "@" not in addr or addr.endswith(_RU_TLD):
        return "нет зарубежного адреса"
    emp = sess.get(Employer, app.employer_id) if app.employer_id else None
    if emp is not None and emp.do_not_contact:
        return "контакт отмечен «не писать»"
    if sess.scalar(select(SendLog.id).where(SendLog.application_id == app.id,
                                            SendLog.result == "bounce").limit(1)):
        return "адрес отбивает почту"
    if job.is_closed:
        return "вакансия закрыта"
    if is_seeker_post((job.title or "") + "\n" + (job.description_raw or "")):
        return "автор поста — соискатель"
    return ""


def candidates(sess) -> list:
    rows = sess.execute(select(Application, Job).join(Job, Application.job_id == Job.id)
                        .where(Job.source == "hn", Application.sent_at.is_not(None))
                        .order_by(Application.score.desc())).all()
    out = []
    for app, job in rows:
        st = _state(app)
        if st.get("sent_at") or st.get("skipped") or st.get("ambiguous") or st.get("attempt_at"):
            continue
        if not _blocked(sess, app, job):
            out.append(app.id)
    return out


def prepare_one(app_id: int) -> str:
    """EN-письмо и EN-резюме тем же конвейером и тем же гейтом, что и обычный отклик."""
    from ..match import workformat
    from ..match.explain import approval_problem
    from ..match.scorer import score_job
    from ..pipeline import _recent_message_corpus, _uid
    from ..tailor.llm_writer import quality_problem
    from ..tailor.message import generate, source_label, with_cv_attached
    from ..tailor.render import render_cv, verify_parsable
    from ..tailor.roletitle import display_role
    from ..tailor.select import tailor

    s = get_settings()
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id) if app else None
        if app is None or job is None:
            return "нет заявки"
        problem = _blocked(sess, app, job)
        score = score_job(job.title, job.tag, job.description_raw, source=job.source)
        if not problem and not score.recommend:
            problem = "вакансия больше не проходит отбор: %s" % (score.reason or "скор")
        if not problem and workformat.detect(job.title, job.tag, job.description_raw or "",
                                             source=job.source) == workformat.ONSITE:
            problem = "только офис"
        if not problem:
            problem = approval_problem(app, job)
        if problem:
            _save(app, skipped=problem[:200])
            return "пропуск: " + problem[:120]

        res = tailor(job.title, job.tag, job.description_raw, lang="en")
        if not res.ok or res.lang != "en":
            _save(app, skipped="гейт резюме не пройден")
            return "пропуск: гейт резюме"
        cv_path, _ = render_cv(res.render, s.cv_out,
                               filename_hint="Hakobyan_%s_%s" % (res.cv_slug, _uid(job.external_uuid)),
                               unique_seed=job.external_uuid + ":en")
        if not verify_parsable(cv_path, res.render)["ok"]:
            _save(app, skipped="резюме не читается парсером")
            return "пропуск: резюме не читается"
        role = display_role(job.title or "", job.tag or "", job.description_raw or "", "en")
        msg = generate(role, job.description_raw or "", score,
                       seed_str=job.external_uuid + ":resend-en",
                       recent_corpus=_recent_message_corpus(sess),
                       source=source_label(job.source, lang="en"), lang="en")
        body = OPENER + "\n\n" + with_cv_attached(msg.text, "en")
        bad = quality_problem(msg.text)
        if not msg.ok or bad:
            _save(app, skipped="гейт письма: %s" % (bad or "не прошло")[:150])
            return "пропуск: гейт письма"
        _save(app, body=body, cv_path=cv_path, prepared_at=utcnow().isoformat())
        return "готово: %s" % role


def send(limit: int = DAILY, dry: bool = False) -> dict:
    """До limit писем за запуск, в пределах общего дневного потолка почты."""
    from zoneinfo import ZoneInfo

    from . import archive
    from .mailer import (
        _never_reached_server,
        _smtp_close,
        _smtp_connect,
        _socket_already_dead,
        _stable_message_id,
        _subject,
        build_message,
        smtp_session,
    )

    s = get_settings()
    stats = {"sent": 0, "skipped": 0, "errors": 0}
    if not dry and not policy.within_send_window(datetime.now(ZoneInfo("Europe/Moscow")).hour):
        return dict(stats, blocked="вне окна 09-21 МСК")
    with session_scope() as sess:
        verdict = policy.can_send_email(sess)
        if not verdict.allowed:
            return dict(stats, blocked=verdict.reason)
        room = policy.email_daily_cap(sess) - policy.email_sent_today(sess)
        ready = [a.id for a in sess.scalars(
            select(Application).where(Application.id.in_(candidates(sess)))).all()
            if _state(a).get("body")]
    batch = ready[:max(0, min(limit, room))]
    if not batch:
        return stats
    from ..convo import gmailapi
    if not dry and not gmailapi.sending_configured():
        return dict(stats, blocked="SMTP не настроен")

    domain = (s.smtp_user or "localhost").rsplit("@", 1)[-1]
    rng = random.Random()
    with (smtp_session() if not dry else _nullcontext()) as opened:
        server = opened
        for i, app_id in enumerate(batch):
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                job = sess.get(Job, app.job_id)
                problem = _blocked(sess, app, job)
                if problem or not policy.can_send_email(sess).allowed:
                    stats["skipped"] += 1
                    continue
                st = _state(app)
                addr, subj = _addr(job), _subject(job, "en")
                title, company = job.title or "", job.company_name or ""
                mid = "<jobhunter-%d-resend-en@%s>" % (app.id, domain)
                msg = build_message(to=addr, subject=subj, body=st["body"], cv_path=st["cv_path"],
                                    app_id=app.id, in_reply_to=_stable_message_id(app.id),
                                    references=list(app.email_thread_refs or []), message_id=mid)
                if dry:
                    print("  [dry-run] #%d → %s | %s" % (app.id, addr, subj[:60]))
                    stats["sent"] += 1
                    continue
                # Отметка ДО сети: упади процесс посреди SMTP — повтора вслепую не будет.
                _save(app, attempt_at=utcnow().isoformat(), message_id=mid)
            try:
                try:
                    server.send_message(msg)
                except Exception as e:                      # noqa: BLE001
                    # Gmail закрывает простаивающую сессию между письмами (паузы —
                    # минуты). Сокет умер до письма — переподключаемся один раз.
                    if not _socket_already_dead(e):
                        raise
                    if server is not opened:
                        _smtp_close(server)
                    server = _smtp_connect()
                    server.send_message(msg)
            except Exception as e:                          # noqa: BLE001
                stats["errors"] += 1
                with session_scope() as sess:
                    app = sess.get(Application, app_id)
                    if _never_reached_server(e) or _socket_already_dead(e):
                        _save(app, attempt_at="")        # письмо точно не ушло — завтра ещё раз
                    else:
                        _save(app, ambiguous=type(e).__name__)
                    sess.add(SendLog(application_id=app_id, result="error",
                                     error_class=type(e).__name__, peer_id=addr))
                continue
            with session_scope() as sess:
                app = sess.get(Application, app_id)
                now = utcnow()
                _save(app, sent_at=now.isoformat())
                app.last_outbound_at = now
                refs = list(app.email_thread_refs or [])
                app.email_thread_refs = (refs + [mid])[-10:]
                sess.add(SendLog(application_id=app_id, result="ok", peer_id=addr))
                sess.add(Message(application_id=app_id, direction="out", body=st["body"],
                                 is_auto=True, sent_at=now, email_message_id=mid,
                                 email_from=s.smtp_user, email_subject=subj))
                emp = sess.get(Employer, app.employer_id) if app.employer_id else None
                if emp is not None:
                    emp.last_contacted_at = now
                    emp.total_messages_sent += 1
            archive.record(app_id, "email", addr, st["body"], job_title=title,
                           company=company, cv_path=st["cv_path"], kind="resend-en")
            stats["sent"] += 1
            if i < len(batch) - 1:
                time.sleep(rng.uniform(60, 180))
        if server is not opened:
            _smtp_close(server)
    return stats


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def run() -> dict:
    """Шаг автопилота: подготовить недостающее на сегодня и отправить."""
    with session_scope() as sess:
        todo = [a.id for a in sess.scalars(select(Application).where(
            Application.id.in_(candidates(sess)))).all() if not _state(a).get("body")]
    prepared = [prepare_one(app_id) for app_id in todo[:DAILY * 2]]
    return dict(send(DAILY), prepared=len([p for p in prepared if p.startswith("готово")]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Повтор на английском для HN")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    with session_scope() as sess:
        ids = candidates(sess)
        print("кандидатов: %d" % len(ids))
        for app_id in ids:
            app = sess.get(Application, app_id)
            job = sess.get(Job, app.job_id)
            print("  #%d %s %s %s" % (app_id, (job.company_name or "")[:24], _addr(job),
                                     "подготовлено" if _state(app).get("body") else ""))
    if args.prepare:
        for app_id in ids:
            print("  #%d: %s" % (app_id, prepare_one(app_id)))
    if args.send:
        print(send(DAILY, dry=args.dry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
