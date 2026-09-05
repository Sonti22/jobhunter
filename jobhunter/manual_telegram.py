"""Owner-operated Telegram outreach: prepare and record, never send to a recruiter.

Only fresh, approved, never-attempted applications are handed over. Ownership
persists in outcome; neither automatic replies nor follow-ups may take it back.
SQLite write transactions serialize issue/mark across the web and bot processes.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import select

from . import notify
from .config import ROOT, get_settings
from .db import session_scope
from .models import Application, Employer, Job, Status, utcnow
from .outreach import eligibility

PREFIX = "manual_tg_"
READY, SENT, SKIPPED, FAILED = (PREFIX + s for s in ("ready", "sent", "skipped", "failed"))
BATCH_SIZE = 5
WARNING = ("Ты отправляешь сам; бот рекрутёру ничего не отправит. "
           "Ограничения Telegram действуют и при ручной отправке. "
           "Если отправка не проходит, не повторяй её подряд; выбери «Не получилось». "
           "«Я отправил» нажимай только после фактической отправки.")


def _handle(job) -> str:
    return (job.contact_handle or "").strip().lstrip("@").lower()


def _packet(app) -> dict:
    return dict((app.apply_packet_json or {}).get("manual_telegram") or {})


def _problem(app, job, employer) -> str:
    verdict = eligibility.check(app, job, employer, manual=True)
    if not verdict.allowed:
        return verdict.reason
    if app.status != Status.APPROVED.value or app.sent_at or app.send_attempts:
        return "Уже была попытка отправки — требуется отдельная проверка доставки"
    if job.contact_kind != "user_handle":
        return "Это не личный Telegram-контакт"
    if not re.fullmatch(r"[a-z][a-z0-9_]{4,31}", _handle(job)) or _handle(job).endswith("bot"):
        return "Контакт не подходит для личного сообщения"
    if app.review_note or not (app.message_body or "").strip():
        return "Текст требует проверки"
    if len(app.message_body) > 2800:
        return "Текст слишком длинный для карточки"
    packet = _packet(app)
    if packet and (packet.get("handle") != _handle(job)
                   or packet.get("text") != app.message_body):
        return "Контакт или текст изменился после выдачи — карточку нужно проверить"
    return ""


def _row(sess, app, job) -> dict:
    employer = sess.get(Employer, app.employer_id) if app.employer_id else None
    packet = _packet(app)
    return dict(id=app.id, title=packet.get("title", job.title),
                company=packet.get("company", job.company_name), score=app.score,
                handle=packet.get("handle", _handle(job)), text=packet.get("text", ""),
                vacancy_url=packet.get("vacancy_url", ""), cv_path=app.cv_path,
                outcome=app.outcome, problem=_problem(app, job, employer))


def listing() -> dict:
    with session_scope() as sess:
        pairs = sess.execute(select(Application, Job).join(Job).where(
            Application.outcome.startswith(PREFIX)).order_by(Application.id.desc())).all()
        counts = Counter(a.outcome for a, _ in pairs)
        rows = [_row(sess, a, j) for a, j in pairs if a.outcome == READY]
    return dict(items=rows, ready=len(rows), sent=counts[SENT],
                skipped=counts[SKIPPED], failed=counts[FAILED])


def keyboard(row: dict) -> dict:
    aid = row["id"]
    rows = []
    if not row["problem"]:
        # Official username deep link opens a draft, never sends it.
        # https://core.telegram.org/api/links#public-username-links
        draft = "https://t.me/" + row["handle"] + "?" + urlencode({"text": row["text"]})
        rows.append([{"text": "Открыть @%s с текстом" % row["handle"], "url": draft}])
        rows.append([{"text": "Текст отдельно", "callback_data": f"t:{aid}:text"},
                     {"text": "Ник отдельно", "callback_data": f"t:{aid}:handle"}])
        rows.append([{"text": "📎 Получить резюме", "callback_data": f"t:{aid}:cv"}])
    if row["vacancy_url"]:
        rows.append([{"text": "Исходная вакансия", "url": row["vacancy_url"]}])
    rows += [[{"text": "✅ Всё отправил → следующий", "callback_data": f"t:{aid}:confirm"}],
             [{"text": "Не подходит", "callback_data": f"t:{aid}:skip"},
              {"text": "Не получилось", "callback_data": f"t:{aid}:failed"}]]
    return {"inline_keyboard": rows}


def card(row: dict) -> str:
    body = (f"🖐 Ручная отправка #{row['id']}\n{row['title'][:160]}\n"
            f"{row['company'][:100]} · соответствие {row['score']:.0f}\n"
            f"Контакт: @{row['handle']}\n\n")
    body += ("⚠️ НЕ ОТПРАВЛЯЙ: " + row["problem"] if row["problem"] else row["text"])
    if row["cv_path"]:
        body += "\n\nРезюме — PDF-файлом здесь в чате; повторно: «📎 Получить резюме»."
    return body + ("\n\n1. Открой чат с текстом и отправь его сам."
                   "\n2. При необходимости приложи PDF."
                   "\n3. Вернись сюда: «✅ Всё отправил → следующий».")


def issue(limit: int = BATCH_SIZE, *, deliver: bool = True, sess=None) -> dict:
    """Reserve at most five items. Repeated requests keep the current batch."""
    limit = min(BATCH_SIZE, max(0, limit))
    own_session = sess is None
    with (session_scope() if own_session else nullcontext(sess)) as sess:
        if own_session:
            sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        active = sess.scalar(select(Application.id).where(Application.outcome == READY).limit(1))
        if active or not limit:
            return {"issued": 0, "reason": "Сначала отметь текущие карточки: /outreach"}
        pairs = sess.execute(select(Application, Job).join(Job).where(
            Application.status == Status.APPROVED.value,
            Job.contact_kind == "user_handle", Application.outcome == "")
            .order_by(Application.score.desc(), Job.posted_at.desc(), Application.id.desc())).all()
        # Compare actual handles, not only employer_id: older rows may have no employer.
        used = {_handle(j) for a, j in sess.execute(select(Application, Job).join(Job).where(
            (Application.sent_at.is_not(None)) | (Application.send_attempts > 0)
            | Application.outcome.startswith(PREFIX))).all() if j.contact_kind == "user_handle"}
        chosen = []
        for app, job in pairs:
            employer = sess.get(Employer, app.employer_id) if app.employer_id else None
            if _handle(job) in used or _problem(app, job, employer):
                continue
            app.outcome = READY
            packet = dict(app.apply_packet_json or {})
            # Telegram post ids come from the parser as channel/message_id.
            post = (job.external_uuid or "").removeprefix("tg:")
            url = "https://t.me/" + post if re.fullmatch(r"[A-Za-z0-9_]+/\d+", post) else ""
            packet["manual_telegram"] = dict(handle=_handle(job), text=app.message_body,
                title=job.title or job.tag, company=job.company_name or "",
                vacancy_url=url, issued_at=utcnow().isoformat())
            app.apply_packet_json = packet
            used.add(_handle(job))
            chosen.append(_row(sess, app, job))
            if len(chosen) >= limit:
                break
        if chosen and deliver:
            notify.push("manual_tg_head", WARNING,
                        dedup="manual_tg_head:%d" % chosen[0]["id"], sess=sess)
            for row in chosen:
                notify.push("manual_tg_item", card(row), markup=keyboard(row),
                            dedup="manual_tg:%d" % row["id"], sess=sess)
        return {"issued": len(chosen), "ids": [r["id"] for r in chosen],
                "reason": "Карточки подготовлены" if chosen else "Нет новых подходящих откликов"}


def get_card(app_id: int) -> dict | None:
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app or app.outcome != READY:
            return None
        return _row(sess, app, sess.get(Job, app.job_id))


def mark(app_id: int, action: str, *, next_chat_id: int | None = None) -> tuple[bool, str]:
    if action not in ("sent", "skip", "failed"):
        return False, "Неизвестная отметка"
    if next_chat_id is not None and next_chat_id not in get_settings().bot_owner_ids:
        return False, "Работай с ручной очередью в личном чате бота"
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        app = sess.get(Application, app_id)
        if not app or app.outcome != READY:
            return False, "Карточка уже обработана или не выдавалась для ручной отправки"
        if action == "sent":
            if app.status != Status.APPROVED.value or app.sent_at or app.send_attempts:
                return False, "Состояние изменилось — сначала проверь доставку и диалог"
            job = sess.get(Job, app.job_id)
            if _packet(app).get("handle") != _handle(job):
                return False, "Контакт изменился — сначала проверь, кому отправлено сообщение"
            now = utcnow()
            if not app.advance(Status.AWAITING_REPLY, reason="Отправку вручную подтвердил владелец"):
                raise RuntimeError("Нет разрешённого перехода для ручной отправки")
            app.outcome = SENT
            app.applied_at = app.sent_at = app.last_outbound_at = now
            app.send_channel = "telegram_manual"
            # No fabricated Telegram message id, Message body or successful SendLog.
            # This is an owner's report, not API-confirmed delivery of the draft.
            employer = sess.get(Employer, app.employer_id) if app.employer_id else None
            if not employer:
                employer = sess.scalar(select(Employer).where(Employer.handle_norm == _handle(job)))
            if not employer:
                employer = Employer(handle_norm=_handle(job), handle_kind="user_handle")
                sess.add(employer)
                sess.flush()
            app.employer_id = employer.id
            employer.last_contacted_at = now
            employer.total_messages_sent = (employer.total_messages_sent or 0) + 1
            note = "Записано с твоих слов: отправлено вручную. Бот повторно не отправит."
        else:
            app.outcome = SKIPPED if action == "skip" else FAILED
            if app.status == Status.APPROVED.value and not app.sent_at and not app.send_attempts:
                app.transition(Status.WITHDRAWN, reason="Ручная отправка: " + action)
            note = "Не подходит — убрано" if action == "skip" else "Не получилось — повторять автоматически не будем"
        # Mark + enqueue the next card commit together. A crash between the
        # callback and network delivery must not lose the next step.
        if next_chat_id is not None and action in ("sent", "skip"):
            _queue_current(sess, next_chat_id)
        elif action == "failed":
            note += ". Выдача приостановлена; когда будешь готов, нажми /outreach."
        return True, note


def _queue_current(sess, chat_id: int, request_key: str = "") -> int | None:
    issue(limit=1, deliver=False, sess=sess)
    app = sess.scalar(select(Application).where(Application.outcome == READY)
                      .order_by(Application.score.desc(), Application.id.desc()).limit(1))
    if not app:
        notify.push("manual_tg_empty", "Подходящие новые отклики закончились. "
                    "Позже нажми /outreach — перепроверю очередь.", chat_id=chat_id,
                    dedup=f"manual_tg_empty:{chat_id}:{utcnow().date()}", sess=sess)
        return None
    # A distinct /outreach request can show the current item again. The
    # automatic continuation has a stable key, so double taps never multiply it.
    suffix = (":" + hashlib.sha256(request_key.encode()).hexdigest()[:16]) if request_key else ""
    notify.push("manual_tg_step", f"Ручной отклик #{app.id}", chat_id=chat_id,
                markup={"app_id": app.id}, dedup=f"manual_tg_step:{chat_id}:{app.id}{suffix}", sess=sess)
    notify.push("manual_tg_document", f"Резюме для отклика #{app.id}", chat_id=chat_id,
                markup={"app_id": app.id}, dedup=f"manual_tg_document:{chat_id}:{app.id}{suffix}", sess=sess)
    return app.id


def queue_current(chat_id: int, request_key: str = "") -> int | None:
    """Start/resume one-at-a-time delivery to the owner, using the durable outbox."""
    if chat_id not in get_settings().bot_owner_ids:
        raise PermissionError("ручная очередь доступна только в личном чате владельца")
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        return _queue_current(sess, chat_id, request_key)


def queue_cv(chat_id: int, app_id: int, request_key: str) -> None:
    if chat_id not in get_settings().bot_owner_ids:
        raise PermissionError("резюме доступно только владельцу")
    suffix = hashlib.sha256(request_key.encode()).hexdigest()[:16]
    notify.push("manual_tg_document", f"Резюме для отклика #{app_id}", chat_id=chat_id,
                markup={"app_id": app_id}, dedup=f"manual_tg_cv:{chat_id}:{app_id}:{suffix}")


def cv_document(app_id: int) -> tuple[str, bytes]:
    """Resolve only a PDF from configured CV locations; never arbitrary DB paths."""
    row = get_card(app_id)
    if not row or row["problem"]:
        raise ValueError("карточка устарела или требует проверки")
    s = get_settings()
    requested = s.base_cv_path or row["cv_path"]
    if not requested:
        raise ValueError("резюме пока не подготовлено")
    path = Path(requested).resolve()
    allowed = [Path(s.cv_out).resolve(), (ROOT / "cv_base").resolve()]
    # Only the exact explicitly configured base PDF, not every sibling file.
    is_base = bool(s.base_cv_path and path == Path(s.base_cv_path).resolve())
    if not is_base and not any(path.is_relative_to(root) for root in allowed):
        raise ValueError("резюме находится вне разрешённых папок")
    if path.suffix.lower() != ".pdf" or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("PDF-резюме недоступно или слишком большое")
    with path.open("rb") as stream:
        content = stream.read(10 * 1024 * 1024 + 1)
    if not content.startswith(b"%PDF-") or len(content) > 10 * 1024 * 1024:
        raise ValueError("файл не является допустимым PDF")
    return path.name, content
