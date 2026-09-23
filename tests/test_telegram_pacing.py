"""Темп холодных Telegram: пауза вместо дневного потолка.

До 23.09 темп держал дневной счётчик (6/день), а пауза между сообщениями
жила в секундах: несколько сообщений уходили за пару минут, и сама плотность
пачки — независимо от текста — уже похожа на спам-паттерн (6 PeerFlood к
22.09). Решение владельца 23.09: дневного потолка нет, новому адресату
пишем не чаще раза в полчаса, а тем, кто уже ответил, — когда угодно.
"""
import os
import random
from datetime import timedelta

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "pacing.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "1"
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


@pytest.fixture(autouse=True)
def clean(db):
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        st = policy.get_state(sess)
        st.manual_only = False
        st.last_cold_sent_at = None
        policy.get_quota(sess).sent_count = 0
        policy.get_lock(sess).locked_until = None
    yield


def test_no_daily_cap_left_only_the_pause(db):
    """Двадцать отправок подряд не упираются в потолок — только в паузу."""
    from jobhunter.models import utcnow
    from jobhunter.outreach import policy
    for i in range(20):
        with db.session_scope() as sess:
            assert policy.can_send_cold(sess).allowed, "отказ на %d-й отправке" % i
            policy.register_sent(sess, cold=True)
            # адресат получил сообщение — отматываем паузу назад, как будто
            # прошли положенные полчаса
            policy.get_state(sess).last_cold_sent_at = (
                utcnow() - timedelta(minutes=policy.COLD_GAP_MINUTES + 1))
    with db.session_scope() as sess:
        assert policy.get_quota(sess).sent_count == 20


def test_second_message_waits_half_an_hour(db):
    from jobhunter.models import utcnow
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.register_sent(sess, cold=True)
    with db.session_scope() as sess:
        v = policy.can_send_cold(sess)
        assert not v.allowed
        assert policy.COLD_GAP_REASON in v.reason
        assert 0 < v.wait_seconds <= policy.COLD_GAP_MINUTES * 60
    with db.session_scope() as sess:       # 29 минут — всё ещё рано
        policy.get_state(sess).last_cold_sent_at = utcnow() - timedelta(minutes=29)
    with db.session_scope() as sess:
        assert not policy.can_send_cold(sess).allowed
    with db.session_scope() as sess:       # 31 минута — можно
        policy.get_state(sess).last_cold_sent_at = utcnow() - timedelta(minutes=31)
    with db.session_scope() as sess:
        assert policy.can_send_cold(sess).allowed


def test_pause_survives_restart_because_it_lives_in_the_db(db):
    """Метка в базе, а не в памяти процесса: планировщик зовёт отправку заново."""
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.register_sent(sess, cold=True)
    with db.session_scope() as sess:
        assert policy.get_state(sess).last_cold_sent_at is not None
        assert policy.cold_gap_left(sess) > 0


def test_warm_replies_ignore_the_cold_pause(db):
    """Тому, кто уже ответил, пишем когда угодно — у тёплых свой бакет."""
    from jobhunter.convo import send as convo_send
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.register_sent(sess, cold=True)          # холодные закрыты паузой
        assert not policy.can_send_cold(sess).allowed
        ok, reason = convo_send.can_reply(sess)
        assert ok, reason


