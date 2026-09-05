"""Регресс-тесты на дефекты, найденные полным аудитом.

Каждый тест — это конкретный сломанный сценарий, который жил в системе:
не «проверка функции», а запрет на возврат конкретной ошибки.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "audit.db")
    os.environ["LLM_ENABLED"] = "false"
    os.environ["DAILY_COLD_LIMIT"] = "30"
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

    from jobhunter.models import Application, CampaignState, Employer, Job, SendLog
    with db.session_scope() as sess:
        for model in (SendLog, Application, Job, Employer, CampaignState):
            sess.execute(delete(model))
    yield


def _job_app(db, *, status, email="", handle="", score=70, employer=None,
             lease=None):
    from jobhunter.models import Application, ContactKind, Job
    with db.session_scope() as sess:
        kind = ContactKind.EMAIL.value if email else ContactKind.USER_HANDLE.value
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:t",
                  title="Python Dev", company_name="Acme",
                  contact_kind=kind,
                  contact_handle=handle,
                  contact_url=("mailto:" + email) if email else "",
                  description_raw="Вакансия Python developer. Требования: Python, FastAPI")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=score, status=status.value,
                          gate_passed=True, employer_id=employer,
                          sending_lease_until=lease)
        sess.add(app)
        sess.flush()
        return app.id


def test_stale_sending_is_reclaimed(db):
    """Сбой между «беру» и «отправлено» больше не хоронит заявку."""
    from jobhunter.models import Application, Status
    from jobhunter.outreach.sender import reclaim_stale_sending

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    live = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=2)
    dead_id = _job_app(db, status=Status.SENDING, handle="a", lease=old)
    live_id = _job_app(db, status=Status.SENDING, handle="b", lease=live)

    assert reclaim_stale_sending() == 1
    with db.session_scope() as sess:
        assert sess.get(Application, dead_id).status == Status.APPROVED.value
        assert sess.get(Application, live_id).status == Status.SENDING.value


def test_freemail_domains_are_not_one_company(db):
    """Два рекрутёра на gmail — два письма, а не одно."""
    from jobhunter.models import Status
    from jobhunter.outreach.mailer import pick_batch

    _job_app(db, status=Status.APPROVED, email="anna.hr@gmail.com")
    _job_app(db, status=Status.APPROVED, email="ivan.recruit@gmail.com")
    _job_app(db, status=Status.APPROVED, email="hr@acme.io")
    _job_app(db, status=Status.APPROVED, email="boss@acme.io")  # тот же домен

    emails = {b["email"] for b in pick_batch(10)}
    assert "anna.hr@gmail.com" in emails and "ivan.recruit@gmail.com" in emails
    assert len([e for e in emails if e.endswith("acme.io")]) == 1


def test_employer_cooldown_between_runs(db):
    """Написали компании неделю назад — новый APPROVED к ней молчит."""
    from jobhunter.models import Employer, Status
    from jobhunter.outreach.mailer import pick_batch

    with db.session_scope() as sess:
        emp = Employer(handle_norm="hr@acme.io", handle_kind="email",
                       display_name="Acme",
                       last_contacted_at=datetime.now(timezone.utc)
                       .replace(tzinfo=None) - timedelta(days=7))
        sess.add(emp)
        sess.flush()
        emp_id = emp.id
    _job_app(db, status=Status.APPROVED, email="hr@acme.io", employer=emp_id)
    assert pick_batch(10) == []


def test_smtp_error_does_not_kill_the_batch(db):
    """APPROVED → SEND_FAILED идёт через advance, а не запрещённым скачком."""
    from jobhunter.models import Application, Status

    app_id = _job_app(db, status=Status.APPROVED, email="hr@x.io")
    with db.session_scope() as sess:
        a = sess.get(Application, app_id)
        assert a.advance(Status.SEND_FAILED, reason="SMTPException")
        assert a.status == Status.SEND_FAILED.value


def test_slot_without_time_has_no_confirm_button(db):
    """«В пятницу» без часа — кнопка уточнения, а не «✅ пт 10:00»."""
    from jobhunter.bot import cards

    class Req:
        id = 7
        kind = "SLOT_CONFIRM"
        payload_json = {"slots": [
            {"utc": "2026-08-28T10:00:00", "tz": "Europe/Moscow",
             "raw": "в пятницу", "has_time": False},
            {"utc": "2026-08-28T16:00:00", "tz": "Europe/Moscow",
             "raw": "в пятницу в 16", "has_time": True},
        ]}

    from jobhunter.models import OwnerRequestKind
    Req.kind = OwnerRequestKind.SLOT_CONFIRM.value
    kb = cards.keyboard_for(Req)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    ok_buttons = [b for b in flat if ":ok:" in b["callback_data"]]
    assert len(ok_buttons) == 1, "подтверждать можно только названное время"
    assert "19:00" in ok_buttons[0]["text"]   # 16:00 UTC = 19:00 МСК
    assert any("уточнить" in b["text"] for b in flat)


def test_campaign_state_ceiling_matches_settings(db):
    """Строку кампании создаёт только policy: потолок 30, не дефолт модели 15."""
    from jobhunter.convo.imapbox import _state
    _state()                            # раньше создавал строку с потолком 15
    from jobhunter.models import CampaignState
    with db.session_scope() as sess:
        st = sess.get(CampaignState, 1)
        assert st is not None
        assert st.quota_ceiling == 30


def test_placeholder_email_subject_uses_real_role():
    from jobhunter.models import Job
    from jobhunter.outreach.mailer import _subject

    job = Job(title="Текст вакансии:", tag="", description_raw="Python FastAPI backend")
    assert "Текст вакансии" not in _subject(job, "ru")
    assert "Backend" in _subject(job, "en")
    url_title = Job(title="https://example.com", tag="python",
                    description_raw="Python backend")
    assert "https://" not in _subject(url_title, "en")


def test_dashboard_rejects_foreign_origin(db):
    """Чужая вкладка не может снять стоп-кран через form POST на localhost."""
    from fastapi.testclient import TestClient

    from jobhunter.web.server import app

    client = TestClient(app)
    r = client.post("/killswitch", data={"on": "0"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r2 = client.post("/killswitch", data={"on": "0"},
                     headers={"Origin": "http://127.0.0.1:8765"},
                     follow_redirects=False)
    assert r2.status_code != 403


def test_dashboard_summary_is_json_serializable(db):
    """Сводка не падает на datetime-полях quota/source-метрик."""
    from fastapi.testclient import TestClient

    from jobhunter.web.server import app

    client = TestClient(app)
    summary = client.get("/api/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert set(("totals", "funnel", "conversion", "quota")) <= payload.keys()
    assert client.get("/queue").status_code == 200


def test_peerflood_peer_is_quarantined(db, monkeypatch):
    """PeerFlood-триггер не возвращается в очередь — второй страйк по тому
    же адресату срезал потолок кампании вдвое и включил ручной режим."""
    import asyncio

    from jobhunter.models import Application, Employer, Status
    from jobhunter.outreach import sender

    with db.session_scope() as sess:
        emp = Employer(handle_norm="angel", handle_kind="user_handle")
        sess.add(emp)
        sess.flush()
        emp_id = emp.id
    app_id = _job_app(db, status=Status.APPROVED, handle="angel",
                      employer=emp_id)

    class FakeErrors:
        class PeerFloodError(Exception):
            pass
        class FloodWaitError(Exception):
            pass
        class UserPrivacyRestrictedError(Exception):
            pass
        class UsernameNotOccupiedError(Exception):
            pass
        class UsernameInvalidError(Exception):
            pass

    class FakePeer:
        user_id = 1

    async def fake_resolve(client, sess, handle):
        return FakePeer()

    class FakeClient:
        def action(self, *a, **kw):
            raise FakeErrors.PeerFloodError("Too many requests")

        async def send_file(self, *a, **kw):
            raise FakeErrors.PeerFloodError("Too many requests")

        async def send_message(self, *a, **kw):
            raise FakeErrors.PeerFloodError("Too many requests")

    import sys
    import types
    monkeypatch.setitem(sys.modules, "telethon",
                        types.SimpleNamespace(errors=FakeErrors))
    monkeypatch.setattr(sender, "resolve", fake_resolve)
    monkeypatch.setattr(sender, "resolve_cv", lambda p: "/tmp/fake.pdf")
    monkeypatch.setattr(sender.policy, "typing_seconds", lambda t, r: 0)

    item = {"app_id": app_id, "handle": "angel", "job_id": 1,
            "employer_id": emp_id, "text": "hi", "cv_path": "",
            "score": 70, "title": "X", "company": "", "tag": ""}
    import random
    res = asyncio.run(sender.send_one(FakeClient(), item,
                                      random.Random(1), dry=False))
    assert res == "stop:PeerFloodError"
    with db.session_scope() as sess:
        a = sess.get(Application, app_id)
        assert a.status == Status.HANDLE_DEAD.value,             "peer-триггер обязан уйти в карантин, а не обратно в очередь"
        assert sess.get(Employer, emp_id).do_not_contact


def test_soft_restart_after_peerflood_lock(db):
    """Первый день после лока — максимум 3 сообщения, не полный потолок."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete

    from jobhunter.models import DailyQuota, SendLock
    from jobhunter.outreach import policy
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db.session_scope() as sess:
        sess.execute(delete(DailyQuota))   # чужая сегодняшняя квота мешает
        st = policy.get_state(sess)
        st.quota_ceiling = 7
        sess.merge(SendLock(id=1, locked_until=now - timedelta(hours=2),
                            reason="peerflood", scope="cold_only"))
    with db.session_scope() as sess:
        q = policy.get_quota(sess)
        assert q.planned_cap == 3, "после лока день обязан начинаться с разведки"


