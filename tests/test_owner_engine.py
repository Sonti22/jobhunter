"""Цикл переписки и согласование с владельцем: e2e на временной БД.

Проверяется контракт, а не реализация: входящее сообщение конкретного типа
должно закончиться конкретным действием (автоответ / карточка / закрытие),
а команда владельца — конкретным изменением состояния.
"""
import asyncio
import os
import types
import uuid
from datetime import datetime, timezone

import pytest

# ── изолированная БД на модуль ─────────────────────────────────────────

@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "test.db")
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture()
def fresh_app(db):
    """Новая заявка в AWAITING_REPLY с уникальным хендлом."""
    from jobhunter.models import Application, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="test",
                  title="Python Backend", company_name="Acme",
                  contact_handle="hr_" + uuid.uuid4().hex[:8],
                  description_raw="Python, FastAPI, PostgreSQL")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=Status.AWAITING_REPLY.value,
                          score=70, gate_passed=True)
        sess.add(app)
        sess.flush()
        return app.id


class FakeClient:
    """Ровно то, что дергают engine/owner: send_message, action, iter_messages."""

    def __init__(self, saved=None):
        self.sent = []
        self.saved = saved or []          # сообщения «Избранного», новые первыми

    async def send_message(self, peer, text):
        self.sent.append((peer, text))
        return types.SimpleNamespace(id=1000 + len(self.sent))

    def action(self, *a, **k):
        class _A:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *e):
                return False
        return _A()

    def iter_messages(self, peer, limit=50, min_id=0):
        msgs = self.saved

        async def gen():
            for m in msgs[:limit]:
                yield m
        return gen()


def run(coro):
    return asyncio.run(coro)


# ── разбор команд ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("/ok 123", ("ok", 123, "")),
    ("/ok 123 2", ("ok", 123, "2")),
    ("/ok #123", ("ok", 123, "")),
    ("/time 123 29.08 16:00", ("time", 123, "29.08 16:00")),
    ("/say 5 привет, готов", ("say", 5, "привет, готов")),
    ("/no 7", ("no", 7, "")),
    ("/skip 9", ("skip", 9, "")),
    ("/status", ("status", None, "")),
    ("/stop", ("stop", None, "")),
    ("/OK 3", ("ok", 3, "")),
])
def test_parse_command(text, want):
    from jobhunter.owner import parse_command
    got = parse_command(text)
    assert (got["cmd"], got["app_id"], got["arg"]) == want


@pytest.mark.parametrize("text", ["привет", "", "ok 5", "/unknown 1", "5 /ok"])
def test_parse_command_rejects(text):
    from jobhunter.owner import parse_command
    assert parse_command(text) is None


# ── машина состояний: каждый переход, который делает код ───────────────

@pytest.mark.parametrize("cur,to", [
    ("SENT", "REPLIED"), ("AWAITING_REPLY", "REPLIED"), ("FOLLOWED_UP", "REPLIED"),
    ("REPLIED", "IN_DIALOGUE"), ("REPLIED", "NEEDS_HUMAN"),
    ("IN_DIALOGUE", "NEEDS_HUMAN"), ("REPLIED", "INTERVIEW_PROPOSED"),
    ("IN_DIALOGUE", "INTERVIEW_PROPOSED"), ("NEEDS_HUMAN", "INTERVIEW_PROPOSED"),
    ("INTERVIEW_PROPOSED", "INTERVIEW_CONFIRMED"),
    ("NEEDS_HUMAN", "INTERVIEW_CONFIRMED"), ("IN_DIALOGUE", "INTERVIEW_CONFIRMED"),
    ("NEEDS_HUMAN", "IN_DIALOGUE"),
    ("REPLIED", "REJECTED_BY_EMPLOYER"), ("IN_DIALOGUE", "REJECTED_BY_EMPLOYER"),
    ("NEEDS_HUMAN", "REJECTED_BY_EMPLOYER"),
    ("INTERVIEW_PROPOSED", "REJECTED_BY_EMPLOYER"),
    ("INTERVIEW_CONFIRMED", "INTERVIEW_DONE"), ("INTERVIEW_DONE", "OFFER"),
])
def test_transition_paths_exist(cur, to):
    from jobhunter.models import Status, transition_path
    assert transition_path(Status[cur], Status[to]), "%s -> %s" % (cur, to)


def test_transition_path_avoids_needs_human():
    """Транзитом через NEEDS_HUMAN ходить нельзя — только явно."""
    from jobhunter.models import Status, transition_path
    p = transition_path(Status.AWAITING_REPLY, Status.INTERVIEW_PROPOSED)
    assert p and Status.NEEDS_HUMAN not in p


def test_terminal_is_dead_end():
    from jobhunter.models import Status, transition_path
    assert transition_path(Status.REJECTED_BY_EMPLOYER, Status.SENT) == []
    assert transition_path(Status.WITHDRAWN, Status.IN_DIALOGUE) == []


# ── сценарии переписки ────────────────────────────────────────────────

def test_ask_cv_autoreplies(db, fresh_app, monkeypatch):
    import jobhunter.convo.engine as eng
    from jobhunter.convo.engine import handle_message, store_incoming
    from jobhunter.models import Application, Status
    monkeypatch.setattr(eng, "within_reply_window", lambda *a, **k: True)
    store_incoming(fresh_app, [(11, "Пришлите, пожалуйста, резюме",
                                datetime.now(timezone.utc))])
    v = run(handle_message(FakeClient(), fresh_app,
                           "Пришлите, пожалуйста, резюме", dry=True))
    assert v.startswith("автоответ (ask_cv")  # суффикс — источник текста
    with db.session_scope() as sess:
        assert (sess.get(Application, fresh_app).status
                == Status.IN_DIALOGUE.value)


