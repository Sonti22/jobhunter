"""Бот-пульт: авторизация, кнопки, очередь решений, уведомления.

Сети нет ни в одном тесте: handlers возвращает план действий, а не выполняет
его, поэтому весь разбор проверяется на временной базе.
"""
import ast
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

OWNER = 5875908057
STRANGER = 111222333


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "bot.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = str(OWNER)
    os.environ["OWNER_CHANNEL"] = "bot"
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture()
def card(db):
    """Карточка SLOT_CONFIRM с двумя слотами и её заявка."""
    from jobhunter.models import Application, Job, OwnerRequest, Status
    when = datetime.now(timezone.utc) + timedelta(days=1)
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="test",
                  title="Python Backend", contact_handle="hr_" + uuid.uuid4().hex[:6])
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=Status.INTERVIEW_PROPOSED.value,
                          score=80)
        sess.add(app)
        sess.flush()
        req = OwnerRequest(
            application_id=app.id, kind="slot_confirm",
            question="🗓 #%d · Python Backend\nВыбери время:\n/ok %d"
                     % (app.id, app.id),
            payload_json={"slots": [
                {"utc": when.replace(microsecond=0).isoformat(),
                 "tz": "Europe/Moscow", "raw": "завтра 15:00",
                 "confidence": 0.9, "has_time": True},
                {"utc": (when + timedelta(days=1)).replace(microsecond=0).isoformat(),
                 "tz": "Europe/Moscow", "raw": "послезавтра 11:00",
                 "confidence": 0.8, "has_time": True}]})
        sess.add(req)
        sess.flush()
        return {"req_id": req.id, "app_id": app.id}


def upd_msg(text, user_id=OWNER, uid=1):
    return {"update_id": uid,
            "message": {"message_id": uid, "text": text,
                        "from": {"id": user_id}, "chat": {"id": user_id}}}


def upd_cb(data, user_id=OWNER, uid=2):
    return {"update_id": uid,
            "callback_query": {"id": "cb%d" % uid, "data": data,
                               "from": {"id": user_id},
                               "message": {"message_id": 500,
                                           "chat": {"id": user_id}}}}


# ── авторизация ────────────────────────────────────────────────────────

def test_stranger_message_ignored(db):
    from jobhunter.bot import handlers
    assert handlers.handle(upd_msg("/start", user_id=STRANGER)) == []


def test_stranger_callback_gets_neutral_answer(db):
    from jobhunter.bot import handlers
    acts = handlers.handle(upd_cb("s:stats", user_id=STRANGER))
    assert len(acts) == 1
    assert acts[0]["do"] == "answer"
    # В ответе не должно быть ни одного поля из базы.
    assert acts[0]["text"] == "Недоступно"


def test_owner_passes(db):
    from jobhunter.bot import handlers
    acts = handlers.handle(upd_msg("/start"))
    assert acts and acts[0]["do"] == "screen"


def test_empty_allowlist_is_fail_closed(db, monkeypatch):
    """Пустой белый список — бот не делает ничего даже для владельца."""
    from jobhunter.config import get_settings
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "")
    get_settings.cache_clear()
    try:
        from jobhunter.bot import handlers
        assert handlers.handle(upd_msg("/start")) == []
    finally:
        monkeypatch.setenv("BOT_ALLOWED_USER_IDS", str(OWNER))
        get_settings.cache_clear()


# ── экраны ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["main", "stats", "funnel", "queue",
                                  "iv", "cards", "ch"])
def test_screens_render(db, name):
    from jobhunter.bot import screens
    text, markup = screens.render(name)
    assert text and len(text) < 4096
    assert "inline_keyboard" in markup


def test_screen_numbers_match_report(db):
    from jobhunter import report
    from jobhunter.bot import screens
    f = report.funnel()
    text, _ = screens.funnel()
    if f["sent"]:
        assert str(f["sent"]) in text


# ── кнопки ─────────────────────────────────────────────────────────────

def test_callback_data_fits_limit(db, card):
    from jobhunter.bot import cards
    from jobhunter.models import OwnerRequest
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, card["req_id"])
        kb = cards.keyboard_for(req)
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= cards.CB_MAX


def test_slot_buttons_match_slots(db, card):
    from jobhunter.bot import cards
    from jobhunter.models import OwnerRequest
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, card["req_id"])
        kb = cards.keyboard_for(req)
    oks = [b for row in kb["inline_keyboard"] for b in row
           if ":ok:" in b["callback_data"]]
    assert len(oks) == 2