def test_pacing_stop_mid_batch_hands_the_rest_to_the_scheduler(db, monkeypatch):
    """Второй адресат в той же партии упирается в паузу: прогон отдаёт остаток
    планировщику (MORE_TO_SEND), а не спит полчаса в однопоточной tg-очереди."""
    import asyncio

    from jobhunter.outreach import policy, sender
    answers = iter(["ok", "stop:%s: ещё 30 мин" % policy.COLD_GAP_REASON])

    async def fake_send(client, item, rng, dry):
        return next(answers)

    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(sender, "send_one", fake_send)
    monkeypatch.setattr(sender.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(sender, "reclaim_stale_sending", lambda: 0)
    monkeypatch.setattr(sender, "pick_batch", lambda limit: [
        {"app_id": i, "handle": "hr_%d" % i, "score": 70.0, "title": "X"} for i in (1, 2, 3)])
    monkeypatch.setattr(sender.policy, "session_plan", lambda n, rng=None: [n])
    assert asyncio.run(sender.run(5, dry=True)) == sender.MORE_TO_SEND
    assert slept == [], "между сообщениями прогон не спит — паузу держит планировщик"


def test_run_does_not_close_the_day(db, monkeypatch):
    """Итог дня — один раз в сутки (step_daily_summary). Второй вызов из
    отправщика удваивал счёт «чистых дней» на пульте."""
    import asyncio

    from jobhunter.outreach import sender

    async def fake_send(client, item, rng, dry):
        return "ok"

    monkeypatch.setattr(sender, "send_one", fake_send)
    monkeypatch.setattr(sender, "pick_batch", lambda limit: [
        {"app_id": 1, "handle": "hr_1", "score": 70.0, "title": "X"}])
    monkeypatch.setattr(sender.policy, "close_day",
                        lambda sess: pytest.fail("close_day из отправщика"))
    assert asyncio.run(sender.run(5, dry=True)) == 0


def test_rearm_is_persisted_so_a_restart_keeps_the_chain(db, monkeypatch):
    """APScheduler держит задание в памяти; без записи в базу перезапуск посреди
    паузы обрывал бы цепочку отправок до следующего крона."""
    from jobhunter import autopilot
    from jobhunter.models import RuntimeState
    jobs = []

    class FakeSched:
        def add_job(self, fn, trigger, **kw):
            jobs.append(kw)

    monkeypatch.setitem(autopilot._SCHED, "sched", FakeSched())
    when = autopilot._rearm_telegram(1800)
    assert jobs and jobs[0]["id"] == "tg_more"
    with db.session_scope() as sess:
        row = sess.get(RuntimeState, "sender:telegram")
        assert row.status == "partial" and row.next_run_at is not None
    assert when is not None


def test_step_during_pause_rearms_quietly(db, monkeypatch, tmp_path):
    """Пауза — штатный ход: без «квота исчерпана» владельцу, с переназначением
    на её конец."""
    from jobhunter import autopilot
    from jobhunter.models import BotOutbox
    from jobhunter.outreach import policy
    _telegram_env(monkeypatch, tmp_path)
    with db.session_scope() as sess:
        policy.register_sent(sess, cold=True)
        before = sess.query(BotOutbox).count()
    rearmed = []
    monkeypatch.setattr(autopilot, "_rearm_telegram", lambda s: rearmed.append(s))
    res = autopilot.step_send_telegram()
    assert policy.COLD_GAP_REASON in res["blocked"]
    assert rearmed and 0 < rearmed[0] <= policy.COLD_GAP_MINUTES * 60 + 30
    with db.session_scope() as sess:
        assert sess.query(BotOutbox).count() == before, "владельцу о паузе не пишем"


def _telegram_env(monkeypatch, tmp_path):
    from jobhunter.config import get_settings
    session_file = tmp_path / "tg.session"
    session_file.write_text("x")
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(session_file))
    get_settings.cache_clear()


@pytest.mark.parametrize("error, rearmed", [
    (ConnectionError("Connection to Telegram failed 5 time(s)"), True),
    (TimeoutError(), True),
    (ValueError("ошибка в коде"), False),
])
def test_network_failure_keeps_the_chain_alive(db, monkeypatch, tmp_path, error, rearmed):
    """23.09: сеть пропала 10:20-12:08, tg1 в 11:15 упал на подключении, и
    Telegram молчал бы до tg2 в 16:40. При обрыве сети цепочка пробует снова;
    ошибку кода повторять бессмысленно — её видно в журнале и на пульте."""
    from jobhunter import autopilot
    from jobhunter.outreach import sender
    _telegram_env(monkeypatch, tmp_path)

    async def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(sender, "run", broken)
    calls = []
    monkeypatch.setattr(autopilot, "_rearm_telegram", lambda s: calls.append(s))
    with pytest.raises(type(error)):
        autopilot.step_send_telegram()
    assert calls == ([autopilot.NETWORK_RETRY_S] if rearmed else [])


def test_pult_does_not_show_the_pause_as_a_stop(db):
    from jobhunter.bot import screens
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.register_sent(sess, cold=True)
    text, _ = screens.main()
    assert "следующее через" in text
    assert "⏸" not in text


def test_gap_seconds_never_shorter_than_the_pause():
    from jobhunter.outreach import policy
    rng = random.Random(1)
    samples = [policy.gap_seconds(rng) for _ in range(500)]
    assert min(samples) >= policy.COLD_GAP_MINUTES * 60
    assert max(samples) <= 2700.0 + 30.0
    assert len({round(x) for x in samples}) > 50, "ровный интервал — машинный признак"
