"""Операционные экраны: готовность отправки, диалоги, покрытие, результаты."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

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


def sending(limit: int = 200) -> dict:
    from .outreach import mailer, policy, sender
    from .report import quota
    s = get_settings()
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
                elif channel == "email" and not (s.smtp_user and s.smtp_app_password):
                    state.update(ready=False, code="configuration", reason="Почта не настроена")
                else:
                    state.update(code="scheduled", reason="Готово; ожидает запуска по расписанию")
            reasons[state["code"]] += 1
            items.append(dict(id=app.id, title=job.title or job.tag, score=app.score,
                              status=app.status, source=job.source,
                              next_attempt=schedule.get(state["channel"], {}).get("at")
                              if state["ready"] else None, **state))
        return {"total": len(items), "eligible": ready, "reasons": dict(reasons),
                "schedule": schedule, "quota": q, "items": items[:max(1, min(limit, 2000))],
                "last_success": {"at": last.attempted_at, "application_id": last.application_id}
                if last else None}


def attention(limit: int = 200) -> dict:
    """Истёкшая карточка и неотправленное решение остаются видимыми."""
    now = _now()
    with session_scope() as sess:
        requests = sess.scalars(select(OwnerRequest).order_by(OwnerRequest.id.desc())).all()
        unfinished = {m.application_id: m for m in sess.scalars(select(Message).where(
            Message.direction == "in", Message.processing_pending.is_(True))).all()}
        latest: dict[int, OwnerRequest] = {}
        for req in requests:
            if req.application_id:
                latest.setdefault(req.application_id, req)
        items = []
        for app, job in sess.execute(select(Application, Job).join(Job).where(
                Application.status.in_([Status.NEEDS_HUMAN.value, Status.REPLIED.value,
                                       Status.SEND_FAILED_AMBIGUOUS.value,
                                       Status.INTERVIEW_PROPOSED.value]) |
                Application.id.in_([r.application_id for r in requests
                                    if r.application_id and (r.apply_error or not r.decision)] +
                                   list(unfinished)))).all():
            req = latest.get(app.id)
            if req and req.decision in ("skip", "close") and app.id not in unfinished:
                continue
            if req and req.apply_error:
                reason = "Ответ не отправлен: " + req.apply_error
            elif req and (req.decision == "expired" or
                          (not req.decision and req.expires_at and req.expires_at <= now)):
                reason = "Карточка истекла; диалог требует решения"
            elif app.status == Status.SEND_FAILED_AMBIGUOUS.value:
                reason = "Проверь доставку перед повторной отправкой"
            elif app.id in unfinished:
                reason = "Входящее сохранено, но его разбор не завершён"
                if unfinished[app.id].processing_error:
                    reason += ": " + unfinished[app.id].processing_error
            elif req and req.decision and not req.applied_at:
                reason = "Решение принято; ожидает отправки"
            elif req and not req.decision:
                reason = "Ожидает твоего решения"
            elif app.status == Status.NEEDS_HUMAN.value:
                reason = "Незавершённый диалог без открытой карточки"
            elif app.status in (Status.REPLIED.value, Status.INTERVIEW_PROPOSED.value):
                reason = "Ответ рекрутёра требует внимания"
            else:
                continue
            last = sess.scalar(select(Message).where(Message.application_id == app.id,
                                                      Message.direction == "in")
                               .order_by(Message.id.desc()).limit(1))
            waiting = app.last_inbound_at or (req.created_at if req else app.updated_at)
            items.append({"id": app.id, "title": job.title or job.tag, "status": app.status,
                          "reason": reason, "request_id": req.id if req else None,
                          "waiting_hours": max(0, (now - waiting).total_seconds() / 3600)
                          if waiting else None, "incoming": last.body if last else "",
                          "draft": (req.payload_json or {}).get("draft", "") if req else ""})
    items.sort(key=lambda x: x["waiting_hours"] or 0, reverse=True)
    return {"total": len(items), "items": items[:max(1, min(limit, 2000))]}


def reading() -> dict:
    from .report import telegram_health
    s = get_settings()
    with session_scope() as sess:
        channels = sess.scalars(select(TelegramChannelStat)
                               .order_by(TelegramChannelStat.username)).all()
        states = {r.key: r for r in sess.scalars(select(RuntimeState).where(
            RuntimeState.key.in_(["gmail", "telegram_inbox"]))).all()}
    result: dict = {"telegram": telegram_health(), "channels": [
        {"username": r.username, "status": r.last_status, "at": r.last_finished_at,
         "posts": r.last_posts, "vacancies": r.last_vacancies, "rejected": r.rejected_posts,
         "pages": r.last_pages, "oldest_id": r.oldest_post_id, "newest_id": r.newest_post_id,
         "history_complete": r.history_complete, "remaining": 0 if r.history_complete else None,
         "error": r.last_error} for r in channels]}
    for key in ("gmail", "telegram_inbox"):
        row = states.get(key)
        result[key] = {"status": row.status if row else "never",
                       "at": row.finished_at if row else None,
                       "error": row.error if row else "",
                       "details": row.details_json if row else {"remaining": None}}
    result["gmail"].update(folder=s.imap_folder, initial_lookback_days=s.inbox_lookback_days)
    result["scope_note"] = ("Публичные Telegram-каналы и рабочие диалоги; Gmail: указанная папка. "
                            "Неизвестный остаток не означает, что всё прочитано.")
    return result


def outcomes(days: int = 90) -> dict:
    """Одна заявка — одна категория, с более поздними бизнес-результатами в приоритете."""
    from .convo import classify as c
    since = _now() - timedelta(days=days)
    categories = Counter({k: 0 for k in ("no_reply", "interested", "cv_requested", "rejected",
                                        "interview", "offer", "other_reply")})
    with session_scope() as sess:
        apps = sess.scalars(select(Application).where(Application.sent_at >= since)).all()
        labels: dict[int, set] = {}
        for app_id, label in sess.execute(select(Message.application_id, Message.classifier_label)
                                          .where(Message.direction == "in",
                                                 Message.application_id.in_([a.id for a in apps]))):
            labels.setdefault(app_id, set()).add(label)
        for app in apps:
            seen = labels.get(app.id, set())
            if app.status == Status.OFFER.value:
                category = "offer"
            elif app.status == Status.REJECTED_BY_EMPLOYER.value:
                category = "rejected"
            elif app.interview_at_utc:
                category = "interview"
            elif c.ASK_CALL in seen or c.SLOT_PROPOSED in seen or c.TECH_QUESTION in seen:
                category = "interested"
            elif c.ASK_CV in seen:
                category = "cv_requested"
            elif app.first_reply_at:
                category = "other_reply"
            else:
                category = "no_reply"
            categories[category] += 1
    return {"days": days, "sent": len(apps), "categories": dict(categories),
            "note": "Интерес — сигнал классификатора. Интервью и оффер — подтверждённые данные заявки."}