def test_needs_human_without_draft_has_no_send_button(db):
    from jobhunter.bot import cards
    from jobhunter.models import OwnerRequest
    req = OwnerRequest(id=999, kind="needs_human", question="✋ вопрос",
                       payload_json={"draft": ""})
    kb = cards.keyboard_for(req)
    assert not any("send" in b["callback_data"]
                   for row in kb["inline_keyboard"] for b in row)


def test_strip_hints_removes_commands():
    from jobhunter.bot.cards import strip_hints
    out = strip_hints("🗓 #12 · Роль\nВремя: пт 16:00\n/ok 12 · /no 12")
    assert "/ok" not in out and "Время" in out


def test_card_in_bot_has_no_command_hints(db, card):
    """В боте команды заменены кнопками — дублировать их в тексте нельзя.

    Иначе владельцу предлагают два разных способа сделать одно и то же в
    одном сообщении, и один из них (набрать /ok 12) выглядит сломанным.
    """
    from jobhunter import notify, owner
    from jobhunter.models import OwnerRequest

    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, card["req_id"])
        assert "/ok" in req.question, "фикстура должна содержать подсказку"
        owner._to_bot(req, sess)

    rows = [r for r in notify.pending(50)
            if r["owner_request_id"] == card["req_id"]]
    assert rows, "карточка не попала в очередь бота"
    assert "/ok" not in rows[-1]["text"]
    assert "Выбери время" in rows[-1]["text"], "полезный текст потерян"
    assert rows[-1]["markup"].get("inline_keyboard"), "кнопки не приложены"


# ── очередь решений ────────────────────────────────────────────────────

def test_claim_is_atomic(db, card):
    from jobhunter import decisions
    assert decisions.claim(card["req_id"], "skip", by="bot:1") is True
    assert decisions.claim(card["req_id"], "ok", "0", by="saved") is False


def test_button_then_saved_command_does_not_double_send(db, card):
    """Кнопка в боте и /ok в «Избранном» по одной карточке — одно решение."""
    import asyncio

    from jobhunter import owner
    from jobhunter.bot import handlers

    acts = handlers.handle(upd_cb("d:%d:ok:0" % card["req_id"]))
    assert any(a.get("text") == "Принято" for a in acts)

    sent = []

    class FakeClient:
        async def send_message(self, peer, text):
            sent.append((peer, text))
            return type("M", (), {"id": 1})()

    answer = asyncio.run(owner.apply_command(
        FakeClient(), {"cmd": "ok", "app_id": card["app_id"], "arg": ""},
        dry=True))
    assert "уже принято" in answer
    assert sent == []


def test_pending_excludes_applied(db, card):
    from jobhunter import decisions
    decisions.claim(card["req_id"], "skip", by="bot:1")
    assert card["req_id"] in decisions.pending()
    decisions.finish(card["req_id"], True, "закрыто")
    assert card["req_id"] not in decisions.pending()


def test_lease_caps_attempts(db, card):
    from jobhunter import decisions
    from jobhunter.models import OwnerRequest
    decisions.claim(card["req_id"], "skip", by="bot:1")
    got = []
    for _ in range(4):
        got.append(decisions._lease(card["req_id"]))
        with db.session_scope() as sess:
            sess.get(OwnerRequest, card["req_id"]).next_try_at = None
    assert sum(1 for g in got if g) == decisions.MAX_ATTEMPTS


# ── уведомления ────────────────────────────────────────────────────────

def test_dedup_prevents_duplicates(db):
    from jobhunter import notify
    key = "test:%s" % uuid.uuid4().hex
    notify.push("error", "первое", dedup=key)
    notify.push("error", "второе", dedup=key)
    rows = [r for r in notify.pending(50) if r["text"] in ("первое", "второе")]
    assert len(rows) == 1


def test_push_card_respects_channel(db, monkeypatch):
    from jobhunter import notify
    from jobhunter.config import get_settings
    from jobhunter.models import OwnerRequest

    req = OwnerRequest(id=12345, kind="needs_human", question="✋ тест канала",
                       payload_json={})
    monkeypatch.setenv("OWNER_CHANNEL", "saved")
    get_settings.cache_clear()
    try:
        notify.push_card(req)
        assert not [r for r in notify.pending(50) if r["text"] == "✋ тест канала"]
    finally:
        monkeypatch.setenv("OWNER_CHANNEL", "bot")
        get_settings.cache_clear()


