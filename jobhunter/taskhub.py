"""Shared owner task reads and atomic card opening; never dispatch replies."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import select, text

from .db import session_scope
from .models import Application, Employer, Job, Message, OwnerRequest, OwnerRequestKind, Status

CLOSED = {Status.WITHDRAWN.value, Status.REJECTED_SCORE.value,
          Status.REJECTED_BY_EMPLOYER.value, Status.DUPLICATE.value,
          Status.HANDLE_DEAD.value, Status.NO_REPLY_CLOSED.value}
DIALOGUE = {Status.NEEDS_HUMAN.value, Status.REPLIED.value, Status.IN_DIALOGUE.value,
            Status.SENT.value, Status.AWAITING_REPLY.value, Status.FOLLOWED_UP.value,
            Status.INTERVIEW_PROPOSED.value, Status.INTERVIEW_CONFIRMED.value,
            Status.INTERVIEW_DONE.value, Status.OFFER.value}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _expired(req, now):
    return req.decision == "expired" or (not req.decision and req.expires_at
                                         and req.expires_at <= now)


def _pending(requests, now):
    # A newer historical/expired card must not hide an older in-flight decision.
    return next((r for r in requests if r.applied_at is None
                 and not _expired(r, now) and r.decision not in ("skip", "close")), None)


def _manual(app):
    return (app.outcome or "").startswith("manual_tg_")


def _handle(app, job):
    packet = (app.apply_packet_json or {}).get("manual_telegram") or {}
    return str((packet.get("handle") if _manual(app) else "")
               or (job.contact_handle if job else "") or "").strip().lstrip("@").lower()


def _problem(sess, app, job, requests, messages):
    from .convo.send import reply_target_problem
    from .decisions import DELIVERY_UNCONFIRMED
    from .ingest.postkind import is_seeker_post

    if app.status in CLOSED or app.outcome == "manual_tg_skipped":
        return "Заявка закрыта: " + app.status
    if app.status in (Status.SENDING.value, Status.SEND_FAILED.value,
                      Status.SEND_FAILED_AMBIGUOUS.value) or app.outcome == "manual_tg_failed":
        return "Проверь доставку перед повторной отправкой"
    if any((r.apply_error or "").startswith(DELIVERY_UNCONFIRMED.split(":", 1)[0])
           for r in requests):
        return "Доставка ответа не подтверждена; проверь диалог. Автоповтор запрещён"
    if not job:
        return "Вакансия не найдена"
    if job.is_closed:
        return "Вакансия закрыта"
    if _manual(app):
        # reply_target_problem intentionally rejects ALL manual Telegram replies.
        # Check its other constraints independently, without altering that guard.
        if is_seeker_post((job.title or "") + "\n" + (job.description_raw or "")):
            return "Автор публикации — соискатель"
        employer = sess.get(Employer, app.employer_id) if app.employer_id else None
        if employer and employer.do_not_contact:
            return "Контакт отмечен «не писать»"
        handle = _handle(app, job)
        if (job.contact_kind != "user_handle" or
                not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", handle) or handle.endswith("bot")):
            return "Контакт не подходит для личного сообщения"
        if handle != (job.contact_handle or "").strip().lstrip("@").lower():
            return "Контакт изменился после выдачи; требуется проверка"
        if app.outcome != "manual_tg_sent" and not any(m.direction == "in" for m in messages):
            return "Нет сохранённого входящего или подтверждённого ручного отклика"
        return ""
    problem = reply_target_problem(sess, app)
    if problem:
        return problem
    # Historical sent applications may still have an approval status. Reading a
    # card must not fabricate SENDING/REPLIED/interview transitions to repair it.
    if app.status not in DIALOGUE and not (
            app.sent_at and app.status in (Status.APPROVED.value,
                                          Status.FOLLOWUP_PENDING_APPROVAL.value)):
        return "Диалог недоступен для новой карточки"
    return ""


def _detail(sess, app, job, requests, messages, now):
    """Build the same read model for a detail and the paginated attention list.

    All supplied rows belong to this application and are ordered newest first.
    """
    if (job is not None and app.status == Status.PENDING_APPROVAL.value
            and not app.sent_at and not app.send_attempts):
        from .match.explain import explain_job
        assessment = explain_job(job)
        reasons = list(assessment["review_reasons"])
        if assessment["track"] not in ("backend", "ml", "architect"):
            reasons.append("Дополнительное направление — требуется решение владельца")
        if app.review_note:
            reasons.append(app.review_note)
        if reasons:
            return {"id": app.id, "title": job.title or job.tag, "status": app.status,
                    "sent_at": None, "reason": "; ".join(reasons), "request_id": None,
                    "manual": False, "handle": _handle(app, job),
                    "can_open_card": False, "open_card_problem": "Это проверка отклика, не переписка",
                    "can_review_match": bool(app.gate_passed and not app.review_note),
                    "incoming": "Требования вакансии:\n" + job.description_raw,
                    "draft": app.message_body, "needs_attention": True,
                    "category": "matching", "priority": 5,
                    "waiting_hours": max(0, (now - app.updated_at).total_seconds() / 3600)
                    if app.updated_at else None}
    pending = _pending(requests, now)
    req = pending or (requests[0] if requests else None)
    incoming = next((m for m in messages if m.direction == "in"), None)
    outgoing = next((m for m in messages if m.direction == "out"), None)
    unfinished = [m for m in messages if m.direction == "in" and m.processing_pending]
    payload = dict(req.payload_json or {}) if req else {}
    draft_stale = bool(req and incoming and (
        ("incoming_message_ids" in payload and incoming.id not in payload["incoming_message_ids"])
        or (incoming.received_at and req.created_at and incoming.received_at > req.created_at)))
    problem = _problem(sess, app, job, requests, messages)
    expired = bool(req and _expired(req, now))
    unconfirmed = next((r.apply_error for r in requests if
                        (r.apply_error or "").startswith("delivery_unconfirmed")), "")
    error = unconfirmed or next((r.apply_error for r in requests if r.apply_error and
                  (req is None or r.id >= req.id or r.applied_at is None)), "")
    unanswered = bool(incoming and (not outgoing or incoming.id > outgoing.id))
    # A recorded decision covers the incoming known when the card was created.
    # A later incoming must remain visible even after skip/close or successful send.
    decided = bool(req and req.decision not in ("", "expired") and not error)
    if decided and incoming:
        covered = payload.get("incoming_message_ids", [])
        at = incoming.received_at
        if incoming.id in covered:
            unanswered = False
        elif "incoming_message_ids" not in payload:
            if (at and (req.answered_at or req.created_at)
                    and at <= (req.answered_at or req.created_at)) or (not at and not unfinished):
                unanswered = False
    active = bool(unfinished or unanswered or error or expired or pending)
    if not decided and app.status in (Status.NEEDS_HUMAN.value, Status.REPLIED.value,
                                      Status.INTERVIEW_PROPOSED.value,
                                      Status.SEND_FAILED.value,
                                      Status.SEND_FAILED_AMBIGUOUS.value):
        active = True
    active = active and app.status not in CLOSED and app.outcome != "manual_tg_skipped"

    if problem and ("доставк" in problem.lower() or "повтор" in problem.lower()):
        reason = problem
    elif error:
        reason = "Ответ не отправлен: " + error
    elif expired:
        reason = "Карточка истекла; диалог требует решения"
    elif unfinished:
        reason = "Входящее сохранено, но его разбор не завершён"
        if unfinished[0].processing_error:
            reason += ": " + unfinished[0].processing_error
    elif pending and pending.decision:
        reason = "Решение принято; ожидает отправки"
    elif pending:
        reason = "Ожидает твоего решения"
    elif active:
        reason = app.needs_human_reason or "Ответ рекрутёра требует внимания"
    else:
        reason = "Нет незавершённого решения"
    if _manual(app):
        reason += ". Переписку ведёт владелец; карточка только для чтения и черновика"
    if problem and problem not in reason:
        reason += ". " + problem
    if app.status == Status.INTERVIEW_PROPOSED.value or (
            pending and pending.kind == OwnerRequestKind.SLOT_CONFIRM.value):
        priority, category = 0, "interview"
    elif unfinished:
        priority, category = 2, "processing"
    elif error or app.status in (Status.SEND_FAILED.value, Status.SEND_FAILED_AMBIGUOUS.value):
        priority, category = 3, "error"
    elif expired:
        priority, category = 4, "expired"
    else:
        priority, category = 1, "incoming"
    waiting = (app.last_inbound_at or (incoming.received_at if incoming else None)
               or (req.created_at if req else app.updated_at))
    if unfinished:
        waiting = min((m.received_at for m in unfinished if m.received_at), default=waiting)
    return {"id": app.id, "title": (job.title or job.tag) if job else "",
            "status": app.status, "sent_at": app.sent_at, "reason": reason,
            "request_id": req.id if req else None,
            "manual": _manual(app), "handle": _handle(app, job),
            "can_open_card": not bool(problem), "open_card_problem": problem,
            "incoming": incoming.body if incoming else payload.get("incoming", ""),
            "draft": "" if draft_stale else payload.get("draft", ""),
            "draft_stale": draft_stale, "needs_attention": bool(active),
            "category": category, "priority": priority,
            "waiting_hours": max(0, (now - waiting).total_seconds() / 3600) if waiting else None}


def _rows(sess, app_id):
    requests = sess.scalars(select(OwnerRequest).where(OwnerRequest.application_id == app_id)
                            .order_by(OwnerRequest.id.desc())).all()
    messages = sess.scalars(select(Message).where(Message.application_id == app_id)
                            .order_by(Message.id.desc())).all()
    return requests, messages


def detail(app_id: int) -> dict | None:
    """Owner-only read view. Does not mark messages, decisions or result events."""
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if app is None:
            return None
        return _detail(sess, app, sess.get(Job, app.job_id), *_rows(sess, app_id), _now())


def _manual_request(sess, app, job, incoming, draft, reason, now):
    from .config import get_settings

    handle = _handle(app, job)
    req = OwnerRequest(application_id=app.id, kind=OwnerRequestKind.NEEDS_HUMAN.value,
                       payload_json={"manual_reply": True, "handle": handle,
                                     "incoming": incoming, "draft": draft, "reason": reason},
                       question=(f"#{app.id} · {job.title or job.tag}\n@{handle}\n"
                                 "Переписку ведёшь вручную. Только чтение и черновик.\n\n"
                                 f"Рекрутёр: {incoming}\n\nЧерновик: {draft or 'не подготовлен'}"),
                       expires_at=now + timedelta(hours=max(
                           24, get_settings().owner_decision_ttl_hours)))
    sess.add(req)
    sess.flush()
    _queue_manual(sess, req)
    return req


def prepare_manual_request(sess, req) -> bool:
    """For owner.py: call before _to_bot for engine-created manual cards.

    This uses the caller's transaction and never queues or sends anything.
    """
    app = sess.get(Application, req.application_id) if req.application_id else None
    if app is None or not _manual(app):
        return False
    job = sess.get(Job, app.job_id)
    incoming_ids = list(sess.scalars(select(Message.id).where(
        Message.application_id == app.id, Message.direction == "in")))
    payload = dict(req.payload_json or {}, manual_reply=True, handle=_handle(app, job),
                   incoming_message_ids=incoming_ids)
    req.payload_json = payload
    req.question = (f"#{app.id} · @{payload['handle']}\nТолько чтение и черновик.\n"
                    f"Рекрутёр: {payload.get('incoming', '')}\nЧерновик: {payload.get('draft', '')}")
    return True


def _queue_manual(sess, req):
    from . import notify
    from .bot.cards import keyboard_for

    markup = keyboard_for(req)
    # Defense at the queue boundary, including during a rolling update of cards.
    # A legacy keyboard must never enqueue send/say or slot-confirmation actions.
    safe = {f"d:{req.id}:skip", f"d:{req.id}:close",
            f"s:work_task_{req.application_id}", f"w:manual:{req.application_id}:{req.id}"}
    handle = req.payload_json["handle"]
    url = "https://t.me/" + handle
    rows = []
    for row in markup.get("inline_keyboard", []):
        buttons = [b for b in row if b.get("callback_data") in safe or
                   b.get("url", "").split("?", 1)[0] == url]
        if buttons:
            rows.append(buttons)
    if not any(b.get("url") for row in rows for b in row):
        rows.insert(0, [{"text": "Открыть @" + handle + " с черновиком",
                         "url": url + "?" + urlencode({"text": req.payload_json.get("draft", "")})}])
    notify.push_card(req, markup={"inline_keyboard": rows}, text=req.question, sess=sess)


def open_card(app_id: int) -> dict:
    """Create/reuse a persisted owner card, serialized with every SQLite writer.

    Failures propagate after session_scope rolls back the card, queue and message
    flags together. Opening is never evidence of a reply or a business milestone.
    """
    with session_scope() as sess:
        sess.execute(text("BEGIN IMMEDIATE"))
        app = sess.get(Application, app_id)
        if app is None:
            return {"ok": False, "note": "Заявка не найдена", "request_id": None, "manual": False}
        job = sess.get(Job, app.job_id)
        requests, messages = _rows(sess, app_id)
        now = _now()
        view = _detail(sess, app, job, requests, messages, now)
        result = {"ok": False, "note": view["open_card_problem"],
                  "request_id": view["request_id"], "manual": view["manual"]}
        if not view["can_open_card"]:
            return result
        req = _pending(requests, now)
        if req is not None and not view["manual"] and view.get("draft_stale"):
            if req.decision:
                return dict(result, note="Есть новое входящее; прежнее решение требует проверки")
            req.decision, req.answered_at = "expired", now
            req = None
        if req is not None:
            snapshot = (req.payload_json or {}).get("incoming_message_ids")
            refresh = snapshot is None or any(m.id not in snapshot for m in messages if m.direction == "in")
            if view["manual"] and (not (req.payload_json or {}).get("manual_reply") or refresh):
                # Never repurpose a queued send decision as a manual card.
                if req.decision:
                    return dict(result, note="Прежнее решение требует проверки перед ручной перепиской")
                req.payload_json = dict(req.payload_json or {}, incoming=view["incoming"],
                                        draft=view["draft"])
                prepare_manual_request(sess, req)
                # Replace only this request's undelivered queued keyboard.
                from .models import BotOutbox
                for row in sess.scalars(select(BotOutbox).where(
                        BotOutbox.owner_request_id == req.id, BotOutbox.sent_at.is_(None))):
                    row.markup_json = {"inline_keyboard": []}
                    row.text = req.question
                _queue_manual(sess, req)
                sess.flush()
                for msg in messages:
                    if msg.direction == "in" and msg.processing_pending:
                        msg.processing_pending = False
                        msg.processing_error = ""
            return dict(result, ok=True, request_id=req.id, note="Открытая карточка уже существует")
        for old in requests:
            if not old.decision and _expired(old, now):
                old.decision, old.answered_at = "expired", now
        incoming = "\n\n".join(m.body for m in reversed(messages)
                                 if m.direction == "in" and m.processing_pending)
        incoming = incoming or view["incoming"]
        if view["manual"]:
            req = _manual_request(sess, app, job, incoming, view["draft"], view["reason"], now)
        else:
            from .owner import create_human_request
            req = create_human_request(sess, app, job, incoming,
                                       "повторная проверка владельцем", view["draft"])
        if req is None or req.id is None:
            raise RuntimeError("Owner request was not persisted")
        req.payload_json = dict(req.payload_json or {}, incoming=incoming,
                                incoming_message_ids=[m.id for m in messages if m.direction == "in"])
        sess.flush()  # A durable handoff in this transaction must precede flag updates.
        for msg in messages:
            if msg.direction == "in" and msg.processing_pending:
                msg.processing_pending = False
                msg.processing_error = ""
        return dict(result, ok=True, request_id=req.id,
                    note="Карточка для чтения и черновика открыта" if view["manual"]
                    else "Карточка для решения открыта")


def complete_manual(app_id: int, req_id: int, actor_id: int) -> tuple[bool, str]:
    """Record the owner's report, never a transport receipt or a synthetic message."""
    from .config import get_settings

    if isinstance(actor_id, bool) or not actor_id or actor_id not in get_settings().bot_owner_ids:
        return False, "Подтверждение доступно только владельцу"
    with session_scope() as sess:
        sess.execute(text("BEGIN IMMEDIATE"))
        app = sess.get(Application, app_id)
        if app is None or not _manual(app):
            return False, "Это не ручная переписка владельца"
        requests, messages = _rows(sess, app_id)
        req = requests[0] if requests else None
        now = _now()
        if (not req or req.id != req_id or req.decision or req.applied_at
                or _expired(req, now) or not (req.payload_json or {}).get("manual_reply")):
            return False, "Карточка устарела или уже закрыта"
        problem = _problem(sess, app, sess.get(Job, app.job_id), requests, messages)
        if problem or req.apply_error:
            return False, problem or "Ошибка карточки требует отдельной проверки"
        snapshot = (req.payload_json or {}).get("incoming_message_ids")
        current = [m.id for m in messages if m.direction == "in"]
        if not isinstance(snapshot, list) or any(mid not in snapshot for mid in current):
            return False, "Есть новое входящее; сначала проверь актуальную карточку"
        req.decision = "manual_sent"
        req.answered_at = req.applied_at = now
        req.decided_by = f"bot:{actor_id}"
        req.decision_note = "Владелец подтвердил ручной ответ; доставка системой не проверялась"
        app.last_outbound_at = now
        if app.status in (Status.NEEDS_HUMAN.value, Status.REPLIED.value):
            app.transition(Status.IN_DIALOGUE)
        return True, "Ручной ответ отмечен со слов владельца"
