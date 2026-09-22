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


def test_gap_seconds_never_shorter_than_the_pause():
    from jobhunter.outreach import policy
    rng = random.Random(1)
    samples = [policy.gap_seconds(rng) for _ in range(500)]
    assert min(samples) >= policy.COLD_GAP_MINUTES * 60
    assert max(samples) <= 2700.0 + 30.0
    assert len({round(x) for x in samples}) > 50, "ровный интервал — машинный признак"