def test_mark_sent_records_card_location(db, card):
    from jobhunter import notify
    from jobhunter.models import OwnerRequest
    notify.push("card", "карточка", dedup="card:test:%d" % card["req_id"],
                req_id=card["req_id"])
    row = [r for r in notify.pending(50) if r["text"] == "карточка"][0]
    notify.mark_sent(row["id"], msg_id=777, chat_id=OWNER)
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, card["req_id"])
        assert req.owner_msg_id == 777 and req.channel == "bot"


# ── состояние ──────────────────────────────────────────────────────────

def test_offset_advances_and_dedups(db):
    from jobhunter.bot import state
    state.set_offset(100)
    assert state.get()["offset"] == 101
    assert state.seen(100) is True
    assert state.seen(101) is False


def test_awaiting_expires(db):
    from jobhunter.bot import state
    state.await_input("say", 42, "текст")
    assert state.peek_awaited()["req_id"] == 42
    taken = state.take_awaited()
    assert taken["text"] == "текст"
    assert state.peek_awaited() is None


# ── архитектурная граница ──────────────────────────────────────────────

def test_bot_never_imports_telethon():
    """Второй MTProto-клиент на общей сессии = разлогин аккаунта.

    Проверяем импорты статически: договорённость в комментарии рано или
    поздно нарушат, а тест — нет.
    """
    bot_dir = Path(__file__).resolve().parent.parent / "jobhunter" / "bot"
    offenders = []
    for path in bot_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] == "telethon" for n in names):
                offenders.append(path.name)
    assert not offenders, "telethon импортируется в %s" % offenders


def test_callback_answer_goes_first(monkeypatch):
    """Часики на кнопке гаснут раньше тяжёлой работы.

    План действий может прийти в любом порядке, но «answer» обязан уйти
    первым: пока бот отправляет длинный текст, владелец видит крутящийся
    индикатор и думает, что кнопка не сработала.
    """
    from jobhunter.bot import runner

    calls = []

    class _Api:
        class TokenRevoked(Exception):
            pass

        @staticmethod
        def answer_callback_query(cb_id, text="", http=None):
            calls.append("answer")

        @staticmethod
        def send_message(chat_id, text, markup=None, http=None):
            calls.append("send")

        @staticmethod
        def edit_message_text(*a, **kw):
            calls.append("edit")

    monkeypatch.setattr(runner, "api", _Api)
    # Намеренно в «неправильном» порядке — так его строят обработчики.
    runner._apply([{"do": "send", "chat_id": 1, "text": "длинный текст"},
                   {"do": "answer", "cb_id": "x"}], http=None)
    assert calls[0] == "answer", calls


def test_form_button_uses_cached_packet(monkeypatch):
    """Кнопка «Анкета» не ходит в сеть, если пакет уже собран."""
    from jobhunter.bot import handlers

    def _boom(*a, **kw):
        raise AssertionError("сетевая сборка при готовом пакете")

    monkeypatch.setattr("jobhunter.apply.packet.build_packet", _boom)
    monkeypatch.setattr(
        "jobhunter.apply.packet.packet_of",
        lambda app_id: __import__("jobhunter.apply.packet", fromlist=["Packet"])
        .Packet(app_id=app_id, provider="greenhouse", apply_url="http://x",
                title="Test"))
    out = handlers._manual_form(1)
    assert "Анкета" in out or "Test" in out


def test_delivery_is_fast_enough_for_a_batch(monkeypatch):
    """Утренняя пачка не должна растягиваться на десятки секунд.

    Пауза между сообщениями 1.1 секунды превращала пятнадцать карточек в
    сорок секунд ожидания — владелец успевал закрыть чат. Проверяем не
    саму константу, а бюджет: пачка обязана уходить быстрее пяти секунд.
    """
    from jobhunter.bot import outbox

    assert outbox.PAUSE * 15 < 6.0, (
        "пачка из 15 карточек идёт %.0f с — слишком долго" % (outbox.PAUSE * 15))


def test_delivery_does_not_wait_for_long_poll():
    """Доставка живёт отдельным потоком, а не в цикле опроса.

    В одном цикле с getUpdates уведомление ждало окончания long-poll —
    до двадцати пяти секунд, хотя отправить его можно сразу.
    """
    import inspect

    from jobhunter.bot import runner

    src = inspect.getsource(runner.run_forever)
    assert "threading.Thread" in src, "поток доставки исчез"
    # В теле основного цикла drain остаться не должен: иначе два писателя
    # в одну очередь и двойная доставка.
    after_loop = src.split("while True:", 1)[1]
    assert "outbox.drain" not in after_loop,         "доставка вернулась в цикл опроса — уведомления снова будут ждать"


