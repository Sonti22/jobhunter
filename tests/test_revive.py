"""Оживление застрявших диалогов и повтор классификации.

Семь диалогов из тринадцати ответивших лежали мёртвыми: рекрутёр написал,
карточка владельцу истекла без нажатия, и защёлку NEEDS_HUMAN снимать было
некому. Плюс сбой LLM записывал «мнения нет» навсегда.

Эти тесты стерегут оба механизма и — главное — их границы: revive не имеет
права трогать деньги, оффер и предложенное время, а expire_stale не имеет
права менять статус.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "revive.db")
    os.environ["LLM_ENABLED"] = "false"
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "5875908057"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean(db):
    from sqlalchemy import delete

    from jobhunter.models import Application, BotOutbox, Job, Message, OwnerRequest, PendingReply
    with db.session_scope() as sess:
        for m in (PendingReply, Message, OwnerRequest, Application, Job,
                  BotOutbox):
            sess.execute(delete(m))
    yield


def _stuck(db, incoming, *, hours_ago=30, decision="expired", status=None):
    """Заявка в NEEDS_HUMAN с истёкшей карточкой — как в бою."""
    from jobhunter.models import (
        Application,
        ContactKind,
        Job,
        Message,
        OwnerRequest,
        OwnerRequestKind,
        Status,
    )
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:t",
                  title="Python Developer", company_name="Acme",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr_acme",
                  description_raw="Python, FastAPI, PostgreSQL")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=80, gate_passed=True,
                          status=(status or Status.NEEDS_HUMAN).value,
                          needs_human_reason="интент unknown")
        sess.add(app)
        sess.flush()
        sess.add(Message(application_id=app.id, direction="in",
                         body=incoming, received_at=old,
                         classifier_label="unknown"))
        sess.add(OwnerRequest(application_id=app.id,
                              kind=OwnerRequestKind.NEEDS_HUMAN.value,
                              question="✋ нужен ответ", decision=decision,
                              created_at=old))
        sess.flush()
        return app.id


def test_stuck_thread_is_revived(db):
    """«Присылайте резюме» с истёкшей карточкой → защёлка снята, ответ в очереди."""
    from sqlalchemy import select

    from jobhunter.convo.revive import revive_one
    from jobhunter.models import Application, PendingReply, Status

    app_id = _stuck(db, "Добрый вечер! Присылайте резюме")
    res = revive_one(app_id)
    assert res.startswith("оживлено"), res
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == Status.IN_DIALOGUE.value
        assert app.needs_human_reason == ""
        assert app.revive_attempts == 1
        pending = sess.scalars(select(PendingReply).where(
            PendingReply.application_id == app_id)).all()
        assert len(pending) == 1, "ответ обязан уйти через общую очередь"


def test_money_is_never_revived(db):
    """Деньги остаются владельцу — решение владельца, не настройка."""
    from jobhunter.convo.revive import revive_one
    from jobhunter.models import Application, Status

    app_id = _stuck(db, "Какие у вас зарплатные ожидания?")
    res = revive_one(app_id)
    assert "остаётся владельцу" in res, res
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.NEEDS_HUMAN.value


def test_offer_and_slots_stay_with_owner(db):
    from jobhunter.convo.revive import revive_one
    from jobhunter.models import Application, Status

    for text in ("Готовы сделать вам оффер!",
                 "Давайте в четверг в 15:00"):
        app_id = _stuck(db, text)
        revive_one(app_id)
        with db.session_scope() as sess:
            assert sess.get(Application, app_id).status == \
                Status.NEEDS_HUMAN.value, text


def test_owner_decision_is_respected(db):
    """Владелец нажал кнопку — оживление не вмешивается."""
    from jobhunter.convo.revive import stale

    app_id = _stuck(db, "Присылайте резюме", decision="send")
    assert app_id not in stale()


def test_fresh_card_is_not_touched(db):
    """Карточка младше порога — у владельца ещё есть время."""
    from jobhunter.convo.revive import stale

    app_id = _stuck(db, "Присылайте резюме", hours_ago=2, decision="")
    assert app_id not in stale()


def test_attempts_are_capped(db):
    """Две попытки — и заявка остаётся владельцу навсегда."""
    from jobhunter.convo.revive import stale
    from jobhunter.models import Application

    app_id = _stuck(db, "Присылайте резюме")
    with db.session_scope() as sess:
        sess.get(Application, app_id).revive_attempts = 2
    assert app_id not in stale()


def test_expire_stale_never_changes_status(db):
    """Истечение карточки гасит кнопки, но не трогает заявку.

    Единственное место, снимающее защёлку, — revive. Если этот тест
    краснеет, значит появился второй путь, и автоматика может перебить
    владельца, пока он думает.
    """
    from jobhunter.models import Application, Status
    from jobhunter.owner import expire_stale

    app_id = _stuck(db, "Присылайте резюме", decision="")
    with db.session_scope() as sess:
        from sqlalchemy import select

        from jobhunter.models import OwnerRequest
        req = sess.scalars(select(OwnerRequest).where(
            OwnerRequest.application_id == app_id)).first()
        req.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) \
            - timedelta(hours=1)
    expire_stale()
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.NEEDS_HUMAN.value


def test_llm_failure_schedules_retry(db):
    """Молчание модели — «спросить позже», а не «мнения нет навсегда»."""
    from sqlalchemy import select

    from jobhunter.convo.engine import _remember_llm_verdict
    from jobhunter.models import Message

    app_id = _stuck(db, "Непонятное сообщение")
    _remember_llm_verdict(app_id, None, error="HTTP 429")
    with db.session_scope() as sess:
        msg = sess.scalars(select(Message).where(
            Message.application_id == app_id)).first()
        assert msg.llm_attempts == 1
        assert msg.llm_next_try_at is not None, "повтор обязан быть запланирован"
        assert "429" in msg.llm_error


def test_retry_backoff_grows_and_stops(db):
    from sqlalchemy import select

    from jobhunter.convo.engine import LLM_MAX_ATTEMPTS, _remember_llm_verdict
    from jobhunter.models import Message

    app_id = _stuck(db, "Непонятное сообщение")
    prev = None
    for _ in range(LLM_MAX_ATTEMPTS):
        _remember_llm_verdict(app_id, None, error="429")
        with db.session_scope() as sess:
            msg = sess.scalars(select(Message).where(
                Message.application_id == app_id)).first()
            if msg.llm_next_try_at and prev:
                assert msg.llm_next_try_at > prev, "пауза обязана расти"
            prev = msg.llm_next_try_at
    with db.session_scope() as sess:
        msg = sess.scalars(select(Message).where(
            Message.application_id == app_id)).first()
        assert msg.llm_attempts == LLM_MAX_ATTEMPTS
        assert msg.llm_next_try_at is None, "после лимита повторов не ждём"


def test_confirmed_rejection_is_closed(db):
    """Отказ не должен висеть в «ждут решения» — закрываем."""
    from jobhunter.convo.revive import revive_one
    from jobhunter.models import Application, Status

    app_id = _stuck(db, "Добрый день, вакансия уже не актуальна")
    res = revive_one(app_id)
    assert res == "закрыто как отказ", res
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status ==             Status.REJECTED_BY_EMPLOYER.value


def test_ambiguous_rejection_stays_with_owner(db):
    """«В четверг не получится, давайте в пятницу» — не отказ, а перенос."""
    from jobhunter.convo.revive import revive_one
    from jobhunter.models import Application, Status

    app_id = _stuck(db, "К сожалению, в четверг не получится, давайте в пятницу")
    revive_one(app_id)
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.NEEDS_HUMAN.value
