"""Обработка апдейтов: update → список действий. Сети здесь нет.

Возвращается план («ответить текстом», «поправить экран», «закрыть часики»),
а исполняет его runner. Благодаря этому весь разбор — включая авторизацию и
двухшаговое подтверждение — проверяется тестами без единого запроса к
Telegram.

Действие — словарь: {"do": "send"|"edit"|"answer"|"screen", ...}.
"""
from __future__ import annotations

import logging

from .. import decisions
from ..config import get_settings
from ..convo.slots import parse_owner_time
from ..db import session_scope
from ..models import Application, Batch, OwnerRequest, Status, utcnow
from . import cards, screens, state

log = logging.getLogger("bot.handlers")


def _allowed(update: dict) -> tuple:
    """(разрешено, user_id, chat_id). Проверка по from.id, не по chat.id."""
    src = update.get("message") or update.get("callback_query") or {}
    user = (src.get("from") or {}).get("id")
    chat = ((src.get("chat") or {}).get("id")
            or ((src.get("message") or {}).get("chat") or {}).get("id"))
    ids = get_settings().bot_owner_ids
    return (bool(user) and user in ids), user, chat


def handle(update: dict) -> list:
    """Разобрать апдейт. Пустой список — ничего делать не надо."""
    ok, user_id, chat_id = _allowed(update)

    if not ok:
        # Чужому сообщению не отвечаем вовсе: ответ подтверждает, что бот
        # живой и обслуживает конкретного человека. А вот «часики» на кнопке
        # закрыть надо — иначе у нажавшего висит ожидание.
        if "callback_query" in update:
            return [{"do": "answer", "cb_id": update["callback_query"]["id"],
                     "text": "Недоступно"}]
        log.info("отклонён апдейт от user_id=%s", user_id)
        return []

    if chat_id != user_id:
        note = "Открой личный чат бота: данные и действия доступны только там."
        if "callback_query" in update:
            return [{"do": "answer", "cb_id": update["callback_query"]["id"], "text": note}]
        return [{"do": "send", "chat_id": chat_id, "text": note}]

    if "callback_query" in update:
        # След каждого нажатия: «кнопки не работают» без этой строки
        # неотличимо от «нажатия не доходят», и диагностика упирается в
        # пересказ со слов владельца.
        log.info("кнопка: %r", (update["callback_query"].get("data") or "")[:40])
        return _callback(update["callback_query"], chat_id)
    if "message" in update:
        log.info("команда: %r", ((update["message"].get("text") or "")[:40]))
        return _message(update["message"], chat_id)
    return []


# ────────────────────────────────────────────────────────── команды ──

def _message(msg: dict, chat_id: int) -> list:
    text = (msg.get("text") or "").strip()
    if not text:
        return []

    # Ожидание свободного ввода после кнопки «Другое время» / «Свой текст».
    waiting = state.peek_awaited()
    if waiting and not text.startswith("/"):
        return _free_input(waiting, text, chat_id)

    cmd = text.lstrip("/").split()[0].lower() if text.startswith("/") else ""
    work_commands = {"tasks": "work_tasks_0", "results": "work_results_all_0",
                     "reading": "work_reading", "sending": "work_sending_0",
                     "tracks": "work_tracks", "feedback": "work_feedback"}
    if cmd.split("@", 1)[0] in work_commands:
        return [{"do": "screen", "chat_id": chat_id,
                 "name": work_commands[cmd.split("@", 1)[0]]}]

    if cmd in ("start", "stats", ""):
        return [{"do": "screen", "chat_id": chat_id, "name": "main"}]
    if cmd == "help":
        return [{"do": "send", "chat_id": chat_id, "text": screens.HELP}]
    if cmd == "queue":
        return [{"do": "screen", "chat_id": chat_id, "name": "queue"}]
    if cmd == "cards":
        return [{"do": "screen", "chat_id": chat_id, "name": "cards"}]
    if cmd in ("interviews", "iv"):
        return [{"do": "screen", "chat_id": chat_id, "name": "iv"}]
    if cmd == "channels":
        return [{"do": "screen", "chat_id": chat_id, "name": "ch"}]
    if cmd in ("manual", "hand"):
        return [{"do": "screen", "chat_id": chat_id, "name": "manual"}]
    if cmd.split("@", 1)[0] == "outreach":
        return _manual_outreach(chat_id, "message:%s" % msg.get("message_id", "command"))
    if cmd in ("mail", "pochta"):
        # IMAP занимает секунды — из главного цикла его выгнали: пока шёл
        # сбор, бот не забирал вообще ничего, включая нажатия кнопок.
        return [{"do": "send", "chat_id": chat_id,
                 "text": "📬 Собираю почту, сводка будет через пару секунд…"},
                {"do": "task", "chat_id": chat_id, "task": "mail"}]
    if cmd in ("stop", "go"):
        from ..owner import set_kill_switch
        note = set_kill_switch(cmd == "stop", "бот", notify_owner=False)
        return [{"do": "send", "chat_id": chat_id, "text": note},
                {"do": "screen", "chat_id": chat_id, "name": "main"}]

    return [{"do": "send", "chat_id": chat_id,
             "text": "Не знаю такой команды. /help — список."}]


