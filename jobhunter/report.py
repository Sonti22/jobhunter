"""Агрегаты по кампании: один источник цифр для бота и дашборда.

До этого воронку считал web/server.py, а сводку — owner.status_text(). Пока
поверхность была одна, дублирование ничего не стоило; с ботом оно означало
бы, что цифры на экране телефона и в браузере однажды разойдутся, и никто не
поймёт, какая правда.

Все функции читают короткой транзакцией и отдают обычные словари: держать
ORM-объекты между запросами нельзя — с несколькими процессами данные
устаревают, а долгий читатель мешает контрольным точкам WAL.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, func, select

from .config import get_settings
from .db import session_scope
from .models import (
    Application,
    ChannelCandidate,
    DailyQuota,
    Job,
    Message,
    OwnerRequest,
    Status,
    TelegramChannelStat,
)
from .outreach import policy

# Порядок воронки: как заявка движется от находки до оффера.
FUNNEL_ORDER = [
    (Status.PENDING_APPROVAL, "в очереди"),
    (Status.APPROVED, "одобрено"),
    (Status.SENT, "отправлено"),
    (Status.AWAITING_REPLY, "ждут ответа"),
    (Status.FOLLOWED_UP, "напомнили"),
    (Status.REPLIED, "ответили"),
    (Status.IN_DIALOGUE, "в диалоге"),
    (Status.NEEDS_HUMAN, "нужен ты"),
    (Status.INTERVIEW_PROPOSED, "предложено время"),
    (Status.INTERVIEW_CONFIRMED, "интервью"),
    (Status.INTERVIEW_DONE, "интервью прошло"),
    (Status.OFFER, "оффер"),
    (Status.REJECTED_BY_EMPLOYER, "отказы"),
]


def funnel() -> dict:
    """Воронка по статусам плюс доля ответов."""
    with session_scope() as sess:
        rows = sess.execute(
            select(Application.status, func.count(Application.id))
            .group_by(Application.status)).all()
        counts = {k: v for k, v in rows}
        sent = sess.scalar(select(func.count(Application.id))
                           .where(Application.sent_at.is_not(None))) or 0
        replied = sess.scalar(select(func.count(Application.id))
                              .where(Application.first_reply_at.is_not(None))) or 0
        open_cards = sess.scalar(select(func.count(OwnerRequest.id))
                                 .where(OwnerRequest.decision == "")) or 0
    return {"counts": counts, "sent": sent, "replied": replied,
            "reply_rate": (100.0 * replied / sent) if sent else 0.0,
            "open_cards": open_cards,
            "stages": [(label, counts.get(st.value, 0))
                       for st, label in FUNNEL_ORDER]}


def quota() -> dict:
    """Дневная квота и предохранители."""
    with session_scope() as sess:
        q = policy.get_quota(sess)
        st = policy.get_state(sess)
        lk = policy.get_lock(sess)
        verdict = policy.can_send_cold(sess)
        # Почта в q.sent_count не входит (квота с разгоном — только про
        # Telegram-аккаунт), поэтому считаем её отдельно: экран «отправлено
        # 0/30» при двух ушедших письмах читается как «система стоит».
        email_sent = policy.email_sent_today(sess)
        return {"sent": q.sent_count,
                "cap": min(q.planned_cap or st.quota_ceiling, st.quota_ceiling),
                "email_sent": email_sent,
                "email_cap": get_settings().email_daily_limit,
                "clean_days": st.consecutive_clean_days,
                "peerflood_total": st.peerflood_total,
                "manual_only": st.manual_only,
                "locked_until": lk.locked_until,
                "lock_reason": lk.reason,
                "can_send": verdict.allowed, "verdict": verdict.reason,
                "kill_switch": policy.kill_switch_active()}


def queue_top(n: int = 10) -> list:
    """Что готово к отправке — по убыванию соответствия.

    Только с пройденным гейтом: заявка без gate_passed не может быть
    одобрена (инвариант transition), и показывать её в «готово к отправке»
    значит рисовать очередь, которую кнопка одобрения молча пропустит.
    """
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status.in_((Status.PENDING_APPROVAL.value,
                                          Status.FOLLOWUP_PENDING_APPROVAL.value)),
                   Application.gate_passed.is_(True))
            .order_by(Application.score.desc())
            .limit(n)).all()
        out = []
        for a in rows:
            job = sess.get(Job, a.job_id)
            out.append({"id": a.id, "score": a.score,
                        "is_followup": a.status
                        == Status.FOLLOWUP_PENDING_APPROVAL.value,
                        "title": (job.title or job.tag or "")[:60],
                        "company": job.company_name or "",
                        "contact": job.contact_handle or job.contact_url or "",
                        "kind": job.contact_kind,
                        "source": (job.source or "").split(":")[0]})
        return out


def upcoming_interviews(n: int = 10) -> list:
    """Ближайшие подтверждённые интервью."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.INTERVIEW_CONFIRMED.value,
                   Application.interview_at_utc.is_not(None))
            .order_by(Application.interview_at_utc)
            .limit(n)).all()
        out = []
        for a in rows:
            job = sess.get(Job, a.job_id)
            out.append({"id": a.id,
                        "at_utc": a.interview_at_utc.replace(tzinfo=timezone.utc)
                        if a.interview_at_utc else None,
                        "tz": a.interview_tz or get_settings().owner_tz,
                        "title": (job.title or job.tag or "")[:60],
                        "company": job.company_name or "",
                        "contact": job.contact_handle or "",
                        "link": a.gcal_link or ""})
        return out


