"""Статус аккаунта у @SpamBot вместо попыток вслепую.

23.09: после ручного «▶️» первое же холодное сообщение получило седьмой PeerFlood, хотя
пять дней до этого не уходило ничего — ограничение висело на самом аккаунте. Каждая
попытка вслепую продлевает его; SpamBot называет статус и срок без попытки.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

FREE_EN = "Good news, no limits are currently applied to your account. You’re free as a bird!"
FREE_RU = ("Отличные новости: на Ваш аккаунт сейчас не наложено никаких ограничений. "
           "Вы свободны как птица!")
LIMITED_EN = ("Unfortunately, some actions can trigger a harsh response from our anti-spam "
              "systems. Your account is now limited until 30 Sep 2036, 12:45 UTC. While the "
              "account is limited, you will not be able to send messages to people who do not "
              "have your number in their phone contacts.")
LIMITED_RU = ("К сожалению, иногда наша антиспам-система излишне сурово реагирует на некоторые "
              "действия. Ваш аккаунт ограничен до 30 сентября 2036, 12:45 UTC. Пока действуют "
              "ограничения, Вы не сможете писать тем, у кого нет Вашего номера в контактах.")
LIMITED_NO_DATE = ("Unfortunately, your account is limited. You will not be able to send "
                   "messages to people who do not have your number in their phone contacts.")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "spambot.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "1"
    os.environ["OWNER_CHANNEL"] = "bot"
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean(db):
    from jobhunter.models import AccountHealth, BotOutbox, SendLog
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        sess.query(AccountHealth).delete()
        sess.query(BotOutbox).delete()
        sess.query(SendLog).delete()
        policy.get_state(sess).manual_only = True
        lk = policy.get_lock(sess)
        lk.locked_until, lk.reason = None, ""
    yield


def _texts(db):
    from jobhunter.models import BotOutbox
    with db.session_scope() as sess:
        return [o.text for o in sess.query(BotOutbox).all()]


@pytest.mark.parametrize("text, status, until", [
    (FREE_EN, "free", None),
    (FREE_RU, "free", None),
    (LIMITED_EN, "limited", datetime(2036, 9, 30, 12, 45)),
    (LIMITED_RU, "limited", datetime(2036, 9, 30, 12, 45)),
    (LIMITED_NO_DATE, "limited", None),
    ("Your account is now limited until 1 Jan 2020, 10:00 UTC.", "limited", None),
    ("Hello! Choose an option below.", "unknown", None),
    ("", "unknown", None),
])
def test_parse_spambot_answers(text, status, until):
    from jobhunter.outreach import spamcheck
    v = spamcheck.parse(text)
    assert (v.status, v.until) == (status, until)


def test_free_answer_resumes_telegram_by_itself(db):
    from jobhunter.models import AccountHealth
    from jobhunter.outreach import policy, spamcheck
    assert spamcheck.apply(FREE_EN).status == "free"
    with db.session_scope() as sess:
        assert policy.get_state(sess).manual_only is False
        row = sess.query(AccountHealth).one()
        assert row.spambot_verdict == "free" and "free as a bird" in row.spambot_raw
    assert any("@SpamBot подтвердил" in t for t in _texts(db))


def test_limited_with_date_pauses_until_that_date_without_trying(db):
    from jobhunter.outreach import policy, spamcheck
    spamcheck.apply(LIMITED_RU)
    spamcheck.apply(LIMITED_RU)                      # повторная проверка не дублирует сообщение
    with db.session_scope() as sess:
        assert policy.get_state(sess).manual_only is True
        lk = policy.get_lock(sess)
        assert lk.locked_until == datetime(2036, 9, 30, 12, 45)
        assert lk.reason.startswith(spamcheck.LOCK_PREFIX)
        assert not policy.can_send_cold(sess).allowed
    notes = [t for t in _texts(db) if "ограничен до" in t]
    assert len(notes) == 1 and "30.09 15:45 МСК" in notes[0]


def test_limited_without_date_keeps_manual_and_points_to_appeal(db):
    from jobhunter.outreach import policy, spamcheck
    spamcheck.apply(LIMITED_NO_DATE)
    with db.session_scope() as sess:
        assert policy.get_state(sess).manual_only is True
        assert policy.get_lock(sess).locked_until is None
    notes = _texts(db)
    assert len(notes) == 1 and "без срока" in notes[0] and "This is a mistake" in notes[0]


def test_run_asks_spambot_through_the_session(db, monkeypatch):
    import telethon

    from jobhunter.outreach import spamcheck
    sent = []

    class Conv:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send_message(self, text):
            sent.append(text)

        async def get_response(self):
            return type("R", (), {"raw_text": LIMITED_EN})()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.closed = False

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return type("Me", (), {"restricted": False, "restriction_reason": ""})()

        def conversation(self, peer, timeout=None):
            assert peer == spamcheck.SPAMBOT
            return Conv()

        async def disconnect(self):
            self.closed = True

    monkeypatch.setattr(telethon, "TelegramClient", FakeClient)
    v = asyncio.run(spamcheck.run())
    assert sent == ["/start"]
    assert v.status == "limited" and v.until == datetime(2036, 9, 30, 12, 45)


def _telegram_env(monkeypatch, tmp_path):
    from jobhunter.config import get_settings
    session_file = tmp_path / "tg.session"
    session_file.write_text("x")
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(session_file))
    get_settings.cache_clear()


def test_step_asks_only_while_manual_and_not_before_the_named_date(db, monkeypatch, tmp_path):
    from jobhunter import autopilot
    from jobhunter.outreach import policy, spamcheck
    _telegram_env(monkeypatch, tmp_path)
    calls = []

    async def fake_run():
        calls.append(1)
        return spamcheck.SpamVerdict("limited")

    monkeypatch.setattr(spamcheck, "run", fake_run)
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = False
    assert "skipped" in autopilot.step_spambot() and calls == []
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = True
        lk = policy.get_lock(sess)
        lk.locked_until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3)
        lk.reason = spamcheck.LOCK_PREFIX + " ограничение"
    assert "skipped" in autopilot.step_spambot() and calls == []
    with db.session_scope() as sess:
        policy.get_lock(sess).reason = "peerflood: 48 ч"   # лок PeerFlood спрашивать не мешает
    assert autopilot.step_spambot()["status"] == "limited" and calls == [1]


def test_pult_shows_what_spambot_said(db):
    from jobhunter.bot import screens
    from jobhunter.outreach import spamcheck
    spamcheck.apply(LIMITED_EN)
    text, _ = screens.main()
    assert "🧊 @SpamBot" in text and "аккаунт ограничен до 30.09 15:45" in text


def test_replies_to_those_who_wrote_first_are_allowed_while_limited(db):
    """SpamBot 23.09: «Если незнакомый пользователь напишет Вам первым, Вы сможете ему
    ответить». Ручной режим глушил и такие ответы, хотя ограничение — только на холодные."""
    from jobhunter.convo import route
    from jobhunter.convo.send import can_reply
    from jobhunter.outreach import spamcheck
    with db.session_scope() as sess:
        assert not can_reply(sess, route.TELEGRAM)[0]        # SpamBot ещё не спрашивали
    spamcheck.apply(LIMITED_RU)
    with db.session_scope() as sess:
        assert can_reply(sess, route.TELEGRAM)[0]
        from jobhunter.outreach import policy
        assert not policy.can_send_cold(sess).allowed        # холодные по-прежнему стоят


def test_a_peerflood_after_the_check_sends_replies_back_to_the_owner(db):
    from jobhunter.convo import route
    from jobhunter.convo.send import can_reply
    from jobhunter.models import SendLog
    from jobhunter.outreach import spamcheck
    spamcheck.apply(LIMITED_RU)
    with db.session_scope() as sess:
        sess.add(SendLog(result="peerflood", error_class="PeerFloodError", peer_id="hr"))
    with db.session_scope() as sess:
        assert not can_reply(sess, route.TELEGRAM)[0]


def test_unrecognised_answer_does_not_open_replies(db):
    from jobhunter.convo import route
    from jobhunter.convo.send import can_reply
    from jobhunter.outreach import spamcheck
    spamcheck.apply("Hello! Choose an option below.")
    with db.session_scope() as sess:
        assert not can_reply(sess, route.TELEGRAM)[0]


def test_pult_telegram_line_says_stopped_not_can_send_now(db):
    """23.09 пульт писал «Telegram 0 за сегодня · можно сейчас» при ручном режиме."""
    from jobhunter.bot import screens
    from jobhunter.outreach import spamcheck
    spamcheck.apply(LIMITED_EN)
    text, _ = screens.main()
    line = next(ln for ln in text.splitlines() if ln.startswith("Telegram"))
    assert "можно сейчас" not in line and "стоит до 30.09 15:45" in line
