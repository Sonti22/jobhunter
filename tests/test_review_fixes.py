# -*- coding: utf-8 -*-
"""Регрессии на дефекты адверсарной перепроверки от 05.09.

Каждый тест — живой сценарий отказа, подтверждённый скептиком на реальном
коде, а не гипотеза. Покраснел — значит вернулся конкретный инцидент.
"""
import os
import uuid

import pytest

OWNER = 5875908057


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "rf.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = str(OWNER)
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


# ── PII: телефон против зарплатной вилки ──

def test_salary_ranges_survive_redaction():
    """«300 000 - 350 000» — деньги: ответ о зарплате цитирует цифру
    дословно, и редакция не имеет права её съесть."""
    from jobhunter.llm import redact_pii

    for txt in ("вилка 300 000 - 350 000 на руки",
                "бюджет 250 000 рублей",
                "оффер 1 200 000 в год"):
        assert "<PHONE>" not in redact_pii(txt), txt


def test_phones_with_dots_are_redacted():
    """«8.999.123.45.67» раньше уходил во внешний API нередактированным."""
    from jobhunter.llm import redact_pii

    for txt in ("тел. 8.999.123.45.67", "звони +7 (999) 404-84-43",
                "мой номер 89994048443"):
        assert "<PHONE>" in redact_pii(txt), txt


def test_redaction_is_wired_into_generate(monkeypatch):
    """Проводка, не только чистая функция: generate() обязан слать провайдеру
    уже вычищенный prompt."""
    import jobhunter.llm as llm

    captured = {}

    def fake_provider(prompt, key, s, timeout):
        captured["prompt"] = prompt
        return "ответ"

    monkeypatch.setattr(llm, "PROVIDERS", [("fake", fake_provider)])
    monkeypatch.setattr(llm, "_key_for", lambda name, s: "k")
    res = llm.generate("позвони мне: +7 999 404-84-43 и напиши a@b.com")
    assert res.ok
    assert "<PHONE>" in captured["prompt"] and "<EMAIL>" in captured["prompt"]
    assert "404-84-43" not in captured["prompt"]


# ── авто-ответ: плейсхолдер не уходит рекрутёру ──

def test_placeholder_in_routine_reply_is_rejected(monkeypatch):
    from jobhunter.convo import draft as d

    monkeypatch.setattr(
        d, "generate",
        lambda prompt, timeout=30.0: type(
            "R", (), {"ok": True, "text": "Наберу вас по <PHONE> завтра!",
                      "provider": "fake", "error": ""})())
    from jobhunter.config import get_settings
    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    try:
        out = d.draft_routine_reply("ask_cv", "Backend", "", "пришлите резюме",
                                    [])
        assert not out.ok
        assert "плейсхолдер" in (out.problem or "")
    finally:
        monkeypatch.setenv("LLM_ENABLED", "false")
        get_settings.cache_clear()


# ── очередь задач: краш-луп не зависает в pending ──

def test_crash_looping_task_becomes_failed(db):
    from datetime import datetime, timedelta, timezone

    from jobhunter.bot import state
    from jobhunter.models import BotTask

    while state.task_pop() is not None:
        pass
    state.task_push({"do": "task", "chat_id": 1, "task": "boom"})
    old = (datetime.now(timezone.utc).replace(tzinfo=None)
           - timedelta(minutes=999))
    with db.session_scope() as sess:
        row = sess.query(BotTask).order_by(BotTask.id.desc()).first()
        row.status = "running"
        row.claimed_at = old
        row.attempts = 5              # пять крашей процесса подряд
        row_id = row.id
    assert state.task_pop() is None, "исчерпанная задача не должна выдаваться"
    with db.session_scope() as sess:
        row = sess.get(BotTask, row_id)
        assert row.status == "failed", \
            "после лимита попыток задача обязана стать failed, а не вечный pending"


# ── дашборд: /approve не валится и не завышает батч ──

def test_dashboard_approve_survives_bad_gate(db):
    from fastapi.testclient import TestClient

    from jobhunter.models import Application, Batch, Job, Status
    from jobhunter.web.server import app as webapp

    with db.session_scope() as sess:
        ids = {}
        for tag, passed in (("bad", False), ("good", True)):
            job = Job(external_uuid=str(uuid.uuid4()), source="test",
                      title="w-" + tag)
            sess.add(job)
            sess.flush()
            a = Application(job_id=job.id,
                            status=Status.PENDING_APPROVAL.value,
                            gate_passed=passed, score=70)
            sess.add(a)
            sess.flush()
            ids[tag] = a.id

    client = TestClient(webapp)
    r = client.post("/approve",
                    data={"ids": [str(ids["bad"]), str(ids["good"])]},
                    follow_redirects=False)
    assert r.status_code == 303, "одна битая заявка не должна давать 500"
    with db.session_scope() as sess:
        assert sess.get(Application, ids["good"]).status == \
            Status.APPROVED.value
        assert sess.get(Application, ids["bad"]).status == \
            Status.PENDING_APPROVAL.value
        batch = sess.query(Batch).order_by(Batch.id.desc()).first()
        assert batch.approved_count == 1, "батч считает факт, не длину формы"


def test_dashboard_approve_handles_followup(db):
    from fastapi.testclient import TestClient

    from jobhunter.models import Application, Job, Status
    from jobhunter.web.server import app as webapp

    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="test", title="fu")
        sess.add(job)
        sess.flush()
        a = Application(job_id=job.id,
                        status=Status.FOLLOWUP_PENDING_APPROVAL.value,
                        gate_passed=True, score=70, followup_body="ping")
        sess.add(a)
        sess.flush()
        app_id = a.id

    client = TestClient(webapp)
    client.post("/approve", data={"ids": [str(app_id)]},
                follow_redirects=False)
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == Status.APPROVED.value, \
            "отмеченный follow-up больше не игнорируется молча"
