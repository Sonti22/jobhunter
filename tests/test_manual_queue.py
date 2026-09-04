"""Очередь ручных откликов: вакансии без прямого контакта.

72% базы — вакансии, куда откликаются через форму компании. Автозаполнять
формы нельзя: отсеивающие вопросы (право на работу, годы опыта, релокация)
требуют ответов владельца, а уверенно-неверный ответ закрывает компанию
навсегда. Поэтому система доводит вакансию до последнего клика и запоминает
отметку, а сам отклик подаёт человек.

Состояние живёт в `Application.outcome`, а не в статусе: у `HANDLE_MISSING`
в графе переходов всего два выхода, и втаскивать ручные отклики в машину
состояний значит переписать граф и сломать смысл воронки.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "manual.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["BOT_ALLOWED_USER_IDS"] = "5875908057"
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


def _job_app(db, *, score=80, source="ats:greenhouse", url="https://co/job/1",
             title="Python Backend", outcome=""):
    from jobhunter.models import Application, ContactKind, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source=source, title=title,
                  company_name="Acme", contact_kind=ContactKind.EXTERNAL_URL.value,
                  contact_url=url)
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=score, outcome=outcome,
                          status=Status.HANDLE_MISSING.value,
                          cv_path="cv_base/resume.pdf")
        sess.add(app)
        sess.flush()
        return app.id


# ── выборка ────────────────────────────────────────────────────────────

def test_listing_is_not_limited_to_ats(db):
    """Раньше показывались только ATS — из тысяч видно было пару сотен."""
    from jobhunter.manual_apply import listing

    _job_app(db, source="ats:lever", title="ATS-вакансия")
    _job_app(db, source="careered", title="Careered-вакансия")
    _job_app(db, source="boards:remoteok", title="Борд-вакансия")

    titles = {r["title"] for r in listing(20)}
    assert titles == {"ATS-вакансия", "Careered-вакансия", "Борд-вакансия"}


def test_row_without_link_is_skipped(db):
    """Строка, по которой нельзя откликнуться, тратит время впустую."""
    from jobhunter.manual_apply import listing

    _job_app(db, url="", title="Без ссылки")
    _job_app(db, url="https://co/job/2", title="Со ссылкой")
    assert [r["title"] for r in listing(20)] == ["Со ссылкой"]


def test_low_score_excluded(db):
    from jobhunter.manual_apply import MIN_SCORE, listing

    _job_app(db, score=MIN_SCORE - 1, title="Слабая")
    _job_app(db, score=MIN_SCORE + 1, title="Годная")
    assert [r["title"] for r in listing(20)] == ["Годная"]


# ── отметки ────────────────────────────────────────────────────────────

def test_applied_leaves_the_queue(db):
    from jobhunter.manual_apply import listing, mark

    app_id = _job_app(db)
    assert len(listing(20)) == 1
    note = mark(app_id, "applied")
    assert "откликнулся" in note
    assert listing(20) == [], "обработанная вакансия не должна возвращаться"


def test_applied_at_recorded(db):
    from jobhunter.manual_apply import mark
    from jobhunter.models import Application

    app_id = _job_app(db)
    mark(app_id, "applied")
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
    assert app.outcome == "applied"
    assert app.applied_at is not None


def test_snoozed_returns_after_deadline(db):
    """«Потом» — это отложить, а не выбросить."""
    from jobhunter.manual_apply import listing, mark
    from jobhunter.models import Application

    app_id = _job_app(db)
    mark(app_id, "snoozed", snooze_days=3)
    assert listing(20) == []

    with db.session_scope() as sess:
        sess.get(Application, app_id).snooze_until = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1))
    assert len(listing(20)) == 1, "срок вышел — вакансия должна вернуться"


def test_not_fit_never_returns(db):
    from jobhunter.manual_apply import listing, mark

    app_id = _job_app(db)
    mark(app_id, "not_fit")
    assert listing(20) == []


def test_unknown_outcome_rejected(db):
    from jobhunter.manual_apply import mark
    from jobhunter.models import Application

    app_id = _job_app(db)
    assert mark(app_id, "чепуха") == "неизвестная отметка"
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).outcome == ""


def test_double_mark_is_harmless(db):
    """Кнопку нажимают дважды — состояние не должно ломаться."""
    from jobhunter.manual_apply import mark
    from jobhunter.models import Application

    app_id = _job_app(db)
    mark(app_id, "applied")
    mark(app_id, "applied")
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).outcome == "applied"


def test_stats_counts_states(db):
    from jobhunter.manual_apply import mark, stats

    a, b, _c = _job_app(db), _job_app(db), _job_app(db)
    mark(a, "applied")
    mark(b, "not_fit")
    s = stats()
    assert s["total"] == 3 and s["applied"] == 1 and s["not_fit"] == 1
    assert s["ready"] == 1


# ── утренняя пачка ─────────────────────────────────────────────────────

def test_batch_sends_cards_with_buttons(db):
    from jobhunter import notify
    from jobhunter.manual_batch import run

    for i in range(3):
        _job_app(db, title="Вакансия %d" % i, url="https://co/job/%d" % i)

    res = run(limit=3)
    assert res["sent"] == 3
    items = [r for r in notify.pending(50) if r["kind"] == "manual_item"]
    assert len(items) == 3
    kb = items[0]["markup"]["inline_keyboard"]
    flat = [b for row in kb for b in row]
    assert any(b.get("url") for b in flat), "должна быть кнопка открытия вакансии"
    assert any("applied" in (b.get("callback_data") or "") for b in flat)


def test_batch_does_not_repeat_same_vacancy(db):
    """Повторный прогон не должен слать ту же вакансию второй раз."""
    from jobhunter import notify
    from jobhunter.manual_batch import run

    _job_app(db, title="Одна")
    run(limit=5)
    before = len([r for r in notify.pending(50) if r["kind"] == "manual_item"])
    run(limit=5)
    after = len([r for r in notify.pending(50) if r["kind"] == "manual_item"])
    assert before == after == 1


def test_batch_skips_marked(db):
    from jobhunter.manual_apply import mark
    from jobhunter.manual_batch import run

    app_id = _job_app(db, title="Обработанная")
    mark(app_id, "not_fit")
    assert run(limit=5)["sent"] == 0


# ── кнопки бота ────────────────────────────────────────────────────────

def test_bot_button_marks_and_dims_card(db):
    from jobhunter.bot import handlers
    from jobhunter.models import Application

    app_id = _job_app(db)
    upd = {"update_id": 1,
           "callback_query": {"id": "cb1", "data": "m:%d:applied" % app_id,
                              "from": {"id": 5875908057},
                              "message": {"message_id": 77,
                                          "chat": {"id": 5875908057}}}}
    acts = handlers.handle(upd)
    assert any(a["do"] == "answer" for a in acts)
    edit = [a for a in acts if a["do"] == "edit"][0]
    assert "откликнулся" in edit["text"]
    assert edit["markup"] == {"inline_keyboard": []}, "кнопки должны погаснуть"

    with db.session_scope() as sess:
        assert sess.get(Application, app_id).outcome == "applied"


def test_manual_callback_fits_telegram_limit(db):
    from jobhunter.bot.cards import CB_MAX, manual_keyboard

    kb = manual_keyboard(999999, "https://example.com/very/long/path")
    for row in kb["inline_keyboard"]:
        for btn in row:
            data = btn.get("callback_data")
            if data:
                assert len(data.encode("utf-8")) <= CB_MAX


def test_sweep_only_touches_unreachable(db):
    """Заявка со ссылкой не закрывается — по ней можно откликнуться."""
    import time
    import uuid as _uuid

    from jobhunter.manual_apply import sweep_unreachable, unreachable_ids
    from jobhunter.models import Application, ContactKind, Job, Status

    old_ts = int(time.time()) - 40 * 86400
    with db.session_scope() as sess:
        # без канала и старая — под метлу
        j1 = Job(external_uuid=str(_uuid.uuid4()), source="tg:t", title="A",
                 contact_kind=ContactKind.UNKNOWN.value, posted_at=old_ts,
                 description_raw="Python")
        # со ссылкой — не трогать
        j2 = Job(external_uuid=str(_uuid.uuid4()), source="tg:t", title="B",
                 contact_kind=ContactKind.EXTERNAL_URL.value, posted_at=old_ts,
                 contact_url="https://boards.greenhouse.io/x/jobs/1",
                 description_raw="Python")
        # без канала, но свежая — дать шанс recontact
        j3 = Job(external_uuid=str(_uuid.uuid4()), source="tg:t", title="C",
                 contact_kind=ContactKind.UNKNOWN.value,
                 posted_at=int(time.time()) - 86400, description_raw="Python")
        for j in (j1, j2, j3):
            sess.add(j)
        sess.flush()
        ids = {}
        for key, j in (("dead", j1), ("live", j2), ("fresh", j3)):
            a = Application(job_id=j.id, score=70,
                            status=Status.HANDLE_MISSING.value)
            sess.add(a)
            sess.flush()
            ids[key] = a.id

    found = unreachable_ids()
    assert ids["dead"] in found
    assert ids["live"] not in found
    assert ids["fresh"] not in found

    dry = sweep_unreachable(apply=False)
    assert dry["dry"] and dry["closed"] == 0
    with db.session_scope() as sess:
        assert sess.get(Application, ids["dead"]).status ==             Status.HANDLE_MISSING.value, "сухой прогон не меняет базу"

    res = sweep_unreachable(apply=True)
    assert res["closed"] >= 1
    with db.session_scope() as sess:
        assert sess.get(Application, ids["dead"]).status == Status.WITHDRAWN.value
        assert sess.get(Application, ids["live"]).status ==             Status.HANDLE_MISSING.value


def test_stats_separates_reachable(db):
    from jobhunter.manual_apply import stats
    st = stats()
    assert "reachable" in st and "unreachable" in st
    assert st["reachable"] + st["unreachable"] == st["total"]


def test_ingest_withdraws_unreachable_jobs(db):
    """Вакансия без единого канала не попадает в ручную очередь вовсе."""
    import uuid as _uuid

    from sqlalchemy import select

    from jobhunter.ingest.base import RawJob, save_jobs
    from jobhunter.models import Application, Job, Status

    uid = "test:no-contact:%s" % _uuid.uuid4()
    rj = RawJob(source="tg:test", external_uuid=uid, title="Python Dev",
                company="X", tag="python", content="Python, FastAPI",
                mode="full")
    save_jobs([rj], verbose=False)
    with db.session_scope() as sess:
        job = sess.scalars(select(Job).where(Job.external_uuid == uid)).first()
        app = sess.scalars(select(Application).where(
            Application.job_id == job.id)).first()
        assert app.status == Status.WITHDRAWN.value,             "без ссылки и хендла откликнуться нечем — не копим в очереди"