def test_clean_days_restore_ceiling(db):
    """3 чистых дня подряд возвращают +1 к потолку, максимум 15."""
    from jobhunter.outreach import policy

    with db.session_scope() as sess:
        st = policy.get_state(sess)
        st.quota_ceiling = 7
        st.consecutive_clean_days = 2
        q = policy.get_quota(sess)
        q.clean_day = True
        q.sent_count = 3
        policy.close_day(sess)
        assert st.consecutive_clean_days == 3
        assert st.quota_ceiling == 8

        st.quota_ceiling = 15
        st.consecutive_clean_days = 5
        policy.close_day(sess)
        assert st.quota_ceiling == 15, "выше 15 не разгоняемся"


def test_repair_queue_never_touches_sent(db):
    """Пересборка очереди не трогает отправленное.

    Без фильтра sent_at модуль откатил 44 уже отправленные заявки в
    дозаявочный статус: они выпали из «живых», ответы рекрутёров по ним
    перестали читаться, а часть встала в очередь на повторную отправку.
    """
    from datetime import datetime, timezone

    from jobhunter.models import Application, Status
    from jobhunter.repair_queue import reset

    fresh = _job_app(db, status=Status.PENDING_APPROVAL, handle="a")
    sent = _job_app(db, status=Status.APPROVED, handle="b")
    with db.session_scope() as sess:
        sess.get(Application, sent).sent_at = datetime.now(timezone.utc) \
            .replace(tzinfo=None)

    stats = reset(dry=True)
    assert stats["всего"] == 1, "в пересборку попала отправленная заявка"

    reset(dry=False)
    with db.session_scope() as sess:
        assert sess.get(Application, sent).status == Status.APPROVED.value
        assert sess.get(Application, fresh).status == Status.DISCOVERED.value


