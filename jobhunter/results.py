"""Observed business milestones and read-only analytics for a shared send cohort.

Events describe facts, never paths through Application.advance(). Unknown event
times stay null. Legacy evidence is projected on reads, without a DB backfill.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, overload

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from .config import get_settings
from .db import session_scope
from .models import Application, Job, Message, OwnerRequest, ResultEvent, SendLog, Status

EVENT_KINDS = (
    "interested", "cv_requested", "interview_scheduled", "interview_done", "offer", "rejected",
)
EVENT_SOURCES = {"classifier", "owner", "calendar", "legacy"}
POSITIVE_KINDS = {"interested", "interview_scheduled", "interview_done", "offer"}
CATEGORY_KEYS = (
    "no_reply", "interested", "cv_requested", "rejected", "interview", "offer", "other_reply",
)
MIN_OBSERVATIONS = 20
MATURITY_DAYS = 14
_ACTIVE_STATUSES = {
    Status.SENT.value, Status.AWAITING_REPLY.value, Status.FOLLOWED_UP.value,
    Status.FOLLOWUP_PENDING_APPROVAL.value, Status.REPLIED.value, Status.IN_DIALOGUE.value,
    Status.NEEDS_HUMAN.value, Status.INTERVIEW_PROPOSED.value,
    Status.INTERVIEW_CONFIRMED.value, Status.INTERVIEW_DONE.value, Status.OFFER.value,
}


@overload
def _utc(value: datetime) -> datetime: ...


@overload
def _utc(value: None) -> None: ...


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() + "Z" if value is not None else None


def record_event(sess, app_id, kind, source, event_key, occurred_at=None, details=None) -> bool:
    """Insert once in the caller's transaction; return whether an event was added.

The unique key arbitrates concurrent writers without rolling back unrelated
work in the session. Reusing a key for another application/kind/source is an
error. Omitted occurred_at means unknown, not the time this function ran.
"""
    if kind not in EVENT_KINDS or source not in EVENT_SOURCES:
        raise ValueError("Unknown result kind or source")
    if not isinstance(event_key, str) or not event_key.strip():
        raise ValueError("An event key is required")
    if details is not None and not isinstance(details, dict):
        raise ValueError("Event details must be a dictionary")
    if not sess.get(Application, app_id):
        raise ValueError("Application does not exist")
    result = sess.execute(insert(ResultEvent).values(
        application_id=app_id, kind=kind, source=source, event_key=event_key,
        occurred_at=_utc(occurred_at), recorded_at=_now(), details_json=dict(details or {}),
    ).on_conflict_do_nothing(index_elements=["event_key"]))
    if result.rowcount:
        return True
    existing = sess.scalar(select(ResultEvent).where(ResultEvent.event_key == event_key))
    if (existing.application_id, existing.kind, existing.source) != (app_id, kind, source):
        raise ValueError("Event key already belongs to a different fact")
    return False


def owner_record(app_id, kind, actor_id, request_key) -> tuple[bool, str]:
    """Record an owner's explicit report and reconcile the workflow without sending.

Deduplication is per application/milestone, including retries with a new request
ID. The first actor and request remain the audit evidence; the occurrence time
is unknown. Transport delivery and intermediate milestones are never inferred.
"""
    if isinstance(actor_id, bool) or actor_id not in get_settings().bot_owner_ids:
        return False, "Нет доступа: отметку может поставить только владелец"
    if kind not in EVENT_KINDS or not isinstance(request_key, str) or not request_key.strip():
        return False, "Неизвестный результат или отсутствует ключ запроса"
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        app = sess.get(Application, app_id)
        if app is None:
            return False, "Заявка не найдена"
        key = "owner:%d:%s" % (app_id, kind)
        if sess.scalar(select(ResultEvent.id).where(ResultEvent.event_key == key)) is not None:
            return True, "Этот результат уже записан с твоих слов"
        if (not app.sent_at or _utc(app.sent_at) > _now()
                or app.status not in _ACTIVE_STATUSES
                or ((app.outcome or "").startswith("manual_tg_")
                    and app.outcome != "manual_tg_sent")):
            return False, "Результат можно отметить только для отправленной активной заявки"
        target = {"rejected": Status.REJECTED_BY_EMPLOYER, "offer": Status.OFFER,
                  "interview_done": Status.INTERVIEW_DONE}.get(kind)
        if target is not None and not app.advance(target, reason="Результат подтверждён владельцем"):
            return False, "Текущее состояние не позволяет подтвердить этот результат"
        if kind in ("rejected", "offer"):
            for req in sess.scalars(select(OwnerRequest).where(
                    OwnerRequest.application_id == app_id, OwnerRequest.applied_at.is_(None))):
                # Keep ambiguous-delivery evidence; never turn it into a retry.
                req.decision = "skip"
                req.answered_at = req.applied_at = _now()
                req.decided_by = f"bot:{actor_id}"
                req.decision_note = "Закрыто после подтверждения результата владельцем"
        added = record_event(sess, app_id, kind, "owner", key,
                             details={"actor_id": actor_id, "request_key": request_key,
                                      "evidence": "owner_report"})
        return True, ("Записано с твоих слов: " + kind if added
                      else "Этот результат уже записан с твоих слов")


def classifier_kind(label: str, confidence: float) -> str | None:
    """Only explicit positive intent signals; an ordinary reply is not interest.

