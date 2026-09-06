"""Двухъярусная классификация: отказ не закрывает заявку в одиночку.

Найдено на живых паттернах: «К сожалению, в четверг не получится, давайте
в пятницу» матчит отказ с уверенностью 0.9 — и заявка, по которой рекрутёр
предложил перенос, терминально закрывалась без карточки. Терминал необратим,
поэтому теперь он требует согласия ДВУХ ярусов: regex ≥ 0.9 и LLM ≥ 0.8,
прочитавшей историю треда. Всё остальное — карточка владельцу.
"""
import asyncio
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "verify.db")
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
    from jobhunter.models import Base

    # Все таблицы в обратном порядке зависимостей, а не семь по списку:
    # у applications появились новые потомки (feedback, батчи), и ручной
    # список падал на FOREIGN KEY, если перед этим модулем отработал любой
    # другой — тест зависел от порядка запуска.
    with db.session_scope() as sess:
        for table in reversed(Base.metadata.sorted_tables):
            sess.execute(table.delete())
    yield


def _app(db, status=None):
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
                          status=(status or Status.AWAITING_REPLY).value)
        sess.add(app)
        sess.flush()
        return app.id


def _llm_json(monkeypatch, payload: str, calls: list | None = None):
    from jobhunter import llm

    def fake(prompt, timeout=30.0):
        if calls is not None:
            calls.append(prompt)
        return llm.LLMResult(ok=True, text=payload, provider="fake")

    monkeypatch.setattr("jobhunter.convo.verify.generate", fake)


def _handle(db, app_id, text):
    from jobhunter.convo.engine import handle_message
    return asyncio.run(handle_message(None, app_id, text, dry=True))


def test_reschedule_is_not_a_rejection(db, monkeypatch):
    """«В четверг не получится, давайте в пятницу» — перенос, не отказ."""
    from jobhunter.convo.classify import REJECTION, classify

    intent = classify("К сожалению, в четверг не получится, давайте в пятницу")
    # regex сам понижает уверенность: маркер отказа рядом с днём недели
    assert intent.label == REJECTION
    assert intent.confidence < 0.62, "перенос не должен добраться до закрытия"


def test_confirmed_rejection_closes_and_notifies(db, monkeypatch):
    from jobhunter import notify
    from jobhunter.models import Application, Status

    app_id = _app(db)
    _llm_json(monkeypatch,
              '{"label": "rejection", "confidence": 0.95, "reason": "явный отказ"}')
    res = _handle(db, app_id, "Мы выбрали другого кандидата, спасибо за отклик.")
    assert "закрыта" in res
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status \
            == Status.REJECTED_BY_EMPLOYER.value
    assert any(r["kind"] == "rejection_closed" for r in notify.pending(20)), \
        "закрытие должно быть видно владельцу, а не только статистике"


def test_llm_disagreement_goes_to_owner(db, monkeypatch):
    from jobhunter.models import Application, OwnerRequest, Status

    app_id = _app(db)
    _llm_json(monkeypatch,
              '{"label": "slot_proposed", "confidence": 0.9, "reason": "перенос"}')
    # Без дня недели в тексте: понижение «отказ+слот» — отдельный кейс выше,
    # здесь проверяем именно несогласие LLM при уверенном regex-отказе.
    res = _handle(db, app_id, "Мы не готовы продолжать общение по позиции.")
    assert "карточка" in res
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == Status.NEEDS_HUMAN.value, "терминала быть не должно"
        assert sess.query(OwnerRequest).count() == 1


def test_llm_unavailable_never_closes(db, monkeypatch):
    """Сбой модели = карточка. Fail-open в сторону владельца."""
    from jobhunter import llm
    from jobhunter.models import Application, Status

    app_id = _app(db)
    monkeypatch.setattr(
        "jobhunter.convo.verify.generate",
        lambda *a, **kw: llm.LLMResult(text="", ok=False, error="timeout"))
    _handle(db, app_id, "Мы выбрали другого кандидата.")
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.NEEDS_HUMAN.value


def test_garbage_llm_answer_is_a_failure(db, monkeypatch):
    from jobhunter.convo.verify import verify_intent
    for garbage in ("не знаю", '{"label": "banana", "confidence": 1}',
                    '{"label": "rejection", "confidence": "high"}'):
        _llm_json(monkeypatch, garbage)
        assert verify_intent("текст", [], "rejection") is None


def test_routine_intents_skip_llm(db, monkeypatch):
    """ask_cv/ack — без второго мнения: там нечего терять."""
    calls = []
    app_id = _app(db)
    _llm_json(monkeypatch, '{"label": "ack", "confidence": 1}', calls)
    _handle(db, app_id, "Пришлите, пожалуйста, ваше резюме")
    assert calls == [], "verify не должен вызываться для рутины"


def test_unknown_upgraded_by_llm(db, monkeypatch):
    """LLM уверенно узнала просьбу резюме в unknown-тексте → автоответ."""
    app_id = _app(db)
    _llm_json(monkeypatch,
              '{"label": "ask_cv", "confidence": 0.9, "reason": "просит резюме"}')
    res = _handle(db, app_id, "Скиньте пожалуйста вашу анкетку глянуть")
    assert "автоответ" in res or "отложен" in res


def test_close_command(db):
    """/close закрывает спорную заявку руками владельца."""
    import asyncio as aio

    from jobhunter import owner
    from jobhunter.models import Application, Job, Status

    app_id = _app(db, status=Status.NEEDS_HUMAN)
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id)
        owner.create_human_request(sess, app, job, "Мы выбрали другого.",
                                   "похоже на отказ")
    cmd = owner.parse_command("/close %d" % app_id)
    assert cmd["cmd"] == "close"
    out = aio.run(owner.apply_command(None, cmd, dry=False))
    assert "закрыта" in out
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status \
            == Status.REJECTED_BY_EMPLOYER.value


def test_rejection_card_has_close_button(db):
    """В боте закрыть спорный отказ можно кнопкой: /close из карточки вырезан."""
    from sqlalchemy import select

    from jobhunter import owner
    from jobhunter.bot import cards
    from jobhunter.models import Application, Job, OwnerRequest, Status

    app_id = _app(db, status=Status.NEEDS_HUMAN)
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        job = sess.get(Job, app.job_id)
        owner.create_human_request(sess, app, job, "Мы выбрали другого.",
                                   "похоже на отказ, но я не уверен")
    with db.session_scope() as sess:
        req = sess.scalars(select(OwnerRequest)).first()
        kb = cards.keyboard_for(req)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    close = [b for b in flat if b["callback_data"].endswith(":close")]
    assert close, "кнопки подтверждения отказа нет"
    # а на обычной эскалации её быть не должно
    with db.session_scope() as sess:
        req = sess.scalars(select(OwnerRequest)).first()
        req.payload_json = dict(req.payload_json, reason="техвопрос")
        kb2 = cards.keyboard_for(req)
    flat2 = [b for row in kb2["inline_keyboard"] for b in row]
    assert not [b for b in flat2 if b["callback_data"].endswith(":close")]
