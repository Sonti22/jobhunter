"""Согласование с владельцем через «Избранное» Telegram.

Требование простое: ничего необратимого без человека. Встреча не назначается,
нешаблонный ответ не уходит, оффер не подтверждается — пока владелец не
ответит командой.

Почему «Избранное», а не бот от BotFather: не нужен второй токен и второй
процесс, переписка не видна никому, и работает тот же клиент Telethon, что
уже авторизован для откликов. Карточка уходит в «Избранное», владелец
отвечает там же обычным сообщением.

Команды (пишутся в «Избранное»):

    /ok 123            подтвердить вариант 1 из карточки
    /ok 123 2          подтвердить вариант 2
    /time 123 29.08 16:00     назначить своё время
    /no 123            отказаться от предложенного времени
    /send 123          отправить черновик ответа как есть
    /say 123 текст     отправить свой текст
    /skip 123          закрыть карточку, ничего не делая
    /status            сводка по кампании
    /stop  /go         стоп-кран отправки: включить / выключить
    /help              напоминание списка команд
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import get_settings
from .convo.slots import fmt
from .db import session_scope
from .models import (
    Application,
    CampaignState,
    Job,
    Message,
    OwnerRequest,
    OwnerRequestKind,
    Status,
    utcnow,
)

OWNER_PEER = "me"

HELP = (
    "Команды jobhunter:\n"
    "/ok N [k]   подтвердить слот (k — номер варианта, по умолчанию 1)\n"
    "/time N 29.08 16:00   назначить своё время\n"
    "/no N       отказаться от предложенного времени\n"
    "/send N     отправить черновик ответа\n"
    "/say N текст  отправить свой текст\n"
    "/skip N     закрыть карточку без действий\n"
    "/close N    закрыть заявку: отказ подтверждён\n"
    "/status     сводка\n"
    "/stop /go   стоп-кран отправки\n"
)


# ────────────────────────────────────────────────── создание карточек ──

def _title(job: Job) -> str:
    t = (job.title or job.tag or "вакансия").strip()
    if job.company_name:
        t = "%s — %s" % (t, job.company_name)
    return t[:80]


def create_slot_request(sess, app: Application, job: Job, slots: list,
                        incoming: str = "", draft_text: str = "") -> OwnerRequest:
    """Карточка «рекрутёр предложил время».

    draft_text — заготовка подтверждения от LLM: /ok не должен быть
    одобрением вслепую, но отправляет текст только человек.
    """
    s = get_settings()
    payload = {"slots": [x.to_json() for x in slots],
               "handle": job.contact_handle, "incoming": (incoming or "")[:600],
               "draft": (draft_text or "")[:800]}
    lines = ["🗓 #%d · %s" % (app.id, _title(job)),
             "Рекрутёр @%s предлагает время:" % (job.contact_handle or "?")]
    for i, sl in enumerate(slots, 1):
        mark = "" if sl.has_time else "  ← время не названо, задай через /time"
        lines.append("  %d) %s%s" % (i, fmt(sl.dt_utc, s.owner_tz), mark))
    if incoming:
        lines += ["", "Сообщение: «%s»" % incoming.strip().replace("\n", " ")[:400]]
    if draft_text:
        lines += ["", "Черновик ответа: «%s»"
                  % draft_text.strip().replace("\n", " ")[:300]]
    lines += ["", "/ok %d   ·   /ok %d 2   ·   /time %d 29.08 16:00   ·   /no %d"
              % (app.id, app.id, app.id, app.id)]

    req = OwnerRequest(
        application_id=app.id, kind=OwnerRequestKind.SLOT_CONFIRM.value,
        question="\n".join(lines), payload_json=payload,
        expires_at=(datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(hours=s.owner_decision_ttl_hours)))
    sess.add(req)
    sess.flush()
    _to_bot(req, sess)
    return req


def create_human_request(sess, app: Application, job: Job, incoming: str,
                         reason: str, draft_text: str = "",
                         draft_note: str = "") -> OwnerRequest:
    """Карточка «автоматика не берётся отвечать»."""
    s = get_settings()
    lines = ["✋ #%d · %s" % (app.id, _title(job)),
             "Нужен твой ответ (%s)." % reason,
             "", "Рекрутёр @%s: «%s»"
             % (job.contact_handle or "?",
                (incoming or "").strip().replace("\n", " ")[:600])]
    if draft_text:
        lines += ["", "Черновик: «%s»" % draft_text.replace("\n", " ")[:700],
                  "", "/send %d   ·   /say %d свой текст   ·   /skip %d"
                  % (app.id, app.id, app.id)]
    else:
        lines += ["", "Черновика нет (%s)." % (draft_note or "LLM недоступна"),
                  "/say %d свой текст   ·   /skip %d" % (app.id, app.id)]

    req = OwnerRequest(
        application_id=app.id, kind=OwnerRequestKind.NEEDS_HUMAN.value,
        question="\n".join(lines),
        payload_json={"incoming": (incoming or "")[:900], "draft": draft_text,
                      "reason": reason, "handle": job.contact_handle},
        expires_at=(datetime.now(timezone.utc).replace(tzinfo=None)
                    + timedelta(hours=max(24, s.owner_decision_ttl_hours))))
    sess.add(req)
    sess.flush()
    _to_bot(req, sess)
    return req


def _to_bot(req: OwnerRequest, sess) -> None:
    """Карточку — в очередь бота, если он назначен каналом владельца.

    Клавиатуру строит bot.cards, но импортируется он лениво: пакет бота не
    должен быть обязателен для работы автопилота.
    """
    from . import notify
    from .taskhub import prepare_manual_request
    if req.application_id and "incoming_message_ids" not in (req.payload_json or {}):
        req.payload_json = dict(req.payload_json or {}, incoming_message_ids=list(sess.scalars(
            select(Message.id).where(Message.application_id == req.application_id,
                                     Message.direction == "in"))))
    prepare_manual_request(sess, req)
    if get_settings().owner_channel not in ("bot", "both"):
        return
    try:
        from .bot.cards import keyboard_for, strip_hints
        markup = keyboard_for(req)
        # В боте на месте команд стоят кнопки. Оставить и то, и другое —
        # значит предложить владельцу два разных способа сделать одно и то же
        # в одном сообщении; подсказка нужна только каналу «Избранное», где
        # набрать команду — единственный способ ответить.
        text = strip_hints(req.question)
    except Exception:
        markup, text = None, req.question
    notify.push_card(req, markup=markup, text=text, sess=sess)


def open_request_for(sess, app_id: int, kind: str | None = None) -> OwnerRequest | None:
    """Незакрытая карточка по заявке — чтобы не плодить дубли на каждый опрос."""
    q = (select(OwnerRequest)
         .where(OwnerRequest.application_id == app_id, OwnerRequest.decision == "")
         .order_by(OwnerRequest.id.desc()))
    if kind:
        q = q.where(OwnerRequest.kind == kind)
    return sess.scalars(q).first()


def retry_missing_drafts(limit: int = 5) -> dict:
    """Повторно собрать отсутствующие черновики до истечения карточки.

    Сбой LLM не должен превращать карточку в «только ручной текст» навсегда,
    но и бесконечно дёргать провайдер нельзя. Попытки и время хранятся в
    payload_json, чтобы не смешивать их с attempts исполнения решения.
    """
    if not get_settings().llm_enabled:
        return {"checked": 0, "skipped": "llm_disabled", "ready": 0}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    candidates = []
    with session_scope() as sess:
        rows = sess.scalars(
            select(OwnerRequest)
            .where(OwnerRequest.kind == OwnerRequestKind.NEEDS_HUMAN.value,
                   OwnerRequest.decision == "",
                   OwnerRequest.expires_at.is_not(None),
                   OwnerRequest.expires_at > now)
            .order_by(OwnerRequest.created_at)
            .limit(limit * 3)).all()
        for req in rows:
            payload = dict(req.payload_json or {})
            if payload.get("draft"):
                continue
            attempts = int(payload.get("_draft_attempts", 0) or 0)
            if attempts >= 2:
                continue
            last_try = payload.get("_draft_last_try", "")
            if last_try:
                try:
                    dt = datetime.fromisoformat(last_try.replace("Z", "+00:00"))
                    if dt.tzinfo:
                        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                    if (now - dt).total_seconds() < 15 * 60:
                        continue
                except (TypeError, ValueError):
                    pass
            app = sess.get(Application, req.application_id) if req.application_id else None
            job = sess.get(Job, app.job_id) if app else None
            if not app or not job:
                continue
            history = sess.execute(
                select(Message.direction, Message.body)
                .where(Message.application_id == app.id)
                .order_by(Message.id)).all()
            payload["_draft_attempts"] = attempts + 1
            payload["_draft_last_try"] = utcnow().isoformat()
            # Сначала фиксируем claim попытки, чтобы два автопилота не
            # отправили один и тот же запрос к LLM параллельно.
            req.payload_json = payload
            candidates.append((req.id, app.id, job.title or job.tag or "разработчик",
                               job.description_raw or "", payload.get("incoming", ""),
                               [(d, b) for d, b in history], attempts + 1))
            if len(candidates) >= limit:
                break

    from .convo.draft import draft_reply
    checked = len(candidates)
    ready = 0
    for req_id, app_id, role, jd_text, incoming, history, attempt in candidates:
        draft = draft_reply(role, jd_text, incoming, history, intent="unknown")
        if not draft.text:
            continue
        with session_scope() as sess:
            req = sess.get(OwnerRequest, req_id)
            if not req or req.decision or (req.payload_json or {}).get("draft"):
                continue
            payload = dict(req.payload_json or {})
            payload["draft"] = draft.text[:700]
            req.payload_json = payload
            req.question = (req.question.rstrip() +
                            "\n\nЧерновик после повторной попытки: «%s»\n"
                            "/send %d   ·   /say %d свой текст   ·   /skip %d"
                            % (draft.text.replace("\n", " ")[:700],
                               app_id, app_id, app_id))
            # Если карточка уже была доставлена в бота, обновляем её на месте;
            # новая карточка создала бы дубликат вопроса владельцу.
            if (req.owner_msg_id and req.owner_chat_id and
                    get_settings().owner_channel in ("bot", "both")):
                from . import notify
                from .bot.cards import keyboard_for, strip_hints
                notify.push(
                    "card", strip_hints(req.question),
                    chat_id=req.owner_chat_id,
                    target_msg_id=req.owner_msg_id,
                    markup=keyboard_for(req), req_id=req.id,
                    dedup="draft-retry:%d:%d" % (req.id, attempt), sess=sess)
            ready += 1
    return {"checked": checked, "ready": ready}


# ────────────────────────────────────────────────────── отправка карточек ──

async def push_pending(client, dry: bool = False) -> int:
    """Отправляет в «Избранное» карточки, которые ещё не отправлены.

    В режиме owner_channel=bot не делает ничего: карточка должна существовать
    в одном экземпляре, иначе владелец решает один и тот же вопрос дважды.
    """
    if get_settings().owner_channel not in ("saved", "both"):
        return 0
    with session_scope() as sess:
        rows = sess.scalars(
            select(OwnerRequest)
            .where(OwnerRequest.sent_at.is_(None))
            .order_by(OwnerRequest.id)).all()
        pending = [(r.id, r.question) for r in rows]

    sent = 0
    for req_id, text in pending:
        if dry:
            print("      [dry-run] карточка #%d в Избранное" % req_id)
            sent += 1
            continue
        try:
            msg = await client.send_message(OWNER_PEER, text)
        except Exception as e:
            print("      карточка #%d не ушла: %s" % (req_id, type(e).__name__))
            continue
        with session_scope() as sess:
            r = sess.get(OwnerRequest, req_id)
            r.sent_at = utcnow()
            r.owner_msg_id = getattr(msg, "id", None)
        sent += 1
    return sent


async def advance_watermark(client) -> int:
    """Двигает водяной знак «Избранного», ничего не исполняя.

    Нужен, пока управление идёт через бота: команды в «Избранном» не
    читаются, знак стоит на месте, и при возврате на этот канал разом
    исполнились бы все накопившиеся за недели /ok.
    """
    try:
        last = 0
        async for msg in client.iter_messages(OWNER_PEER, limit=1):
            last = msg.id
        if not last:
            return 0
    except Exception:
        return 0
    with session_scope() as sess:
        from .outreach.policy import get_state
        st = get_state(sess)          # не голый CampaignState: тот портил квоту
        if last > int(st.owner_last_seen_msg_id or 0):
            st.owner_last_seen_msg_id = last
    return last


async def notify(client, text: str) -> None:
    """Короткое уведомление владельцу без карточки и без ответа."""
    try:
        await client.send_message(OWNER_PEER, text)
    except Exception:
        pass


# ────────────────────────────────────────────────────── разбор команд ──

_CMD = re.compile(r"^\s*/(?P<cmd>ok|time|no|send|say|skip|close|status|stop|go|help)\b"
                  r"\s*(?P<rest>.*)$", re.I | re.S)


def parse_command(text: str) -> dict | None:
    """Команда владельца из текста сообщения. None — это не команда."""
    m = _CMD.match(text or "")
    if not m:
        return None
    cmd = m.group("cmd").lower()
    rest = (m.group("rest") or "").strip()
    out = {"cmd": cmd, "app_id": None, "arg": rest}
    m2 = re.match(r"#?(\d+)\s*(.*)$", rest, re.S)
    if m2:
        out["app_id"] = int(m2.group(1))
        out["arg"] = (m2.group(2) or "").strip()
    return out


async def poll_commands(client, limit: int = 50) -> list:
    """Читает новые сообщения владельца в «Избранном» и записывает решения.

    Возвращает список (application_id, decision, note) для применения.
    """
    with session_scope() as sess:
        st = sess.get(CampaignState, 1)
        last_seen = int(getattr(st, "owner_last_seen_msg_id", 0) or 0)

    decisions, max_id = [], last_seen
    messages = []
    try:
        async for msg in client.iter_messages(OWNER_PEER, limit=limit):
            if last_seen and msg.id <= last_seen:
                break
            messages.append(msg)
    except Exception as e:
        print("      Избранное недоступно: %s" % type(e).__name__)
        return []

    if not last_seen:
        # Первый запуск: водяного знака ещё нет, а в «Избранном» может лежать
        # старая переписка со старыми /ok. Исполнять её нельзя — только
        # ставим знак на самое свежее сообщение и начинаем слушать с него.
        max_id = max([m.id for m in messages], default=0)
        messages = []

    for msg in reversed(messages):               # от старых к новым
        max_id = max(max_id, msg.id)
        cmd = parse_command(getattr(msg, "message", "") or "")
        if not cmd:
            continue
        decisions.append(cmd)

    with session_scope() as sess:
        from .outreach.policy import get_state
        st = get_state(sess)
        st.owner_last_seen_msg_id = max_id
    return decisions


# ──────────────────────────────────────────────────── применение решений ──

async def apply_command(client, cmd: dict, dry: bool = False) -> str:
    """Выполняет одну команду владельца. Возвращает текст ответа ему.

    Решения по карточкам проходят через decisions: там атомарная постановка
    и единственная реализация исполнения, общая с кнопками бота. Здесь
    остаётся разбор команды и приведение аргумента к машинному виду.
    """
    from . import decisions

    name = cmd["cmd"]
    if name == "help":
        return HELP
    if name == "status":
        return status_text()
    if dry and name in ("stop", "go"):
        return "dry: стоп-кран не изменён"
    if name == "stop":
        set_kill_switch(True, "команда в Избранном")
        return "Стоп-кран включён: отправка остановлена. Снять — /go"
    if name == "go":
        set_kill_switch(False, "команда в Избранном")
        return "Стоп-кран снят, отправка возобновится по расписанию."

    app_id = cmd.get("app_id")
    if not app_id:
        return "Не указан номер заявки. Пример: /ok 123"

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app:
            return "Заявки #%d нет в базе." % app_id
        req = open_request_for(sess, app_id)
        slots = list((req.payload_json or {}).get("slots", [])) if req else []
        req_id = req.id if req else None
        if req is None:
            # Открытой карточки нет. Различаем «её и не было» от «по ней уже
            # решили кнопкой в боте»: иначе владелец получает «нет слотов» на
            # команду, которая опоздала на секунду, и не понимает почему.
            last = sess.scalars(
                select(OwnerRequest)
                .where(OwnerRequest.application_id == app_id)
                .order_by(OwnerRequest.id.desc())).first()
            if last is not None and last.decision:
                return ("#%d: решение уже принято (%s%s)."
                        % (app_id, last.decision,
                           ", " + last.decided_by if last.decided_by else ""))

    # Аргумент решения приводим к машинному виду прямо здесь: дальше и
    # команда из «Избранного», и кнопка в боте идут одним путём.
    arg = (cmd.get("arg") or "").strip()
    if name == "ok":
        if not slots:
            return ("У #%d нет предложенных слотов. Задай время: "
                    "/time %d 29.08 16:00" % (app_id, app_id))
        m = re.match(r"(\d+)", arg)
        idx = max(0, int(m.group(1)) - 1) if m else 0
        if idx >= len(slots):
            return "У #%d только %d вариант(а)." % (app_id, len(slots))
        arg = str(idx)
    elif name == "say" and not arg:
        return "Пустой текст. /say %d текст ответа" % app_id
    elif name == "time" and not arg:
        return "Не указано время. Формат: /time %d 29.08 16:00" % app_id

    if not req_id:
        return "#%d: нет открытой карточки на решение." % app_id

    if dry:
        return "#%d: dry — решение не записано и не исполнено" % app_id

    # Единственная точка постановки решения — атомарный claim. Если владелец
    # уже нажал кнопку в боте, команда в «Избранном» не должна отправить
    # рекрутёру второе сообщение.
    if not decisions.claim(req_id, name, arg, by="saved"):
        return "#%d: решение уже принято (возможно, кнопкой в боте)." % app_id

    res = await decisions.apply_one(client, req_id, dry=dry)
    with session_scope() as sess:
        r = sess.get(OwnerRequest, req_id)
        note = (r.decision_note or res) if r else res
    return "#%d: %s" % (app_id, note)


def set_kill_switch(on: bool, by: str = "", notify_owner: bool = True) -> str:
    """Единственная точка переключения стоп-крана.

    Каналов управления теперь три (Избранное, бот, дашборд), и каждый должен
    оставлять след: молчаливо остановленная отправка выглядит как поломка.

    notify_owner=False — для бота: он отвечает владельцу сам и сразу, а push
    приносил то же сообщение вторым экземпляром через очередь доставки.
    """
    from . import notify

    p = get_settings().kill_switch
    if on:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stop", encoding="utf-8")
        text = "⛔ Стоп-кран включён (%s). Отправка остановлена." % (by or "?")
    else:
        if p.exists():
            p.unlink()
        text = "▶️ Стоп-кран снят (%s). Отправка пойдёт по расписанию." % (by or "?")
    if notify_owner:
        # Без dedup: каждое переключение важно само по себе.
        notify.push("killswitch", text)
    return text


def _confirm_text(dt_utc: datetime, tz_name: str, meet_link: str = "") -> str:
    """Подтверждение слота рекрутёру: время в его поясе, без лишних слов."""
    line = "Спасибо, подтверждаю: %s. Буду на связи." % fmt(dt_utc, tz_name)
    if meet_link:
        line += " Ссылка на встречу с моей стороны: %s" % meet_link
    return line


def expire_stale() -> int:
    """Карточки, на которые владелец не ответил вовремя, закрываются.

    Тихо висящая карточка хуже честного «просрочено»: рекрутёр ждёт ответа,
    а система думает, что вопрос ещё в работе.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = 0
    with session_scope() as sess:
        rows = sess.scalars(
            select(OwnerRequest)
            .where(OwnerRequest.decision == "",
                   OwnerRequest.expires_at.is_not(None),
                   OwnerRequest.expires_at < now)).all()
        for r in rows:
            r.decision = "expired"
            r.answered_at = utcnow()
            n += 1
            # Гасим кнопки в чате: сообщение с живой клавиатурой у закрытой
            # карточки — ловушка, нажатие отвечало ложью «решение уже
            # принято». Outbox умеет редактировать по target_msg_id.
            if r.owner_msg_id and r.owner_chat_id:
                from . import notify
                notify.push(
                    "card_expired",
                    "⌛ Просрочено без ответа — карточка #%d закрыта.\n"
                    "Если вопрос ещё жив, рекрутёру можно ответить: /cards"
                    % r.id,
                    chat_id=r.owner_chat_id, target_msg_id=r.owner_msg_id,
                    markup={"inline_keyboard": []},
                    dedup="expired:%d" % r.id, sess=sess)
    return n