def _free_input(waiting: dict, text: str, chat_id: int) -> list:
    """Владелец прислал время или текст ответа — показываем на подтверждение."""
    req_id, kind = waiting["req_id"], waiting["kind"]

    if kind == "time":
        when = parse_owner_time(text, get_settings().owner_tz)
        if not when:
            return [{"do": "send", "chat_id": chat_id,
                     "text": "Не разобрал время «%s». Формат: 29.08 16:00"
                             % text[:40]}]
        from ..convo.slots import fmt
        state.await_input("time", req_id, text)
        return [{"do": "send", "chat_id": chat_id,
                 "text": "Подтвердить интервью на %s?" % fmt(when, get_settings().owner_tz),
                 "markup": cards.confirm_keyboard(req_id, "time")}]

    # say: сообщение рекрутёру отозвать нельзя — показываем финальный текст.
    state.await_input("say", req_id, text)
    return [{"do": "send", "chat_id": chat_id,
             "text": "Отправить рекрутёру этот текст?\n\n%s" % text[:1500],
             "markup": cards.confirm_keyboard(req_id, "say")}]


# ───────────────────────────────────────────────────────── кнопки ──

def _callback(cbq: dict, chat_id: int) -> list:
    data = cards.parse_cb(cbq.get("data", ""))
    cb_id = cbq["id"]
    msg = cbq.get("message") or {}
    msg_id = msg.get("message_id")

    if not data:
        return [{"do": "answer", "cb_id": cb_id, "text": ""}]

    if data["kind"] == "work":
        from .workbench import callback
        return callback(data, cb_id, chat_id, msg_id)

    if data["kind"] == "screen":
        return [{"do": "answer", "cb_id": cb_id},
                {"do": "screen", "chat_id": chat_id, "name": data["screen"],
                 "msg_id": msg_id}]

    if data["kind"] == "killswitch":
        from ..owner import set_kill_switch
        set_kill_switch(data["on"], "бот", notify_owner=False)
        return [{"do": "answer", "cb_id": cb_id,
                 "text": "Стоп-кран включён" if data["on"] else "Отправка возобновлена"},
                {"do": "screen", "chat_id": chat_id, "name": "main",
                 "msg_id": msg_id}]

    if data["kind"] == "queue" and data["action"] == "approve10":
        n = _approve_top(10)
        return [{"do": "answer", "cb_id": cb_id, "text": "Одобрено: %d" % n},
                {"do": "screen", "chat_id": chat_id, "name": "queue",
                 "msg_id": msg_id}]

    if data["kind"] == "queue" and data["action"].startswith("d"):
        # Прямые письма: пауза/возобновление канала, ручное одобрение удержанного,
        # «не писать этой компании».
        from ..outreach import direct
        action = data["action"]
        if action == "dpause":
            direct.pause("остановлено владельцем из бота")
            note = "Канал на паузе"
        elif action == "dresume":
            direct.resume()
            note = "Канал возобновлён"
        elif action.startswith("dappr") and action[5:].isdigit():
            note = direct.approve_held(int(action[5:]))
        elif action.startswith("ddnc") and action[4:].isdigit():
            note = direct.block_company(int(action[4:]))
        else:
            note = "неизвестное действие"
        return [{"do": "answer", "cb_id": cb_id, "text": note[:180]},
                {"do": "screen", "chat_id": chat_id, "name": "direct", "msg_id": msg_id}]

    if data["kind"] == "manual_telegram":
        from .. import manual_telegram as manual_tg
        aid, action = data["app_id"], data["action"]
        answer = {"do": "answer", "cb_id": cb_id}
        if chat_id not in get_settings().bot_owner_ids:
            return [{**answer, "text": "Ручные отклики — только в личном чате бота"}]
        if action == "next":
            return [answer] + _manual_outreach(chat_id, "callback:" + cb_id)
        if action == "cancel":
            return [{**answer, "text": "Ничего не изменено"}]
        if action == "why":
            return [answer, {"do": "send", "chat_id": chat_id,
                             "text": manual_tg.explain_card(aid)}]
        if action == "skip_reason":
            from .workbench import skip_keyboard
            if not manual_tg.get_card(aid):
                return [{**answer, "text": "Карточка уже обработана"}]
            return [answer, {"do": "send", "chat_id": chat_id,
                             "text": f"Почему не подходит #{aid}? Настройки поиска не изменятся.",
                             "markup": skip_keyboard(aid)}]
        if action == "undo":
            ok, note = manual_tg.unmark(aid, chat_id)
            if not ok:
                return [{**answer, "text": note[:180]}]
            # Карточка снова в работе — выдаём её же заново, одним сообщением.
            manual_tg.queue_current(chat_id, "undo:" + cb_id)
            return [answer, {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
                             "text": f"↩️ #{aid}: {note}",
                             "markup": {"inline_keyboard": []}}]
        if action in ("text", "handle", "cv", "confirm"):
            row = manual_tg.get_card(aid)
            if not row:
                return [{**answer, "text": "Карточка уже обработана"}]
            if action in ("text", "handle", "cv"):
                if row["problem"]:
                    return [answer, {"do": "send", "chat_id": chat_id,
                                     "text": "Не отправляй: " + row["problem"]}]
                if action == "cv":
                    manual_tg.queue_cv(chat_id, aid, cb_id)
                    return [{**answer, "text": "Присылаю PDF сюда в чат"}]
                return [answer, {"do": "send", "chat_id": chat_id,
                                 "text": row["text"] if action == "text" else "@" + row["handle"]}]
            # «confirm» со старых карточек — теперь то же, что «sent»: второй
            # диалог заменён кнопкой «↩️ Вернуть» на 15 минут.
            action = "sent"
        ok, note = manual_tg.mark(aid, action, next_chat_id=chat_id)
        if not ok:
            return [answer, {"do": "send", "chat_id": chat_id, "text": note}]
        label = "✅" if action == "sent" else "⏭"
        return [answer, {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
                         "text": f"{label} #{aid}: {note}",
                         "markup": manual_tg.after_mark_keyboard(aid, undo=(action == "sent"))}]

    if data["kind"] == "manual":
        if data["action"] == "form":
            # Только план: сборка анкеты ходит в сеть (до 6 секунд), и
            # делать это до гашения часиков значило вешать ВСЕ кнопки —
            # 98% нажатий не находили готового пакета и ждали сеть.
            return [{"do": "answer", "cb_id": cb_id, "text": "Готовлю анкету…"},
                    {"do": "task", "chat_id": chat_id,
                     "task": "form", "app_id": data["app_id"]}]
        if data["action"] == "letter":
            # Письмо жжёт ~220 мс CPU (скоринг + генерация) — тоже после
            # ответа на нажатие, а не до.
            return [{"do": "answer", "cb_id": cb_id, "text": "Пишу письмо…"},
                    {"do": "task", "chat_id": chat_id,
                     "task": "letter", "app_id": data["app_id"]}]
        from ..manual_apply import mark
        note = mark(data["app_id"], data["action"])
        # Карточку не удаляем, а гасим: список решений за день остаётся
        # видимым, и понятно, что уже разобрано.
        return [{"do": "answer", "cb_id": cb_id, "text": note[:60]},
                {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
                 "text": "✔️ " + note, "markup": {"inline_keyboard": []}}]

    if data["kind"] == "decision":
        return _decision(data, cb_id, chat_id, msg_id)
    return [{"do": "answer", "cb_id": cb_id}]


