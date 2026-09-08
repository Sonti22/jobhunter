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
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy import select

from . import notify
from .config import ROOT, get_settings
from .db import session_scope
from .models import Application, Employer, HandleCache, Job, OwnerPreference, Status, utcnow
from .outreach import eligibility

PREFIX = "manual_tg_"
READY, SENT, SKIPPED, FAILED = (PREFIX + s for s in ("ready", "sent", "skipped", "failed"))
BATCH_SIZE = 5
TRACKS = {"all": "Лучшие из трёх", "backend": "Backend", "ml": "AI / ML",
          "architect": "Tech Lead / Архитектор", "additional": "Дополнительные"}
WARNING = ("Отправляешь ты сам, бот рекрутёру ничего не шлёт. "
           "Карточка = PDF: кнопкой открой чат с готовым письмом → отправь → "
           "перешли этот PDF → нажми «✅ Отправил». Ошибся — «↩️ Вернуть» "
           "работает %d минут." % 15)
UNDO_MINUTES = 15


def _handle(job) -> str:
    return (job.contact_handle or "").strip().lstrip("@").lower()


def _packet(app) -> dict:
    return dict((app.apply_packet_json or {}).get("manual_telegram") or {})


def _owner_id(chat_id=None) -> int:
    return chat_id if chat_id is not None else next(iter(sorted(get_settings().bot_owner_ids)), 0)


def _selected_track(sess, chat_id=None) -> str:
    pref = sess.get(OwnerPreference, _owner_id(chat_id))
    return pref.outreach_track if pref and pref.outreach_track in TRACKS else "all"


def selected_track(chat_id=None) -> str:
    with session_scope() as sess:
        return _selected_track(sess, chat_id)


def set_track(chat_id: int, track: str) -> None:
    if chat_id not in get_settings().bot_owner_ids:
        raise PermissionError("Подборка доступна только владельцу")
    if track not in TRACKS:
        raise ValueError("Неизвестное направление")
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        pref = sess.get(OwnerPreference, chat_id)
        if pref is None:
            pref = OwnerPreference(owner_id=chat_id)
            sess.add(pref)
        pref.outreach_track = track


def _in_track(assessment: dict, track: str) -> bool:
    family = assessment.get("track", "unknown")
    if track == "all":
        return family in ("backend", "ml", "architect")
    if track == "additional":
        return family not in ("backend", "ml", "architect")
    return family == track


GROUP_MARKERS = re.compile(r"(subscriber|подписчик|member|участник)", re.I)
_SUBS_RE = re.compile(r'tgme_page_extra">([^<]*)</div>')


def _cached_kind(sess, handle: str) -> str:
    """Что резолвер уже знает о хендле: user | not_a_user | dead | unknown."""
    row = sess.get(HandleCache, handle)
    if row is None:
        return "unknown"
    if row.last_error in ("not_a_user", "dead"):
        return row.last_error
    return "user" if row.user_id else "unknown"