def open_cards(n: int = 10) -> list:
    """Карточки, ждущие решения владельца."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(OwnerRequest)
            .where(OwnerRequest.decision == "")
            .order_by(OwnerRequest.id.desc())
            .limit(n)).all()
        return [{"id": r.id, "kind": r.kind, "application_id": r.application_id,
                 "question": r.question, "created_at": r.created_at,
                 "expires_at": r.expires_at,
                 "payload": dict(r.payload_json or {})} for r in rows]


def sources(days: int = 30) -> dict:
    """Откуда пришли заявки, по которым что-то делали."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    with session_scope() as sess:
        rows = sess.execute(
            select(Job.source, func.count(Application.id))
            .join(Application, Application.job_id == Job.id)
            .where(Application.created_at >= since)
            .group_by(Job.source)).all()
    agg: Counter = Counter()
    for src, cnt in rows:
        agg[(src or "?").split(":")[0]] += cnt
    return dict(agg.most_common())


def intents_daily(days: int = 7) -> dict:
    """Решения классификатора по дням + счётчик поправок LLM.

    {"days": {дата: {метка: сколько}}, "llm_corrections": N}.
    llm_corrections — входящие, где второе мнение LLM разошлось с регэксом.
    Это метрика качества паттернов: растёт — значит, есть что чинить в
    classify.py; сами решения уже приняты с учётом обоих ярусов.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    days_map: dict = {}
    corrections = 0
    with session_scope() as sess:
        rows = sess.scalars(
            select(Message)
            .where(Message.direction == "in",
                   Message.received_at.is_not(None),
                   Message.received_at >= since)).all()
        for m in rows:
            day = m.received_at.strftime("%d.%m")
            label = m.classifier_label or "?"
            days_map.setdefault(day, Counter())[label] += 1
            if m.llm_label and m.llm_label != m.classifier_label:
                corrections += 1
    return {"days": {d: dict(c) for d, c in sorted(days_map.items())},
            "llm_corrections": corrections}


def channels_summary() -> dict:
    """Автопоиск каналов: сколько проверено, годных, в работе."""
    with session_scope() as sess:
        total = sess.scalar(select(func.count(ChannelCandidate.id))) or 0
        passed = sess.scalar(select(func.count(ChannelCandidate.id))
                             .where(ChannelCandidate.passed.is_(True))) or 0
        enabled = sess.scalar(select(func.count(ChannelCandidate.id))
                              .where(ChannelCandidate.enabled.is_(True))) or 0
        top = sess.scalars(
            select(ChannelCandidate)
            .where(ChannelCandidate.enabled.is_(True))
            .order_by(ChannelCandidate.posts_with_contact.desc())
            .limit(8)).all()
        return {"checked": total, "passed": passed, "enabled": enabled,
                "top": [{"username": c.username, "subs": c.subscribers,
                         "contacts": c.posts_with_contact,
                         "fresh7": c.fresh_7d} for c in top]}


def telegram_health() -> dict:
    """Покрытие и свежесть последнего чтения Telegram-каналов."""
    from .ingest.tgchannels import CHANNELS, _discovered, _verified_channels

    active = {c[0].lower() for c in CHANNELS}
    active.update(c.lower() for c in _verified_channels())
    active.update(c.lower() for c in _discovered())
    since = (datetime.now(timezone.utc) - timedelta(hours=36)).replace(tzinfo=None)
    with session_scope() as sess:
        rows = sess.scalars(select(TelegramChannelStat).where(
            TelegramChannelStat.username.in_(active))).all() if active else []
    by_name = {row.username.lower(): row for row in rows}
    failed = [row for row in rows if row.last_status in ("error", "partial")]
    fresh = [row for row in rows if row.last_success_at and row.last_success_at >= since]
    stale = [name for name in active
             if name not in by_name or not by_name[name].last_success_at
             or by_name[name].last_success_at < since]
    return {
        "active_channels": len(active),
        "reported_channels": len(rows),
        "fresh_channels": len(fresh),
        "stale_channels": len(stale),
        "failed_channels": len(failed),
        "unreported_channels": len(active) - len(rows),
        "last_run_at": max((row.last_finished_at for row in rows), default=None),
        "errors": [{"username": row.username, "status": row.last_status,
                    "error": row.last_error} for row in failed[:20]],
        "stale": sorted(stale)[:30],
    }


def messages_today() -> dict:
    """Переписка за сегодня: входящие, автоответы, ответы владельца."""
    start = (datetime.now(timezone.utc)
             .replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None))
    with session_scope() as sess:
        incoming = sess.scalar(
            select(func.count(Message.id))
            .where(Message.direction == "in", Message.received_at >= start)) or 0
        auto = sess.scalar(
            select(func.count(Message.id))
            .where(Message.direction == "out", Message.sent_at >= start,
                   Message.is_auto.is_(True))) or 0
        manual = sess.scalar(
            select(func.count(Message.id))
            .where(Message.direction == "out", Message.sent_at >= start,
                   Message.is_auto.is_(False))) or 0
    return {"incoming": incoming, "auto": auto, "manual": manual}


def by_template(min_sent: int = 1) -> list:
    """Ответы и явные результаты по шаблонам одной 90-дневной когорты.

    Старые key/sent/replied/rate сохранены; quality_rate считает только
    положительные результаты. Неизвестный текст ручной отправки исключён.
    """
    from .results import comparison_rows
    return comparison_rows("template", min_sent)


def by_source(min_sent: int = 1) -> list:
    """Совместимая доля ответов и отдельная доля положительных результатов."""
    from .results import comparison_rows
    return comparison_rows("source", min_sent)


def template_preferences(min_sent: int = 20) -> dict:
    """Качество после 14 дней наблюдения, минимум 20 отправок на шаблон."""
    from .results import MIN_OBSERVATIONS, comparison_rows
    return {row["key"]: row["quality_rate"] / 100.0
            for row in comparison_rows("template", max(MIN_OBSERVATIONS, min_sent), mature=True)}


def source_preferences(min_sent: int = 20) -> dict:
    """Явные положительные результаты зрелых групп, минимум 20 отправок."""
    from .results import MIN_OBSERVATIONS, comparison_rows
    return {row["key"]: row["quality_rate"] / 100.0
            for row in comparison_rows("source", max(MIN_OBSERVATIONS, min_sent), mature=True)}


def source_priority_penalty(source: str, preferences: dict | None = None) -> float:
    """Небольшой штраф источнику с измеренной слабой конверсией."""
    rate = (preferences or {}).get((source or "?").split(":")[0])
    if rate == 0.0:
        return 8.0
    if rate is not None and rate < 0.10:
        return 4.0
    return 0.0


def source_health() -> list:
    """Состояние источников: последний seen, объём и закрытые записи."""
    with session_scope() as sess:
        rows = sess.execute(
            select(Job.source, func.count(Job.id), func.max(Job.last_seen_at),
                   func.sum(Job.is_closed.cast(Integer)))
            .group_by(Job.source).order_by(func.count(Job.id).desc())).all()
    return [{"source": source or "?", "jobs": int(count or 0),
             "last_seen_at": last_seen, "closed": int(closed or 0)}
            for source, count, last_seen, closed in rows]


def totals() -> dict:
    """Общие числа для шапки дашборда — без выгрузки таблиц в память."""
    with session_scope() as sess:
        return {
            "applications": sess.scalar(select(func.count(Application.id))) or 0,
            "jobs": sess.scalar(select(func.count(Job.id))) or 0,
            "sent": sess.scalar(select(func.count(Application.id))
                                .where(Application.sent_at.is_not(None))) or 0,
            "replied": sess.scalar(select(func.count(Application.id))
                                   .where(Application.first_reply_at.is_not(None))) or 0,
        }


def conversion_metrics(days: int = 90) -> dict:
    """Ответы, интервью и офферы той же когорты, что знаменатель отправок."""
    from .results import aggregate
    data = aggregate(days=days)
    counts = data["counts"]
    return {
        "days": days, "sent": counts["sent"], "replied": counts["replied"],
        "interviews": counts["interview_scheduled"], "offers": counts["offer"],
        "reply_rate": data["reply_rate"], "positive": counts["positive"],
        "positive_rate": data["positive_rate"],
        "interview_rate": data["interview_scheduled_rate"],
        "offer_rate": data["offer_rate"], "counts": counts,
        "avg_response_hours": data["avg_response_hours"],
        "responses_measured": data["responses_measured"],
    }


def daily_series(days: int = 7) -> list:
    """Отправки по дням — ряд для текстового графика."""
    with session_scope() as sess:
        rows = sess.scalars(
            select(DailyQuota).order_by(DailyQuota.date.desc()).limit(days)).all()
        return [{"date": r.date, "sent": r.sent_count,
                 "cap": r.planned_cap, "clean": r.clean_day} for r in rows][::-1]
