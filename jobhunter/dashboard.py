"""Операционные экраны: готовность отправки, диалоги, покрытие, результаты."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .config import get_settings
from .db import session_scope
from .models import (
    Application,
    ContactKind,
    Employer,
    Job,
    Message,
    OwnerRequest,
    RuntimeState,
    SendLog,
    Status,
    TelegramChannelStat,
)
from .outreach import eligibility

ATTENTION_CLOSED = {Status.WITHDRAWN.value, Status.REJECTED_SCORE.value,
                    Status.REJECTED_BY_EMPLOYER.value, Status.DUPLICATE.value,
                    Status.HANDLE_DEAD.value}


def _mail_ready() -> bool:
    from .convo import gmailapi
    return gmailapi.sending_configured()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def next_attempts() -> dict:
    """Живой график демона; при отсутствии данных — явно помеченный прогноз."""
    from .health import check
    s = get_settings()
    tz = ZoneInfo(s.owner_tz)
    now = datetime.now(tz)
    with session_scope() as sess:
        rows = sess.scalars(select(RuntimeState).where(
            RuntimeState.key.like("schedule:%"))).all()
    result = {}
    for channel, jobs, times in (("telegram", {"tg1", "tg2", "tg_more"}, [(11, 15), (16, 40)]),
                                  ("email", {"email"}, [(10, 30)])):
        candidates = [r.next_run_at.replace(tzinfo=timezone.utc) for r in rows
                      if r.key.split(":", 1)[1] in jobs and r.next_run_at
                      and r.finished_at and _now() - r.finished_at < timedelta(minutes=2)
                      and r.next_run_at.replace(tzinfo=timezone.utc) > now]
        estimated = not bool(candidates)
        if not candidates:
            for hh, mm in times:
                at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                candidates.append(at if at > now else at + timedelta(days=1))
        result[channel] = {"at": min(candidates).astimezone(tz).isoformat(),
                           "estimated": estimated, "scheduler_alive": check("autopilot", 420)}
    return result


def application_readiness(app, job, employer=None) -> dict:
    verdict = eligibility.check(app, job, employer)
    channel = ("telegram" if job and job.contact_kind == ContactKind.USER_HANDLE.value else
               "email" if job and job.contact_kind == ContactKind.EMAIL.value else "manual")
    return {"code": verdict.code, "reason": verdict.reason,
            "ready": verdict.allowed, "channel": channel,
            "retry_at": verdict.next_try_at}


def sending(limit: int = 200, offset: int = 0) -> dict:
    from .outreach import mailer, policy, sender
    from .report import quota
    s = get_settings()
    offset, limit = max(0, offset), max(1, limit)
    schedule = next_attempts()
    q = quota()
    chosen = {i["app_id"] for i in sender.pick_batch(10000)}
    chosen.update(i["app_id"] for i in mailer.pick_batch(10000))
    with session_scope() as sess:
        pairs = sess.execute(select(Application, Job).join(Job).where(
            Application.status.in_([Status.APPROVED.value, Status.PENDING_APPROVAL.value,
                                   Status.SENDING.value, Status.SEND_FAILED.value,
                                   Status.SEND_FAILED_AMBIGUOUS.value,
                                   Status.FOLLOWUP_PENDING_APPROVAL.value]))
            .order_by(Application.score.desc(), Application.id.desc())).all()
        employers = {e.id: e for e in sess.scalars(select(Employer)).all()}
        last = sess.scalar(select(SendLog).where(SendLog.result == "ok")
                           .order_by(SendLog.attempted_at.desc()).limit(1))
        items: list[dict] = []
        reasons: Counter[str] = Counter()
        ready = 0
        stop = policy.kill_switch_active()
        for app, job in pairs:
            state = application_readiness(app, job, employers.get(app.employer_id))
            if state["ready"] and app.id not in chosen:
                state.update(ready=False, code="batch_filter",
                             reason="Другой отклик этому работодателю имеет приоритет или контакт отфильтрован")
            if state["ready"]:
                ready += 1
                channel = state["channel"]
                if stop:
                    state.update(ready=False, code="kill_switch", reason="Включён стоп отправки")
                elif channel == "telegram" and not q["can_send"]:
                    state.update(ready=False, code="quota", reason=q["verdict"],
                                 retry_at=q.get("locked_until"))
                elif channel == "email" and q["email_sent"] >= q["email_cap"]:
                    state.update(ready=False, code="quota", reason="Дневной лимит email исчерпан")
                elif channel == "telegram" and not (s.tg_api_id and s.telegram_api_hash):
                    state.update(ready=False, code="configuration", reason="Telegram не настроен")
                elif channel == "email" and not _mail_ready():
                    state.update(ready=False, code="configuration", reason="Почта не настроена")
                elif not schedule[channel]["scheduler_alive"]:
                    state.update(ready=False, code="scheduler_offline",
                                 reason="Нет свежего пульса планировщика; время запуска не подтверждено")
                else:
                    state.update(code="scheduled", reason="Готово; ожидает запуска по расписанию")
            reasons[state["code"]] += 1
            items.append(dict(id=app.id, title=job.title or job.tag, score=app.score,
                              status=app.status, source=job.source,
                              next_attempt=schedule.get(state["channel"], {}).get("at")
                              if state["ready"] else None, **state))
        return {"total": len(items), "eligible": ready, "reasons": dict(reasons),
                "schedule": schedule, "quota": q, "items": items[offset:offset + limit],
                "offset": offset, "limit": limit, "has_more": offset + limit < len(items),
                "last_success": {"at": last.attempted_at, "application_id": last.application_id}
                if last else None}


def attention(limit: int = 200, offset: int = 0) -> dict:
    """All unresolved tasks, ordered by urgency then age, with a stable total."""
    from . import taskhub
    offset, limit = max(0, offset), max(1, min(limit, 2000))
    now = _now()
    with session_scope() as sess:
        requests: dict[int, list] = {}
        for req in sess.scalars(select(OwnerRequest).order_by(OwnerRequest.id.desc())):
            if req.application_id:
                requests.setdefault(req.application_id, []).append(req)
        messages: dict[int, list] = {}
        for msg in sess.scalars(select(Message).order_by(Message.id.desc())):
            messages.setdefault(msg.application_id, []).append(msg)
        candidate_ids = set(requests) | set(messages)
        items = []
        for app, job in sess.execute(select(Application, Job).outerjoin(Job).where(
                Application.status.notin_(ATTENTION_CLOSED),
                Application.status.in_((Status.NEEDS_HUMAN.value, Status.REPLIED.value,
                                       Status.INTERVIEW_PROPOSED.value, Status.SEND_FAILED.value,
                                       Status.SEND_FAILED_AMBIGUOUS.value,
                                       Status.PENDING_APPROVAL.value)) |
                Application.id.in_(candidate_ids))):
            row = taskhub._detail(sess, app, job, requests.get(app.id, []),
                                  messages.get(app.id, []), now)
            if row["needs_attention"]:
                items.append(row)
    items.sort(key=lambda row: (row["priority"], -(row["waiting_hours"] or 0), row["id"]))
    return {"total": len(items), "items": items[offset:offset + limit],
            "offset": offset, "limit": limit, "has_more": offset + limit < len(items)}


def reading() -> dict:
    from .report import telegram_health
    s = get_settings()
    with session_scope() as sess:
        channels = sess.scalars(select(TelegramChannelStat)
                               .order_by(TelegramChannelStat.username)).all()
        states = {r.key: r for r in sess.scalars(select(RuntimeState).where(
            RuntimeState.key.in_(["gmail", "telegram_inbox"]))).all()}
        pending = dict(sess.execute(select(
            (Message.email_uid > 0).label("email"), func.count(Message.id)).where(
            Message.direction == "in", Message.processing_pending.is_(True))
            .group_by(Message.email_uid > 0)).all())
    result: dict = {"telegram": telegram_health(), "channels": [
        {"username": r.username, "status": r.last_status, "at": r.last_finished_at,
         "posts": r.last_posts, "vacancies": r.last_vacancies,
         "rejected": r.rejected_posts if r.newest_post_id or not r.last_posts else None,
         "pages": r.last_pages, "oldest_id": r.oldest_post_id, "newest_id": r.newest_post_id,
         "history_complete": r.history_complete, "remaining": 0 if r.history_complete else None,
         "error": r.last_error} for r in channels]}
    for key in ("gmail", "telegram_inbox"):
        row = states.get(key)
        result[key] = {"status": row.status if row else "never",
                       "at": row.finished_at if row else None,
                       "started_at": row.started_at if row else None,
                       "pending_processing": pending.get(key == "gmail", 0),
                       "error": row.error if row else "",
                       "details": row.details_json if row else {"remaining": None}}
    result["gmail"].update(folder=s.imap_folder, initial_lookback_days=s.inbox_lookback_days)
    result["scope_note"] = ("Публичные Telegram-каналы и рабочие диалоги; Gmail: указанная папка. "
                            "Остаток относится к загрузке после сохранённой границы, а не ко всей истории. "
                            "Неизвестный остаток не означает, что всё прочитано.")
    return result


def outcomes(days: int = 90, track: str = "all", offset: int = 0, limit: int = 50) -> dict:
    """Shared results aggregation, retaining days/sent/categories/note for callers."""
    from . import feedback, results
    data = dict(results.aggregate(days=days, track=track, offset=offset, limit=limit))
    data["feedback"] = feedback.summary()
    return data
