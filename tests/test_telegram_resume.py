"""Ручной режим Telegram раньше было нечем снять.

on_peer_flood() переводит кампанию в manual_only и пишет владельцу «до твоего решения»,
но ни кнопки, ни команды для этого решения в коде не было — временный лок (48ч) истекал
сам, а ручной самозапрет оставался висеть вечно (проверка 22.09: лок истёк 20.09,
peerflood_total=6, manual_only оставался True два дня спустя).
"""
import os

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "tgresume.db")
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


def test_two_peerfloods_lock_the_campaign_and_only_resume_can_lift_it(db):
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.on_peer_flood(sess, "strike 1")
        policy.on_peer_flood(sess, "strike 2")
        assert policy.get_state(sess).manual_only is True
    with db.session_scope() as sess:
        # истёкший временной лок сам по себе ручной режим не снимает
        from datetime import datetime, timedelta
        policy.get_lock(sess).locked_until = datetime.now() - timedelta(days=2)
    with db.session_scope() as sess:
        assert policy.get_state(sess).manual_only is True
        assert policy.resume_manual_only(sess) is True
        assert policy.get_state(sess).manual_only is False
    with db.session_scope() as sess:
        # темп не восстанавливается автоматически — только чистыми днями
        assert policy.get_state(sess).quota_ceiling < 30
        assert policy.get_state(sess).peerflood_total == 2          # история сохранена
        assert policy.resume_manual_only(sess) is False              # нечего снимать повторно


def test_a_third_peerflood_right_after_resume_locks_it_again(db):
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.on_peer_flood(sess, "strike 1")
        policy.on_peer_flood(sess, "strike 2")
        policy.resume_manual_only(sess)
    with db.session_scope() as sess:
        policy.on_peer_flood(sess, "strike 3")
        assert policy.get_state(sess).manual_only is True            # без обхода — снова ручной


def test_dashboard_shows_the_resume_button_only_while_manual(db):
    from jobhunter.bot import screens
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = False
    text, kb = screens.main()
    flat = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]
    assert "q:tgresume" not in flat
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = True
    text, kb = screens.main()
    assert "@SpamBot" in text
    flat = [b.get("callback_data") for row in kb["inline_keyboard"] for b in row]
    assert flat[0] == "q:tgresume"                                   # первая кнопка — самое важное действие


def test_button_press_resumes_and_answering_it_twice_is_safe(db):
    from jobhunter.bot import handlers
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = True
    acts = handlers._callback({"id": "cb1", "data": "q:tgresume",
                               "message": {"message_id": 9}}, chat_id=1)
    kinds = {a["do"]: a for a in acts}
    assert "screen" in kinds and kinds["screen"]["name"] == "main"
    assert kinds["answer"]["text"] == "Telegram возобновлён"
    with db.session_scope() as sess:
        assert policy.get_state(sess).manual_only is False
    acts = handlers._callback({"id": "cb2", "data": "q:tgresume",
                               "message": {"message_id": 9}}, chat_id=1)
    assert {a["do"]: a for a in acts}["answer"]["text"] == "уже не в ручном режиме"