def test_command_sends_fresh_screen_down(monkeypatch):
    """Команда /stats шлёт новый экран вниз, а не правит уехавший вверх.

    Правка старого сообщения выглядела как зависание: ответ приходил за
    полсекунды, но в сотне сообщений выше, куда владелец не смотрит.
    """
    from jobhunter.bot import runner

    calls = []

    class _Api:
        class TokenRevoked(Exception):
            pass

        @staticmethod
        def edit_message_text(chat_id, msg_id, text, markup, http=None):
            calls.append(("edit", msg_id))

        @staticmethod
        def send_message(chat_id, text, markup=None, http=None):
            calls.append(("send", None))
            return {"message_id": 999}

    monkeypatch.setattr(runner, "api", _Api)
    monkeypatch.setattr(runner.screens, "render",
                        lambda name: ("экран", {"inline_keyboard": []}))
    monkeypatch.setattr(runner.state, "get",
                        lambda: {"screen_msg_id": 5, "screen_chat_id": 1})
    monkeypatch.setattr(runner.state, "set_screen", lambda *a: None)

    # Команда: без msg_id → должен уйти НОВЫЙ экран.
    runner._show_screen({"chat_id": 1, "name": "stats"}, http=None)
    assert ("send", None) in calls, "команда обязана слать свежий экран вниз"


def test_button_edits_in_place(monkeypatch):
    """Нажатие кнопки правит то сообщение, на котором нажали."""
    from jobhunter.bot import runner

    calls = []

    class _Api:
        class TokenRevoked(Exception):
            pass

        @staticmethod
        def edit_message_text(chat_id, msg_id, text, markup, http=None):
            calls.append(("edit", msg_id))

        @staticmethod
        def send_message(*a, **kw):
            calls.append(("send", None))
            return {"message_id": 1}

    monkeypatch.setattr(runner, "api", _Api)
    monkeypatch.setattr(runner.screens, "render",
                        lambda name: ("экран", {"inline_keyboard": []}))
    monkeypatch.setattr(runner.state, "set_screen", lambda *a: None)

    runner._show_screen({"chat_id": 1, "name": "stats", "msg_id": 42},
                        http=None)
    assert calls == [("edit", 42)], "кнопка правит на месте, без новой отправки"


def test_heavy_buttons_return_plan_only(monkeypatch):
    """«Анкета», «Письмо» и /mail не делают тяжёлой работы в handle().

    Сеть к ATS шла до гашения часиков: 98% нажатий «Анкеты» ждали до шести
    секунд, и весь цикл стоял. Теперь handle() возвращает план с task, а
    работу делает поток доставки.
    """
    from jobhunter.bot import handlers

    def _boom(*a, **kw):
        raise AssertionError("тяжёлая работа в handle()")

    monkeypatch.setattr(handlers, "_manual_form", _boom)
    monkeypatch.setattr(handlers, "_manual_letter", _boom)

    for action in ("form", "letter"):
        acts = handlers._callback(
            {"id": "cb1", "data": "m:5:%s" % action,
             "message": {"message_id": 7}}, chat_id=1)
        kinds = [a["do"] for a in acts]
        assert "task" in kinds and "answer" in kinds, (action, kinds)

    acts = handlers._message({"text": "/mail"}, chat_id=1)
    assert any(a["do"] == "task" for a in acts), "IMAP обязан уйти в задачу"


def test_task_action_goes_to_queue(monkeypatch):
    """runner._apply кладёт task в очередь в БД, а не исполняет сам.

    Очередь именно в БД: offset апдейта подтверждён до исполнения, и задача
    в памяти процесса пропадала бы при рестарте вместе с нажатием владельца.
    """
    from jobhunter.bot import runner, state

    class _Api:
        class TokenRevoked(Exception):
            pass

        @staticmethod
        def answer_callback_query(*a, **kw):
            pass

    monkeypatch.setattr(runner, "api", _Api)
    while state.task_pop() is not None:
        pass
    runner._apply([{"do": "answer", "cb_id": "x"},
                   {"do": "task", "chat_id": 1, "task": "mail"}], http=None)
    act = state.task_pop()
    assert act is not None and act["task"] == "mail" and act["chat_id"] == 1
    assert state.task_pop() is None, "задача обязана забираться ровно один раз"