Rejections need the engine's verified rejection branch. A slot proposal proves
interest, not a scheduled or held interview.
"""
    from .convo import classify as c
    if (confidence or 0) < c.CONFIDENCE_MIN:
        return None
    return {c.ASK_CV: "cv_requested", c.ASK_CALL: "interested",
            c.SLOT_PROPOSED: "interested", c.TECH_QUESTION: "interested",
            c.OFFER: "interested"}.get(label)


def _track(app, job) -> tuple[str, str]:
    breakdown = app.score_breakdown_json or {}
    assessment = breakdown.get("assessment") if isinstance(breakdown, dict) else None
    if isinstance(assessment, dict) and assessment.get("track"):
        return str(assessment["track"]), "saved"
    try:
        from .match.explain import assessment_for
    except ImportError:
        return "unknown", "unknown"
    assessment = assessment_for(app, job)
    if isinstance(assessment, dict) and assessment.get("track"):
        return str(assessment["track"]), "inferred"
    return "unknown", "unknown"


def _legacy(kind, app_id, evidence, occurred_at=None, **details) -> dict:
    return {"id": None, "application_id": app_id, "kind": kind, "source": "legacy",
            "event_key": "legacy:%d:%s:%s" % (app_id, kind, evidence),
            "occurred_at": _iso(occurred_at), "recorded_at": None,
            "historical": True, "details": {"evidence": evidence, **details}}


def _events(app, messages, stored, now) -> list[dict]:
    events = []
    since = _utc(app.sent_at)
    for event in stored:
        occurred = _utc(event.occurred_at)
        recorded = _utc(event.recorded_at)
        if event.kind not in EVENT_KINDS or (recorded and recorded > now):
            continue
        if occurred is not None and not since <= occurred <= now:
            continue
        kind = "interested" if event.kind == "offer" and event.source == "classifier" else event.kind
        events.append({"id": event.id, "application_id": app.id, "kind": kind,
                       "source": event.source, "event_key": event.event_key,
                       "occurred_at": _iso(occurred), "recorded_at": _iso(recorded),
                       "historical": event.source == "legacy",
                       "details": dict(event.details_json or {})})

    # Prefer the stored final classifier verdict over the initial regex label.
    from .convo import classify as c
    for msg in messages:
        received = _utc(msg.received_at)
        if msg.direction != "in" or (received is not None and not since <= received <= now):
            continue
        label, confidence = msg.classifier_label, msg.classifier_confidence
        if msg.llm_label and (msg.llm_confidence or 0) >= 0.75:
            label, confidence = msg.llm_label, msg.llm_confidence
        kind = classifier_kind(label, confidence)
        if (label == c.REJECTION and msg.classifier_label == c.REJECTION
                and (msg.classifier_confidence or 0) >= c.REJECTION_CLOSE_MIN
                and msg.llm_label == c.REJECTION and (msg.llm_confidence or 0) >= 0.8):
            kind = "rejected"
        if kind is None:
            continue
        if any(e["kind"] == kind and (e["details"].get("message_id") == msg.id
                                     or e["event_key"] == "message:%d:%s" % (msg.id, kind))
               for e in events):
            continue
        events.append(_legacy(kind, app.id, "message:%d" % msg.id, received,
                              message_id=msg.id, label=label, confidence=confidence))

    seen = {event["kind"] for event in events}
    if app.interview_at_utc and "interview_scheduled" not in seen:
        # This is the scheduled appointment time, not when scheduling happened.
        events.append(_legacy("interview_scheduled", app.id, "application.interview_at_utc",
                              interview_at_utc=_iso(app.interview_at_utc)))
    for status, kind in ((Status.OFFER.value, "offer"),
                         (Status.REJECTED_BY_EMPLOYER.value, "rejected")):
        if app.status == status and kind not in seen:
            events.append(_legacy(kind, app.id, "application.status", status=status))
    # In particular, OFFER and a past appointment do not prove INTERVIEW_DONE.
    # A held interview requires an explicit event, including for legacy records.
    return sorted(events, key=lambda e: (e["occurred_at"] or e["recorded_at"] or "",
                                         e["event_key"]), reverse=True)


def _send_proof(app, messages, logs, now) -> str:
    if (app.send_channel in ("telegram_manual", "manual") or app.outcome == "manual_tg_sent"
            or app.outcome == "applied"):
        return "owner_reported"
    if app.telegram_msg_id and app.telegram_msg_id > 0:
        return "transport_confirmed"
    since = _utc(app.sent_at)
    for msg in messages:
        sent = _utc(msg.sent_at)
        if (msg.direction == "out" and sent is not None and since <= sent <= now
                and (msg.telegram_msg_id or msg.email_message_id)):
            return "transport_confirmed"
    if any(log.result == "ok" and log.attempted_at is not None
           and since <= _utc(log.attempted_at) <= now for log in logs):
        return "transport_confirmed"
    return "unknown"


def _category(kinds, replied) -> str:
    if "offer" in kinds:
        return "offer"
    if "rejected" in kinds:
        return "rejected"
    if kinds & {"interview_scheduled", "interview_done"}:
        return "interview"
    if "interested" in kinds:
        return "interested"
    if "cv_requested" in kinds:
        return "cv_requested"
    return "other_reply" if replied else "no_reply"


def application_history(app_id: int) -> list[dict]:
    """Read explicit and supported historical evidence without a cohort cutoff."""
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if app is None or not app.sent_at or _utc(app.sent_at) > _now():
            return []
        messages = sess.scalars(select(Message).where(Message.application_id == app_id)).all()
        stored = sess.scalars(select(ResultEvent).where(ResultEvent.application_id == app_id)).all()
        return _events(app, messages, stored, _now())


def _cohort(days=90, *, mature=False) -> list[dict]:
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError("days must be a positive integer")
    now = _now()
    since = now - timedelta(days=days)
    until = now - timedelta(days=MATURITY_DAYS) if mature else now
    cohort = select(Application.id).where(Application.sent_at >= since,
                                         Application.sent_at <= until)
    with session_scope() as sess:
        apps = sess.execute(select(Application, Job).join(Job, Application.job_id == Job.id)
                            .where(Application.id.in_(cohort))
                            .order_by(Application.sent_at.desc(), Application.id.desc())).all()
        messages: dict[int, list[Any]] = defaultdict(list)
        stored: dict[int, list[Any]] = defaultdict(list)
        logs: dict[int, list[Any]] = defaultdict(list)
        for model, target in ((Message, messages), (ResultEvent, stored), (SendLog, logs)):
            for row in sess.scalars(select(model).where(model.application_id.in_(cohort))):
                target[row.application_id].append(row)
        rows = []
        for app, job in apps:
            events = _events(app, messages[app.id], stored[app.id], now)
            kinds = {event["kind"] for event in events}
            sent_at = _utc(app.sent_at)
            reply_at = _utc(app.first_reply_at)
            reply_times = [_utc(m.received_at) for m in messages[app.id]
                           if m.direction == "in" and m.received_at is not None
                           and sent_at <= _utc(m.received_at) <= now]
            if reply_at is not None and sent_at <= reply_at <= now:
                reply_times.append(reply_at)
            reply_at = min(reply_times, default=None)
            replied = reply_at is not None or any(
                m.direction == "in" and m.received_at is None for m in messages[app.id])
            track, track_source = _track(app, job)
            proof = _send_proof(app, messages[app.id], logs[app.id], now)
            # A manual Telegram card contains a draft. An owner's "sent" click
            # does not attest that the draft, or its template, was actually used.
            template = app.message_skeleton_id if proof != "owner_reported" else ""
            rows.append({"id": app.id, "application_id": app.id,
                         "title": job.title or job.tag or "", "company": job.company_name or "",
                         "source": (job.source or "?").split(":")[0], "status": app.status,
                         "track": track, "track_source": track_source,
                         "track_inferred": track_source == "inferred",
                         "mature": sent_at <= now - timedelta(days=MATURITY_DAYS),
                         "sent_at": _iso(sent_at), "first_reply_at": _iso(reply_at),
                         "send_proof": proof, "template": template or None,
                         "template_known": bool(template), "replied": replied,
                         "positive": bool(kinds & POSITIVE_KINDS),
                         "response_hours": ((reply_at - sent_at).total_seconds() / 3600
                                            if reply_at is not None else None),
                         "category": _category(kinds, replied),
                         "kinds": [kind for kind in EVENT_KINDS if kind in kinds],
                         "events": events})
    return rows


def _summary(rows) -> dict:
    counts = {kind: sum(kind in row["kinds"] for row in rows) for kind in EVENT_KINDS}
    counts.update(sent=len(rows), replied=sum(row["replied"] for row in rows),
                  positive=sum(row["positive"] for row in rows),
                  owner_reported_sent=sum(row["send_proof"] == "owner_reported" for row in rows),
                  transport_confirmed_sent=sum(row["send_proof"] == "transport_confirmed"
                                               for row in rows),
                  unknown_sent_proof=sum(row["send_proof"] == "unknown" for row in rows))
    categories = {key: sum(row["category"] == key for row in rows) for key in CATEGORY_KEYS}
    rates = {key + "_rate": 100.0 * counts[key] / len(rows) if rows else 0.0
             for key in ("replied", "positive", *EVENT_KINDS)}
    rates["reply_rate"] = rates.pop("replied_rate")
    return {"sent": len(rows), "counts": counts, "categories": categories, **rates,
            "milestones": {kind: counts[kind] for kind in EVENT_KINDS},
            "delivery": {"owner_reported": counts["owner_reported_sent"],
                         "transport_confirmed": counts["transport_confirmed_sent"],
                         "unknown": counts["unknown_sent_proof"]},
            "stages": [{"kind": kind, "count": counts[kind], "rate": rates[kind + "_rate"]}
                       for kind in EVENT_KINDS]}


def aggregate(days=90, track="all", offset=0, limit=50) -> dict:
    """One send cohort; exclusive old categories plus non-exclusive milestones.