def _manual_outreach(chat_id: int, request_key: str = "") -> list:
    from .. import manual_telegram as manual_tg
    if chat_id not in get_settings().bot_owner_ids:
        return [{"do": "send", "chat_id": chat_id,
                 "text": "Открой личный чат бота и нажми /outreach — данные не публикуются в группе."}]
    aid = manual_tg.queue_current(chat_id, request_key)
    if not aid:
        return [{"do": "send", "chat_id": chat_id,
                 "text": "Сейчас нет подходящих новых откликов для ручной отправки."}]
    return [{"do": "send", "chat_id": chat_id, "text": manual_tg.WARNING}]


def _decision(data: dict, cb_id: str, chat_id: int, msg_id: int) -> list:
    req_id, action, arg = data["req_id"], data["action"], data["arg"]

    # Двухшаговое подтверждение свободного ввода.
    if action == "cancel":
        state.clear_awaited()
        return [{"do": "answer", "cb_id": cb_id, "text": "Отменено"},
                {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
                 "text": "✖️ Отменено.", "markup": {"inline_keyboard": []}}]

    if action in ("yestime", "yessay"):
        waiting = state.take_awaited()
        if not waiting or waiting["req_id"] != req_id:
            return [{"do": "answer", "cb_id": cb_id,
                     "text": "Срок ввода истёк, начни заново"}]
        real = "time" if action == "yestime" else "say"
        return _claim(req_id, real, waiting["text"], cb_id, chat_id, msg_id)

    # Кнопки, требующие ввода: сначала спрашиваем, решение ставим потом.
    if action in ("time", "say"):
        state.await_input(action, req_id)
        prompt = ("Напиши время в формате 29.08 16:00"
                  if action == "time" else "Напиши текст ответа рекрутёру")
        return [{"do": "answer", "cb_id": cb_id},
                {"do": "send", "chat_id": chat_id, "text": prompt}]

    return _claim(req_id, action, arg, cb_id, chat_id, msg_id)