def status_text() -> str:
    """Короткая сводка для команды /status."""
    from collections import Counter

    from .outreach import policy

    with session_scope() as sess:
        apps = sess.scalars(select(Application)).all()
        c = Counter(a.status for a in apps)
        q = policy.get_quota(sess)
        st = policy.get_state(sess)
        open_reqs = sess.scalars(
            select(OwnerRequest).where(OwnerRequest.decision == "")).all()
        interviews = sess.scalars(
            select(Application)
            .where(Application.status == Status.INTERVIEW_CONFIRMED.value,
                   Application.interview_at_utc.is_not(None))
            .order_by(Application.interview_at_utc)).all()
        upcoming = [(a.id, a.interview_at_utc, sess.get(Job, a.job_id).title)
                    for a in interviews[:5]]

    s = get_settings()
    lines = ["Сводка jobhunter",
             "отправлено сегодня: %d/%d" % (q.sent_count, st.quota_ceiling),
             "ждут ответа: %d · ответили: %d · в очереди: %d"
             % (c.get(Status.AWAITING_REPLY.value, 0),
                c.get(Status.REPLIED.value, 0) + c.get(Status.IN_DIALOGUE.value, 0),
                c.get(Status.PENDING_APPROVAL.value, 0)),
             "ждут твоего решения: %d" % len(open_reqs)]
    if upcoming:
        lines.append("ближайшие интервью:")
        for aid, dt, title in upcoming:
            lines.append("  #%d %s — %s"
                         % (aid, fmt(dt.replace(tzinfo=timezone.utc), s.owner_tz),
                            (title or "")[:40]))
    if get_settings().kill_switch.exists():
        lines.append("⛔ стоп-кран активен (/go чтобы снять)")
    return "\n".join(lines)