def test_slot_proposal_creates_card_not_reply(db, fresh_app):
    from jobhunter import owner
    from jobhunter.convo.engine import handle_message, store_incoming
    from jobhunter.models import Application, Status
    txt = "Давайте созвон в пятницу в 16:00 по Москве?"
    store_incoming(fresh_app, [(21, txt, datetime.now(timezone.utc))])
    client = FakeClient()
    v = run(handle_message(client, fresh_app, txt, dry=True))
    assert v.startswith("слоты")
    assert client.sent == []                 # рекрутёру ничего не ушло
    with db.session_scope() as sess:
        assert (sess.get(Application, fresh_app).status
                == Status.INTERVIEW_PROPOSED.value)
        req = owner.open_request_for(sess, fresh_app)
        assert req and req.kind == "slot_confirm"
        assert req.payload_json["slots"]


def test_ok_command_confirms_interview(db, fresh_app, monkeypatch):
    from jobhunter import owner
    from jobhunter.convo.engine import handle_message, store_incoming
    from jobhunter.models import Application, Status
    txt = "Удобно завтра в 15:00?"
    store_incoming(fresh_app, [(31, txt, datetime.now(timezone.utc))])
    run(handle_message(FakeClient(), fresh_app, txt, dry=True))
    async def sent_without_network(*args, **kwargs):
        return "ok"
    monkeypatch.setattr("jobhunter.convo.send.send_reply", sent_without_network)
    ans = run(owner.apply_command(
        FakeClient(), {"cmd": "ok", "app_id": fresh_app, "arg": ""}, dry=False))
    assert "интервью" in ans
    with db.session_scope() as sess:
        app = sess.get(Application, fresh_app)
        assert app.status == Status.INTERVIEW_CONFIRMED.value
        assert app.interview_at_utc is not None
        req = owner.open_request_for(sess, fresh_app)
        assert req is None                   # карточка закрыта


def test_rejection_goes_to_owner_without_llm(db, fresh_app):
    """Отказ без второго мнения LLM — карточка, не терминал.

    Раньше regex закрывал заявку в одиночку и уже ошибался на живом
    паттерне («в четверг не получится» = отказ). Теперь терминал требует
    подтверждения LLM; в этом наборе тестов она выключена, значит закрытие
    невозможно в принципе — только карточка владельцу и /close.
    """
    from jobhunter.convo.engine import handle_message
    from jobhunter.models import Application, Status
    v = run(handle_message(FakeClient(), fresh_app,
                           "Спасибо, но мы выбрали другого кандидата.", dry=True))
    assert "возможный отказ" in v
    with db.session_scope() as sess:
        assert (sess.get(Application, fresh_app).status
                == Status.NEEDS_HUMAN.value)


def test_escalation_latch(db, fresh_app):
    """После эскалации автоматика молчит даже на рутинные вопросы."""
    from jobhunter.convo.engine import handle_message
    from jobhunter.models import Application, Status
    v = run(handle_message(FakeClient(), fresh_app,
                           "Какая у вас вилка по зарплате?", dry=True))
    assert v.startswith("эскалация")
    with db.session_scope() as sess:
        assert (sess.get(Application, fresh_app).status
                == Status.NEEDS_HUMAN.value)
    client = FakeClient()
    run(handle_message(client, fresh_app, "Пришлите резюме", dry=True))
    assert client.sent == []
    with db.session_scope() as sess:
        assert (sess.get(Application, fresh_app).status
                == Status.NEEDS_HUMAN.value)


# ── тёплая квота ──────────────────────────────────────────────────────

def test_warm_quota_ignores_cold_sends(db, fresh_app):
    from jobhunter.convo.send import warm_sent_today
    from jobhunter.models import Application, Message, utcnow
    with db.session_scope() as sess:
        before = warm_sent_today(sess)
        # холодное: рекрутёр ещё не отвечал (first_reply_at нет)
        sess.add(Message(application_id=fresh_app, direction="out",
                         body="cold", sent_at=utcnow()))
        assert warm_sent_today(sess) == before
        # тёплое: рекрутёр отвечал
        sess.get(Application, fresh_app).first_reply_at = utcnow()
        sess.add(Message(application_id=fresh_app, direction="out",
                         body="warm", sent_at=utcnow()))
        assert warm_sent_today(sess) == before + 2


# ── первый опрос «Избранного» не исполняет старые команды ─────────────

def test_poll_commands_first_run_only_sets_watermark(db):
    from jobhunter import owner
    from jobhunter.models import CampaignState
    with db.session_scope() as sess:
        st = sess.get(CampaignState, 1)
        if st:
            st.owner_last_seen_msg_id = 0
    old = [types.SimpleNamespace(id=50, message="/ok 1"),
           types.SimpleNamespace(id=49, message="/ok 2")]
    decisions = run(owner.poll_commands(FakeClient(saved=old)))
    assert decisions == []                   # старое не исполняется
    with db.session_scope() as sess:
        assert sess.get(CampaignState, 1).owner_last_seen_msg_id == 50
    # следующий опрос видит только новое
    new = [types.SimpleNamespace(id=51, message="/status")] + old
    decisions = run(owner.poll_commands(FakeClient(saved=new)))
    assert [d["cmd"] for d in decisions] == ["status"]
