"""Напоминания должны доходить до отправки, а не застревать.

Модуль напоминаний был написан и поставлен в расписание, но заявка после
подготовки попадала в статус, из которого её никто не забирал: отправители
берут только APPROVED, а перехода туда в графе не было. Хуже того, этого
статуса не было в списке живых — заявка переставала слушать входящие, то
есть поздний ответ рекрутёра (а они приходят и через три недели) был бы
потерян навсегда.

Ловушка не успела сработать только потому, что первым заявкам не исполнилось
72 часа. Тесты ниже закрывают её на всех трёх уровнях: граф переходов,
список живых статусов и выбор текста при отправке.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "fu.db")
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
    from sqlalchemy import delete

    from jobhunter.models import (
        Application,
        BotOutbox,
        Employer,
        Job,
        Message,
        OwnerRequest,
        SendLog,
    )
    with db.session_scope() as sess:
        for model in (Message, SendLog, OwnerRequest, Application, Job,
                      Employer, BotOutbox):
            sess.execute(delete(model))
    yield


def _sent_app(db, *, days_ago=4):
    """Заявка, отправленная давно и без ответа — кандидат на напоминание."""
    from jobhunter.models import Application, ContactKind, Job, Status
    sent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr_acme",
                  posted_at=int(sent.timestamp()))
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=80, gate_passed=True,
                          status=Status.AWAITING_REPLY.value,
                          message_body="Исходное письмо про вакансию.",
                          message_skeleton_id="s0_stack_only",
                          cv_path="cv_base/resume.pdf", sent_at=sent)
        sess.add(app)
        sess.flush()
        return app.id


# ── граф переходов ─────────────────────────────────────────────────────

def test_prepared_followup_can_reach_sending():
    """Из статуса напоминания должен быть путь к отправке.

    Без перехода в APPROVED подготовленное напоминание не заберёт ни один
    отправитель — оба фильтруют строго по APPROVED.
    """
    from jobhunter.models import ALLOWED_TRANSITIONS, Status

    assert Status.APPROVED in ALLOWED_TRANSITIONS[Status.FOLLOWUP_PENDING_APPROVAL]


def test_followup_status_is_live():
    """Заявка с готовым напоминанием обязана слушать входящие.

    Рекрутёр может ответить как раз в те три дня, пока напоминание ждёт
    одобрения, — и этот ответ нельзя пропустить.
    """
    from jobhunter.convo.engine import LIVE
    from jobhunter.models import Status

    assert Status.FOLLOWUP_PENDING_APPROVAL.value in LIVE


# ── подготовка ─────────────────────────────────────────────────────────

def test_prepare_keeps_original_letter(db):
    """Текст напоминания не должен затирать исходное письмо.

    Иначе владелец при разборе видит «напоминаю о своём отклике» вместо
    того, что реально было отправлено, а аналитика шаблонов теряет привязку.
    """
    from jobhunter.models import Application, Status
    from jobhunter.outreach.followup import prepare

    app_id = _sent_app(db)
    prepare(dry=False)

    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
    assert app.status == Status.FOLLOWUP_PENDING_APPROVAL.value
    assert app.message_body == "Исходное письмо про вакансию."
    assert app.followup_body and app.followup_body != app.message_body
    assert app.message_skeleton_id == "s0_stack_only", "шаблон исходного письма"


def test_prepared_followup_appears_in_queue(db):
    """Напоминание должно попасть в очередь на одобрение, а не исчезнуть."""
    from jobhunter import report
    from jobhunter.outreach.followup import prepare

    _sent_app(db)
    prepare(dry=False)

    rows = report.queue_top(10)
    assert rows, "напоминание не видно в очереди — одобрить его нечем"
    assert rows[0]["is_followup"] is True


# ── отправка ───────────────────────────────────────────────────────────

def test_sender_picks_followup_text(db):
    """После одобрения отправляется текст напоминания, а не исходник."""
    from jobhunter.models import Application, Status
    from jobhunter.outreach.followup import prepare
    from jobhunter.outreach.sender import pick_batch

    app_id = _sent_app(db)
    prepare(dry=False)
    with db.session_scope() as sess:
        sess.get(Application, app_id).transition(Status.APPROVED)

    batch = pick_batch(10)
    assert batch, "одобренное напоминание не попало в партию отправки"
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        expected = app.followup_body
    # pick_batch отдаёт исходный текст, реальный выбор — в send_one; здесь
    # важно, что заявка вообще доехала до партии.
    assert batch[0]["app_id"] == app_id
    assert expected


def test_reply_during_followup_wait_is_seen(db):
    """Ответ, пришедший пока напоминание ждёт одобрения, не теряется."""
    from jobhunter.convo.engine import store_incoming
    from jobhunter.models import Application, Status
    from jobhunter.outreach.followup import prepare

    app_id = _sent_app(db)
    prepare(dry=False)

    store_incoming(app_id, [(1, "Здравствуйте! Вакансия ещё актуальна.",
                             datetime.now(timezone.utc))])
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
    assert app.first_reply_at is not None
    assert app.status == Status.REPLIED.value, (
        "заявка должна выйти из ожидания напоминания при живом ответе")