def public_handle_kind(handle: str, http=None) -> str:
    """Тип аккаунта по ОТКРЫТОЙ странице t.me: user | group | unknown.

    Автоотправка узнаёт группу от резолвера (NotAUser «канал/группа»), но
    ручная выдача в MTProto не ходит — и в карточки владельцу попали
    @xyflow (канал) и @it_kz_chat (групповой чат). Отклик, отправленный
    в общий чат, — спам на глазах у сотни человек. Публичная страница
    отличает их без сессии: у групп и каналов есть счётчик участников.
    """
    import httpx

    own = http is None
    http = http or httpx.Client(trust_env=False, follow_redirects=True)
    try:
        r = http.get("https://t.me/" + handle, timeout=10,
                     headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return "unknown"
        m = _SUBS_RE.search(r.text)
        return "group" if (m and GROUP_MARKERS.search(m.group(1))) else "user"
    except Exception:                      # noqa: BLE001 — сеть не обязана быть
        return "unknown"
    finally:
        if own:
            http.close()


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
    # Кеш резолвера читаем без сети: он уже знает ботов, каналы и группы.
    from .db import session_scope as _scope
    with _scope() as _s:
        if _cached_kind(_s, _handle(job)) in ("not_a_user", "dead"):
            return "Это групповой чат, канал или мёртвый контакт — не личное сообщение"
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
    from .match.explain import assessment_for
    employer = sess.get(Employer, app.employer_id) if app.employer_id else None
    packet = _packet(app)
    assessment = assessment_for(app, job)
    return dict(id=app.id, title=packet.get("title", job.title),
                company=packet.get("company", job.company_name), score=app.score,
                handle=packet.get("handle", _handle(job)), text=packet.get("text", ""),
                vacancy_url=packet.get("vacancy_url", ""), cv_path=app.cv_path,
                outcome=app.outcome, problem=_problem(app, job, employer),
                track=assessment.get("track", "unknown"), assessment=assessment)


def listing(track: str = "all") -> dict:
    if track not in TRACKS:
        raise ValueError("Неизвестное направление")
    with session_scope() as sess:
        pairs = sess.execute(select(Application, Job).join(Job).where(
            Application.outcome.startswith(PREFIX)).order_by(Application.id.desc())).all()
        counts = Counter(a.outcome for a, _ in pairs)
        rows = [_row(sess, a, j) for a, j in pairs if a.outcome == READY]
        for row in rows:
            row["matches_filter"] = _in_track(row["assessment"], track)
    return dict(items=rows, ready=len(rows), sent=counts[SENT],
                skipped=counts[SKIPPED], failed=counts[FAILED], track=track,
                tracks=TRACKS, note="Выданные карточки сохраняются при смене фильтра.")


def keyboard(row: dict) -> dict:
    """Три ряда вместо шести: открыть чат / отправил-пропустить / детали.

    Раньше на карточке было девять кнопок и второй диалог «точно отправил?»;
    владелец выдал четыре карточки и бросил. «Ник отдельно», «Получить
    резюме», «Направления», «Не получилось» ушли: ник есть в ссылке, PDF —
    само сообщение, отказ от отправки — «Пропустить».
    """
    aid = row["id"]
    rows = []
    if not row["problem"]:
        # Official username deep link opens a draft, never sends it.
        # https://core.telegram.org/api/links#public-username-links
        draft = "https://t.me/" + row["handle"] + "?" + urlencode({"text": row["text"]})
        rows.append([{"text": "📨 Открыть чат @%s с письмом" % row["handle"], "url": draft}])
        rows.append([{"text": "✅ Отправил", "callback_data": f"t:{aid}:sent"},
                     {"text": "⏭ Пропустить", "callback_data": f"t:{aid}:skip"}])
        rows.append([{"text": "📝 Текст письма", "callback_data": f"t:{aid}:text"},
                     {"text": "ℹ️ Почему подходит", "callback_data": f"t:{aid}:why"}])
    else:
        rows.append([{"text": "⏭ Пропустить", "callback_data": f"t:{aid}:skip"},
                     {"text": "ℹ️ Почему подходит", "callback_data": f"t:{aid}:why"}])
    if row["vacancy_url"]:
        rows.append([{"text": "Исходная вакансия", "url": row["vacancy_url"]}])
    return {"inline_keyboard": rows}


def after_mark_keyboard(aid: int, undo: bool) -> dict:
    rows = []
    if undo:
        rows.append([{"text": "↩️ Вернуть", "callback_data": f"t:{aid}:undo"}])
    else:
        rows.append([{"text": "Почему не подошло? (необязательно)",
                      "callback_data": f"w:feedback:{aid}"}])
    rows.append([{"text": "➡️ Следующий", "callback_data": "t:0:next"}])
    return {"inline_keyboard": rows}


def card(row: dict) -> str:
    """Подпись к PDF: умещается в лимит caption (1024), без инструкций на
    каждой карточке — они один раз в /outreach."""
    body = (f"🖐 #{row['id']} · {row['title'][:120]}\n"
            f"{row['company'][:80]} · соответствие {row['score']:.0f}\n"
            f"@{row['handle']}")
    if row["problem"]:
        return (body + "\n\n⚠️ НЕ ОТПРАВЛЯЙ: " + row["problem"][:300])[:1000]
    tail = "\n\n📨 → ✉️ → 📎 переслать этот PDF → ✅"
    # Письмо — прямо в подписи, пока влезает в лимит caption: владелец
    # видит, что уйдёт, без лишнего тапа. Длинное — по кнопке «📝 Текст».
    letter = (row.get("text") or "").strip()
    if letter and len(body) + len(letter) + len(tail) + 2 <= 1000:
        return body + "\n\n" + letter + tail
    return (body + "\n\n📝 письмо — кнопкой ниже" + tail)[:1000]


def issue(limit: int = BATCH_SIZE, *, deliver: bool = True, sess=None,
          track: str | None = None) -> dict:
    """Reserve at most five items. Repeated requests keep the current batch."""
    limit = min(BATCH_SIZE, max(0, limit))
    own_session = sess is None
    with (session_scope() if own_session else nullcontext(sess)) as sess:
        if own_session:
            sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        active = sess.scalar(select(Application.id).where(Application.outcome == READY).limit(1))
        if active or not limit:
            return {"issued": 0, "reason": "Сначала отметь текущие карточки: /outreach"}
        from .match.explain import assessment_for
        track = _selected_track(sess) if track is None else track
        if track not in TRACKS:
            raise ValueError("Неизвестное направление")
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
            if not _in_track(assessment_for(app, job), track):
                continue
            employer = sess.get(Employer, app.employer_id) if app.employer_id else None
            if _handle(job) in used or _problem(app, job, employer):
                continue
            # Незнакомый хендл проверяем по открытой странице t.me: выдача
            # идёт по расписанию и по кнопке владельца, задержка допустима,
            # а карточка «напиши в групповой чат» — нет. Результат кладём в
            # общий кеш, чтобы и автоотправка не тратила на него резолв.
            if _cached_kind(sess, _handle(job)) == "unknown":
                kind = public_handle_kind(_handle(job))
                if kind == "group":
                    row = sess.get(HandleCache, _handle(job)) or HandleCache(
                        handle_norm=_handle(job))
                    row.last_error = "not_a_user"
                    row.resolved_at = utcnow()
                    sess.merge(row)
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


def explain_card(app_id: int) -> str:
    from .match.explain import assessment_for
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app:
            return "Заявка не найдена"
        job = sess.get(Job, app.job_id)
        data = assessment_for(app, job)
        lines = [f"Почему подходит #{app_id} · {job.title[:160]}",
                 "Направление: " + TRACKS.get(str(data.get("track") or "unknown"), "Не определено"),
                 f"Версия правил: {data.get('version', 'неизвестно')}",
                 "Оценка соответствия — не вероятность оффера."]
        for item in data.get("matched", [])[:8]:
            evidence = ", ".join(item.get("evidence_ids", [])) or "нет подтверждающей записи"
            lines.append(f"✓ {item.get('skill', '?')}: {evidence}")
        for label, key in (("Обязательно", "required"), ("Желательно", "desired"),
                           ("Пробелы", "gaps"), ("Неизвестно", "unknowns"),
                           ("Нужна проверка", "review_reasons")):
            values = data.get(key) or []
            if values:
                lines.append(label + ": " + "; ".join(str(x) for x in values)[:650])
        if data.get("vacancy_url"):
            lines.append("Вакансия: " + data["vacancy_url"])
        return "\n".join(lines)[:3900]


def get_card(app_id: int) -> dict | None:
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app or app.outcome != READY:
            return None
        return _row(sess, app, sess.get(Job, app.job_id))


def mark(app_id: int, action: str, *, next_chat_id: int | None = None,
         reason: str = "unspecified") -> tuple[bool, str]:
    if action not in ("sent", "skip", "failed"):
        return False, "Неизвестная отметка"
    if next_chat_id is not None and next_chat_id not in get_settings().bot_owner_ids:
        return False, "Работай с ручной очередью в личном чате бота"
    from . import feedback
    if action == "skip" and reason not in feedback.REASONS:
        return False, "Неизвестная причина пропуска"
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
            packet = _packet(app)
            packet["previous_employer_contacted_at"] = (
                employer.last_contacted_at.isoformat() if employer.last_contacted_at else None)
            app.apply_packet_json = dict(app.apply_packet_json or {}, manual_telegram=packet)
            employer.last_contacted_at = now
            employer.total_messages_sent = (employer.total_messages_sent or 0) + 1
            note = "Записано с твоих слов: отправлено вручную. Бот повторно не отправит."
        else:
            if action == "skip":
                feedback.record(sess, app_id, reason, _owner_id(next_chat_id))
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


def unmark(app_id: int, chat_id: int | None = None) -> tuple[bool, str]:
    """Откатить «✅ Отправил», нажатое по ошибке, — в течение UNDO_MINUTES.

    Замена второму диалогу «точно отправил?»: один тап вместо двух, а
    ошибка исправляется одним тапом обратно. Откат невозможен, если
    рекрутёр уже ответил — тогда отправка была настоящей.
    """
    if chat_id is not None and chat_id not in get_settings().bot_owner_ids:
        return False, "Работай с ручной очередью в личном чате бота"
    with session_scope() as sess:
        sess.connection().exec_driver_sql("BEGIN IMMEDIATE")
        app = sess.get(Application, app_id)
        if not app or app.outcome != SENT or app.send_channel != "telegram_manual":
            return False, "Эту отметку вернуть нельзя"
        from .models import Message, ResultEvent, SendLog
        if app.status != Status.AWAITING_REPLY.value or any(
                sess.scalar(select(model.id).where(model.application_id == app_id).limit(1))
                for model in (Message, ResultEvent, SendLog)):
            return False, "Есть история переписки или результата — отмена отправки недоступна"
        if app.first_reply_at or app.last_inbound_at:
            return False, "Рекрутёр уже ответил — отправка была настоящей"
        sent_at = app.sent_at.replace(tzinfo=None) if app.sent_at else None
        now = utcnow().replace(tzinfo=None)
        if not sent_at or (now - sent_at).total_seconds() > UNDO_MINUTES * 60:
            return False, "Прошло больше %d минут — отметка закреплена" % UNDO_MINUTES
        # Граф не ведёт из AWAITING_REPLY назад в APPROVED: это откат
        # отметки владельца, а не переход конвейера — пишем напрямую.
        app.status = Status.APPROVED.value
        app.outcome = READY
        app.sent_at = app.applied_at = app.last_outbound_at = None
        app.send_channel = ""
        employer = sess.get(Employer, app.employer_id) if app.employer_id else None
        if employer:
            employer.total_messages_sent = max(0, (employer.total_messages_sent or 0) - 1)
            packet = _packet(app)
            if (employer.last_contacted_at == sent_at and
                    "previous_employer_contacted_at" in packet):
                previous = packet["previous_employer_contacted_at"]
                employer.last_contacted_at = datetime.fromisoformat(previous) if previous else None
        return True, "Отметка снята — карточка снова в работе"


def _queue_current(sess, chat_id: int, request_key: str = "") -> int | None:
    issue(limit=1, deliver=False, sess=sess, track=_selected_track(sess, chat_id))
    app = sess.scalar(select(Application).join(Job).where(Application.outcome == READY)
                      .order_by(Application.score.desc(), Job.posted_at.desc(),
                                Application.id.desc()).limit(1))
    if not app:
        notify.push("manual_tg_empty", "Подходящие новые отклики закончились. "
                    "Позже нажми /outreach — перепроверю очередь.", chat_id=chat_id,
                    dedup=f"manual_tg_empty:{chat_id}:{utcnow().date()}", sess=sess)
        return None
    # A distinct /outreach request can show the current item again. The
    # automatic continuation has a stable key, so double taps never multiply it.
    suffix = (":" + hashlib.sha256(request_key.encode()).hexdigest()[:16]) if request_key else ""
    # Одно сообщение на отклик: outbox доставит карточку как PDF с подписью
    # и кнопками. Отдельной строки «документ» больше нет — она удваивала
    # каждый отклик в чате.
    notify.push("manual_tg_step", f"Ручной отклик #{app.id}", chat_id=chat_id,
                markup={"app_id": app.id}, dedup=f"manual_tg_step:{chat_id}:{app.id}{suffix}", sess=sess)
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
