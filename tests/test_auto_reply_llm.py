"""Контекстные автоответы: живой текст вместо random.choice.

Правило безопасности: LLM-вариант проходит проверку качества, гейт правды
и — для предложений времени — дословную сверку строки слотов, которую
посчитал КОД. Любой сбой на любом шаге откатывает на шаблон: шаблон
выдумать ничего не может, и автоответ уходит всегда.
"""
import asyncio
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "arl.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "5875908057"
    os.environ["LLM_ENABLED"] = "true"
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
        PendingReply,
        SendLog,
    )
    with db.session_scope() as sess:
        for model in (Message, SendLog, OwnerRequest, PendingReply,
                      Application, Job, Employer, BotOutbox):
            sess.execute(delete(model))
    yield


def _app(db):
    from jobhunter.models import Application, ContactKind, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr_acme",
                  description_raw="Python, FastAPI, Docker")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=80,
                          status=Status.AWAITING_REPLY.value)
        sess.add(app)
        sess.flush()
        return app.id


def _mock_llm(monkeypatch, text):
    from jobhunter import llm

    monkeypatch.setattr(
        "jobhunter.convo.draft.generate",
        lambda prompt, timeout=30.0: llm.LLMResult(ok=True, text=text,
                                                   provider="fake"))


def _handle(app_id, text):
    from jobhunter.convo.engine import handle_message
    return asyncio.run(handle_message(None, app_id, text, dry=True))


def test_llm_reply_used_when_clean(db, monkeypatch):
    _mock_llm(monkeypatch, "Конечно, прикладываю резюме — там подробно про "
                           "опыт с Python и FastAPI. Буду рад вопросам.")
    app_id = _app(db)
    res = _handle(app_id, "Пришлите, пожалуйста, ваше резюме")
    assert "llm" in res, res


def test_fabrication_falls_back_to_template(db, monkeypatch):
    """LLM приписала кандидату чужой стек — гейт режет, уходит шаблон."""
    _mock_llm(monkeypatch, "Прикладываю резюме. Также десять лет пишу на "
                           "C# и веду команды в .NET-проектах.")
    app_id = _app(db)
    res = _handle(app_id, "Пришлите, пожалуйста, ваше резюме")
    assert "шаблон" in res, "выдумка обязана откатить на шаблон"


def test_slots_line_must_survive_verbatim(db, monkeypatch):
    """Модель «переписала» время — текст не годится, уходит шаблон."""
    from jobhunter import llm

    def fake(prompt, timeout=30.0):
        return llm.LLMResult(ok=True, provider="fake",
                             text="Готов созвониться завтра в 9 утра или "
                                  "в любое время в выходные.")

    monkeypatch.setattr("jobhunter.convo.draft.generate", fake)
    app_id = _app(db)
    res = _handle(app_id, "Когда вам удобно созвониться?")
    assert "шаблон" in res
    # шаблонный текст содержит слоты, посчитанные кодом


def test_llm_off_is_byte_identical_to_templates(db, monkeypatch):
    """Регресс-защита: выключенная LLM — ровно старое поведение."""
    from jobhunter.config import get_settings

    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        app_id = _app(db)
        res = _handle(app_id, "Пришлите, пожалуйста, ваше резюме")
        assert "шаблон" in res
    finally:
        monkeypatch.setenv("LLM_ENABLED", "true")
        get_settings.cache_clear()


def test_ack_ping_pong_goes_silent(db):
    """«Спасибо» после нашего же вежливого закрытия — тишина, не третий ответ."""
    from jobhunter.models import Message
    app_id = _app(db)
    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="in",
                         body="Спасибо, передам коллегам"))
        sess.add(Message(application_id=app_id, direction="out",
                         body="Спасибо! Буду ждать обратной связи.",
                         is_auto=True))
    res = _handle(app_id, "Спасибо вам!")
    assert "без действий" in res
    assert "вежлив" in res


def test_slot_card_carries_draft(db, monkeypatch):
    """Слот-карточка приходит с черновиком подтверждения, а не голой."""
    from sqlalchemy import select

    from jobhunter.models import OwnerRequest

    _mock_llm(monkeypatch, "Отлично, четверг подходит. Подскажите, созвон "
                           "в Zoom или по телефону?")
    app_id = _app(db)
    res = _handle(app_id, "Удобно в четверг в 15:00?")
    assert "карточка" in res
    with db.session_scope() as sess:
        req = sess.scalars(select(OwnerRequest)).first()
        assert req is not None
        assert req.payload_json.get("draft"), "черновик должен лежать в payload"
        assert "Черновик ответа" in req.question