def test_task_queue_survives_restart_shape():
    """task_push/task_pop — FIFO и переживают «рестарт» (новая сессия БД)."""
    from jobhunter.bot import state

    while state.task_pop() is not None:
        pass
    state.task_push({"do": "task", "chat_id": 7, "task": "form", "app_id": 42})
    state.task_push({"do": "task", "chat_id": 7, "task": "mail"})
    first = state.task_pop()
    assert first["task"] == "form" and first["app_id"] == 42
    assert state.task_pop()["task"] == "mail"
    assert state.task_pop() is None


def test_remote_protocol_error_retries_without_sleep(monkeypatch):
    """Обрыв keep-alive повторяется мгновенно, без секунд сна."""
    import httpx

    from jobhunter.bot import api

    calls = {"n": 0}
    slept = []

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"ok": True}}

    class _Http:
        def post(self, url, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected")
            return _Resp()

    monkeypatch.setattr(api.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(api, "get_settings",
                        lambda: type("S", (), {"telegram_bot_token": "t:x"})())
    out = api.call("getMe", _http=_Http())
    assert out == {"ok": True}
    assert slept == [], "обрыв keep-alive не должен приводить ко сну"


def test_remote_protocol_error_does_not_consume_transport_budget(monkeypatch):
    """Два обрыва keep-alive не съедают три сетевые попытки."""
    import httpx

    from jobhunter.bot import api

    calls = {"n": 0}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"result": "ok"}

    class _Http:
        def post(self, url, json=None, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise httpx.RemoteProtocolError("Server disconnected")
            if calls["n"] <= 4:
                raise httpx.ConnectError("temporary")
            return _Resp()

    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(api, "get_settings",
                        lambda: type("S", (), {"telegram_bot_token": "t:x"})())
    assert api.call("getMe", _http=_Http()) == "ok"
    assert calls["n"] == 5


def test_lock_reason_translates_jargon():
    """Находки ревью: ручной режим и kill-switch не должны превращаться в
    «антиспам-пауза … снимется сама» — ручной режим сам не снимается."""
    from jobhunter.bot.screens import _lock_reason

    for verdict in ("кампания переведена в ручной режим (2× PeerFlood)",
                    "второй PeerFlood — постоянный ручной режим"):
        out = _lock_reason(verdict)
        assert "ручной режим" in out and "снимется сама" not in out, out
    out = _lock_reason("kill-switch: STOP_SENDING.flag")
    assert "kill-switch" not in out and "выключена тобой" in out, out
    out = _lock_reason("лок до 2026-08-31 17:48:00 (peerflood)")
    assert "антиспам-пауза" in out and "31.08" in out, out


def test_plural_cards():
    from jobhunter.bot.screens import _plural

    got = {n: _plural(n, "карточку", "карточки", "карточек")
           for n in (1, 2, 4, 5, 11, 14, 21, 22, 25, 111)}
    assert got[1] == got[21] == "карточку"
    assert got[2] == got[4] == got[22] == "карточки"
    assert got[5] == got[11] == got[14] == got[25] == got[111] == "карточек"


def test_approve_top_skips_unpassed_gate(db):
    """Заявка без gate_passed не одобряется и не валит пачку исключением.

    Живой случай #1846: follow-up со сбитым гейтом и score=100 стоял первым,
    TransitionError откатывал всю пачку — кнопка «Одобрить топ-10» пять дней
    не одобряла ничего.
    """
    import uuid as _uuid

    from jobhunter.bot.handlers import _approve_top
    from jobhunter.models import Application, Job, Status

    with db.session_scope() as sess:
        ids = {}
        for tag, (passed, score) in {"bad": (False, 100), "good": (True, 50)}.items():
            job = Job(external_uuid=str(_uuid.uuid4()), source="test",
                      title="t-" + tag)
            sess.add(job)
            sess.flush()
            app = Application(job_id=job.id,
                              status=Status.PENDING_APPROVAL.value,
                              gate_passed=passed, score=score)
            sess.add(app)
            sess.flush()
            ids[tag] = app.id

    n = _approve_top(10)
    assert n >= 1
    with db.session_scope() as sess:
        assert sess.get(Application, ids["bad"]).status == \
            Status.PENDING_APPROVAL.value, "непройденный гейт нельзя одобрять"
        assert sess.get(Application, ids["good"]).status == \
            Status.APPROVED.value, "живая заявка обязана одобриться"