def test_repair_queue_ignores_advance_refusal(monkeypatch):
    """Заявка, чей переход граф запрещает, не должна терять gate-поля.

    Модель гонки: между выборкой ids и обработкой заявка ушла дальше по
    конвейеру. advance() тогда возвращает False — и раньше reset() это
    игнорировал, обнуляя gate_passed у живой отправленной заявки, из-за
    чего позже падало одобрение follow-up.
    """
    import uuid

    from jobhunter import repair_queue
    from jobhunter.db import session_scope
    from jobhunter.models import Application, Job, Status

    with session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="test",
                  title="race-check")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=Status.SENT.value,
                          gate_passed=True, sent_at=None)
        sess.add(app)
        sess.flush()
        app_id = app.id

    # SENT попадает в выборку только через подменённый RESETTABLE — это и
    # моделирует «статус сменился после выборки»: guard в цикле обязан
    # увидеть, что advance(DISCOVERED) из SENT запрещён, и не трогать поля.
    monkeypatch.setattr(
        repair_queue, "RESETTABLE",
        (Status.PENDING_APPROVAL.value, Status.APPROVED.value,
         Status.SENT.value))
    repair_queue.reset(dry=False)
    with session_scope() as sess:
        got = sess.get(Application, app_id)
        assert got.status == Status.SENT.value
        assert got.gate_passed is True, "gate-поля отправленной трогать нельзя"


def test_sender_text_first_then_file():
    """Решение владельца 04.09: сначала сообщение, потом файл отдельно.

    Проверяем форму кода: текст уходит первым (send_message), файл — после,
    в отдельном try, и его сбой не мешает заявке дойти до SENT. Follow-up
    файл не прикладывает вовсе.
    """
    import inspect

    from jobhunter.outreach import sender

    src = inspect.getsource(sender.send_one)
    assert src.index("await client.send_message(peer.user_id, text)") \
        < src.index("await client.send_file"), "текст обязан уходить первым"
    assert "caption=text" not in src, "письмо больше не подпись к файлу"
    assert "not is_followup" in src, "follow-up не должен повторять резюме"
    assert "cv_peerflood" in src, "страйк на файле должен логироваться"