def _claim(req_id: int, action: str, arg: str, cb_id: str, chat_id: int,
           msg_id: int) -> list:
    """Поставить решение и перерисовать карточку в «принято»."""
    with session_scope() as sess:
        req = sess.get(OwnerRequest, req_id)
        question = cards.strip_hints(req.question) if req else ""
        already = bool(req and req.decision)
        app = sess.get(Application, req.application_id) if req and req.application_id else None
        if app and (app.outcome or "").startswith("manual_tg_") and action in decisions.NEEDS_SEND:
            return [{"do": "answer", "cb_id": cb_id, "text": "Этот диалог ведёшь вручную"},
                    {"do": "screen", "chat_id": chat_id, "name": f"work_task_{app.id}"}]
        if app and action in decisions.NEEDS_SEND and "incoming_message_ids" in (req.payload_json or {}):
            from sqlalchemy import select

            from ..models import Message
            snapshot = req.payload_json["incoming_message_ids"]
            latest = sess.scalar(select(Message.id).where(Message.application_id == app.id,
                                  Message.direction == "in").order_by(Message.id.desc()).limit(1))
            if latest is not None and latest not in snapshot:
                return [{"do": "answer", "cb_id": cb_id, "text": "Пришло новое сообщение — обнови карточку"},
                        {"do": "screen", "chat_id": chat_id, "name": f"work_task_{app.id}"}]
    if not req_id or not question:
        return [{"do": "answer", "cb_id": cb_id, "text": "Карточка не найдена"}]
    if already:
        # «Решение уже принято» на просроченной карточке — ложь: решения не
        # было, карточку закрыл таймер. Говорим правду и куда идти дальше.
        with session_scope() as sess:
            expired = (sess.get(OwnerRequest, req_id).decision == "expired")
        note = ("Карточка просрочена и закрыта — свежие в /cards"
                if expired else "Решение уже принято")
        return [{"do": "answer", "cb_id": cb_id, "text": note},
                {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
                 "text": ("⌛ %s" % question) if expired else question,
                 "markup": {"inline_keyboard": []}}]

    ids = get_settings().bot_owner_ids
    by = "bot:%s" % (next(iter(ids)) if ids else "?")
    if not decisions.claim(req_id, action, arg, by=by):
        return [{"do": "answer", "cb_id": cb_id, "text": "Решение уже принято"}]

    labels = {"ok": "подтверждаю время", "time": "своё время",
              "no": "отказ от времени", "send": "отправляю черновик",
              "say": "отправляю свой текст", "skip": "пропускаю",
              "close": "отказ подтверждён, закрываю"}
    note = labels.get(action, action)
    return [{"do": "answer", "cb_id": cb_id, "text": "Принято"},
            {"do": "edit", "chat_id": chat_id, "msg_id": msg_id,
             "text": "%s\n\n⏳ Принято: %s — исполняется…" % (question, note),
             "markup": {"inline_keyboard": []}}]


