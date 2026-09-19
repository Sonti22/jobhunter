"""Напоминания о карточках владельцу.

Проверка 19.09: из 25 карточек 12 истекли без ответа — бот получал ответ рекрутёра, а
разговор умирал в ожидании решения. Карточка приходила один раз и тонула в ленте.
"""
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "cards.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:test")
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "42")
    monkeypatch.setenv("OWNER_CHANNEL", "bot")
    monkeypatch.setenv("OWNER_TZ", "Europe/Moscow")
    import jobhunter.db as dbmod
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    dbmod._engine = None
    dbmod._Session = None
    get_settings.cache_clear()


def _card(db, company, created, expires):
    from jobhunter.models import Application, Job, OwnerRequest
    with db.session_scope() as sess:
        job = Job(external_uuid=str(random.random()), source="hn", title="Backend Engineer",
                  company_name=company, contact_kind="email", contact_url="hr@x.io")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status="NEEDS_HUMAN", score=80)
        sess.add(app)
        sess.flush()
        req = OwnerRequest(application_id=app.id, kind="needs_human", question="?",
                           created_at=created, expires_at=expires)
        sess.add(req)
        sess.flush()
        return req.id


def _texts(db):
    from jobhunter.models import BotOutbox
    with db.session_scope() as sess:
        return [o.text for o in sess.scalars(select(BotOutbox).order_by(BotOutbox.id))]


def test_reminders_fire_once_per_stage_and_digest_once_a_day(db):
    from jobhunter import owner
    noon = datetime(2026, 9, 19, 9, 0)                       # 12:00 МСК
    _card(db, "Astoria AI", noon - timedelta(hours=70), noon + timedelta(hours=2))    # скоро истечёт
    _card(db, "Zapier", noon - timedelta(hours=10), noon + timedelta(hours=38))       # висит 10 часов
    _card(db, "Fresh Co", noon - timedelta(hours=1), noon + timedelta(hours=47))      # только пришла
    assert owner.remind_pending(now=noon) == 2
    texts = _texts(db)
    assert any("Истекает через 2 ч" in t and "Astoria AI" in t for t in texts)
    assert any("Zapier" in t and "уже 10 ч" in t for t in texts)
    assert not any("Fresh Co" in t and "🔔" in t for t in texts)
    digest = [t for t in texts if t.startswith("📬")]
    assert len(digest) == 1 and "Ждут твоего ответа: 3" in digest[0] and "Fresh Co" in digest[0]
    # повторный проход через полчаса ничего не дублирует
    owner.remind_pending(now=noon + timedelta(minutes=30))
    assert len(_texts(db)) == len(texts)


def test_no_reminders_at_night_and_none_for_decided_cards(db):
    from jobhunter import owner
    from jobhunter.models import OwnerRequest
    night = datetime(2026, 9, 19, 0, 30)                     # 03:30 МСК
    rid = _card(db, "Astoria AI", night - timedelta(hours=70), night + timedelta(hours=2))
    assert owner.remind_pending(now=night) == 0 and _texts(db) == []
    with db.session_scope() as sess:
        sess.get(OwnerRequest, rid).decision = "send"
    assert owner.remind_pending(now=night + timedelta(hours=9)) == 0 and _texts(db) == []


def test_human_card_lives_two_days(db):
    from jobhunter import owner
    from jobhunter.models import Application, Job
    with db.session_scope() as sess:
        job = Job(external_uuid="tg:x/1", source="tg:x", title="DevOps", contact_kind="user_handle",
                  contact_handle="anna", contact_url="https://t.me/anna")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status="REPLIED", score=80)
        sess.add(app)
        sess.flush()
        req = owner.create_human_request(sess, app, job, "Пришлите резюме", "не понял")
        ttl = req.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
        assert ttl > timedelta(hours=47)


def test_send_decision_in_telegram_manual_mode_hands_the_text_to_the_owner(db):
    """Проверка 19.09: владелец дважды нажал «отправить», бот ответил «отложено» и крутил
    повтор каждые 15 минут. Рекрутёр ничего не получил."""
    import asyncio

    from jobhunter import decisions
    from jobhunter.models import Application, BotOutbox, Job, OwnerRequest, utcnow
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = True
        job = Job(external_uuid="tg:x/9", source="tg:x", title="Teamlead DevOps", company_name="Acme",
                  contact_kind="user_handle", contact_handle="recruiter_anna",
                  contact_url="https://t.me/recruiter_anna")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status="NEEDS_HUMAN", score=80, sent_at=utcnow())
        sess.add(app)
        sess.flush()
        req = OwnerRequest(application_id=app.id, kind="needs_human", decision="say",
                           decision_arg="Здравствуйте! Резюме во вложении.", answered_at=utcnow())
        sess.add(req)
        sess.flush()
        req_id = req.id
    assert asyncio.run(decisions.apply_one(None, req_id)) == "manual"
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, req_id)
        assert req.applied_at is not None and req.next_try_at is None      # повтор больше не крутится
        out = [o for o in sess.scalars(select(BotOutbox)) if o.kind == "manual_reply"]
        assert len(out) == 1 and "@recruiter_anna" in out[0].text and "Резюме во вложении" in out[0].text
        url = out[0].markup_json["inline_keyboard"][0][0]["url"]
        assert url.startswith("https://t.me/recruiter_anna?text=")


def test_reminders_do_not_repeat_after_delivery(db):
    """19.09 вживую: сводка пришла трижды за час, напоминание про Zapier — дважды. Ключ dedup у
    push() защищает только недоставленные копии; доставленное сообщение ключ освобождало."""
    from jobhunter import owner
    from jobhunter.models import BotOutbox, utcnow
    noon = datetime(2026, 9, 19, 9, 0)
    _card(db, "Zapier", noon - timedelta(hours=10), noon + timedelta(hours=38))
    assert owner.remind_pending(now=noon) == 1
    with db.session_scope() as sess:                      # бот всё доставил
        for o in sess.scalars(select(BotOutbox)):
            o.sent_at = utcnow()
    before = len(_texts(db))
    assert owner.remind_pending(now=noon + timedelta(minutes=30)) == 0
    assert owner.remind_pending(now=noon + timedelta(hours=1)) == 0
    assert len(_texts(db)) == before
    # на следующий день сводка приходит снова — ключ у неё по дате
    owner.remind_pending(now=noon + timedelta(hours=24))
    assert len([t for t in _texts(db) if t.startswith("📬")]) == 2