applications paginates sends (newest first); history contains their evidence
events. by_track contains the entire cohort so track tabs retain their counts.
Dates are UTC ISO strings; unknown event dates are null. Rates are percentages.
"""
    if (not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or limit < 1):
        raise ValueError("offset must be nonnegative and limit must be positive")
    rows = _cohort(days)
    by_track = defaultdict(list)
    for row in rows:
        by_track[row["track"]].append(row)
    selected = rows if track == "all" else by_track.get(track, [])
    page = selected[offset:offset + limit]
    hours = [row["response_hours"] for row in selected if row["response_hours"] is not None]
    return {"days": days, "track": track, **_summary(selected),
            "by_track": {key: _summary(group) for key, group in sorted(by_track.items())},
            "applications": page,
            "history": sorted((event for row in page for event in row["events"]),
                              key=lambda e: (e["occurred_at"] or e["recorded_at"] or "",
                                             e["event_key"]), reverse=True),
            "total": len(selected),
            "offset": offset, "limit": limit, "has_more": offset + limit < len(selected),
            "group_quality": {
                "by_" + group: _comparison_from_rows(
                    [row for row in selected if row["mature"]], group,
                    1, days=days, mature=True)
                for group in ("source", "template")},
            "avg_response_hours": sum(hours) / len(hours) if hours else None,
            "responses_measured": len(hours),
            "note": "Одна когорта по дате отправки. Этапы сохраняются после отказа. "
                    "Ручная отправка — со слов владельца; transport_confirmed — принятие "
                    "транспортом, не прочтение. Исторические данные и вывод о треке отмечены; "
                    "неизвестные даты не восстановлены. Прошедший слот не доказывает интервью."}


def comparison_rows(group, min_sent=1, *, days=90, mature=False) -> list[dict]:
    """Compatible key/sent/replied/rate rows with observed quality counts added."""
    if group not in ("source", "template"):
        raise ValueError("Unknown comparison group")
    return _comparison_from_rows(_cohort(days, mature=mature), group, min_sent,
                                 days=days, mature=mature)


def _comparison_from_rows(rows, group, min_sent, *, days, mature):
    groups = defaultdict(list)
    for row in rows:
        if row[group]:
            groups[row[group]].append(row)
    out = []
    for key, rows in groups.items():
        if len(rows) < min_sent:
            continue
        summary = _summary(rows)
        out.append({"key": key, **summary["counts"], "counts": summary["counts"],
                    "rate": summary["reply_rate"], "reply_rate": summary["reply_rate"],
                    "quality_rate": summary["positive_rate"],
                    "positive_rate": summary["positive_rate"],
                    "preference_eligible": bool(mature and days == 90 and len(rows) >= MIN_OBSERVATIONS),
                    "mature": mature, "days": days})
    return sorted(out, key=lambda row: (-row["quality_rate"], -row["sent"], row["key"]))