def run_task(act: dict) -> str:
    """Тяжёлая работа кнопок — исполняется в потоке доставки, не в цикле.

    Возвращает текст для отправки владельцу. Ошибка — тоже текст: молчание
    после «Готовлю анкету…» хуже честного «не получилось».
    """
    kind = act.get("task", "")
    try:
        if kind == "form":
            return _manual_form(act["app_id"])
        if kind == "letter":
            return _manual_letter(act["app_id"])
        if kind == "mail":
            from ..convo.mail_digest import run as digest_run
            return digest_run(hours=24)
    except Exception as e:                                 # noqa: BLE001
        log.warning("задача %s: %s: %s", kind, type(e).__name__, str(e)[:120])
        return "Не получилось (%s) — попробуй ещё раз." % type(e).__name__
    return "Неизвестная задача"


def _short_http():
    """HTTP-клиент с поводком под лимит ответа на нажатие кнопки."""
    import httpx
    return httpx.Client(trust_env=False, follow_redirects=True, timeout=6.0,
                        headers={"User-Agent": "Mozilla/5.0"})


def _manual_form(app_id: int) -> str:
    """Готовая анкета: поля, ответы и то, что нужно решить владельцу.

    Сначала берём готовый пакет из базы: анкеты собирает утренний шаг, и к
    моменту нажатия они обычно уже есть. Ходить в сеть ради сверки
    отпечатка формы нельзя — у Telegram около десяти секунд на ответ
    кнопке, а таймаут HTTP по умолчанию двадцать пять: владелец увидел бы
    висящие часики вместо анкеты.
    """
    try:
        from ..apply.packet import build_packet, packet_of, render
        pkt = packet_of(app_id)
        if pkt is None:
            # Пакета нет — собираем на месте, но с коротким поводком.
            pkt = build_packet(app_id, http=_short_http())
    except Exception as e:                                 # noqa: BLE001
        return "📝 Анкету получить не удалось (%s)" % type(e).__name__
    if pkt is None:
        return ("📝 Для этой вакансии схема формы недоступна — "
                "заполняется вручную по ссылке из карточки.")
    return render(pkt)


def _manual_letter(app_id: int) -> str:
    """Сопроводительное к ручной вакансии — скопировать и вставить в форму."""
    from ..match.scorer import score_job
    from ..models import Job
    from ..tailor.message import generate, source_label
    from ..tailor.select import _pick_lang
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app:
            return "Заявка #%d не найдена" % app_id
        job = sess.get(Job, app.job_id)
        lang = _pick_lang(job.description_raw or "", job.title or "")
        score = score_job(job.title or "", job.tag or "",
                          job.description_raw or "")
        from ..tailor.roletitle import display_role
        role = display_role(job.title or "", job.tag or "", job.description_raw or "", lang)
        msg = generate(role, job.description_raw or "", score,
                       seed_str=job.external_uuid,
                       source=source_label(job.source, lang=lang), lang=lang)
        return ("📋 Письмо для «%s» — скопируй в форму:\n\n%s"
                % ((job.title or "вакансия")[:60], msg.text))


def _approve_top(n: int) -> int:
    """Одобрить топ очереди — то же, что кнопка на дашборде.

    gate_passed в выборке обязателен: заявка с непройденным гейтом (след
    инцидента с repair_queue) со score=100 вставала первой, transition()
    кидал TransitionError, и session_scope откатывал ВСЮ пачку — кнопка
    «Одобрить топ-10» пять дней молча не одобряла ничего.
    """
    with session_scope() as sess:
        from sqlalchemy import select

        from ..match.explain import approval_problem
        from ..models import Job
        rows = sess.scalars(
            select(Application)
            .where(Application.status.in_((Status.PENDING_APPROVAL.value,
                                          Status.FOLLOWUP_PENDING_APPROVAL.value)),
                   Application.gate_passed.is_(True))
            .order_by(Application.score.desc())).all()
        rows = [a for a in rows if a.sent_at or not approval_problem(a, sess.get(Job, a.job_id))][:n]
        if not rows:
            return 0
        batch = Batch(planned_count=len(rows), approved_at=utcnow(),
                      approved_count=len(rows))
        sess.add(batch)
        sess.flush()
        done = 0
        for a in rows:
            # Одна недопустимая заявка не должна валить остальные девять.
            try:
                a.transition(Status.APPROVED)
            except Exception as e:                          # noqa: BLE001
                log.warning("одобрение %s пропущено: %s", a.id, e)
                continue
            a.approved_at = utcnow()
            a.batch_id = batch.id
            done += 1
        batch.planned_count = batch.approved_count = done
        return done
