"""Автоподача в Ashby: факты из профиля, fail-closed, ни байта в сеть в dry.

Сети нет ни в одном тесте: схема формы подменяется готовым FormSpec, а
боевой POST уходит в фейковый httpx-клиент, который только записывает
вызовы. Клиент, падающий на любом обращении, сторожит dry-режим.
"""
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "ashby.db")
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def cv_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("cv") / "Suren_Hakobyan_en.pdf"
    p.write_bytes(b"%PDF-1.4 test resume")
    return p


@pytest.fixture(autouse=True)
def bind_mailbox(db, monkeypatch):
    """Ящик SMTP = email профиля: без этого plus-привязка ответов невозможна
    и build_submission обязан отказывать (см. test_mismatched_mailbox…)."""
    from jobhunter.config import get_settings
    from jobhunter.profile import get_profile

    ident = get_profile().raw.get("identity", {}) or {}
    monkeypatch.setattr(get_settings(), "smtp_user", ident.get("email", ""))


@pytest.fixture()
def mk_app(db, cv_file):
    """Фабрика: ashby-вакансия + одобренная заявка с готовым письмом."""
    from jobhunter.models import Application, Job, Status

    def make(**over):
        jid = uuid.uuid4().hex[:12]
        with db.session_scope() as sess:
            job = Job(external_uuid="ats:ashby:acme:%s" % jid, source="ats",
                      title="Senior Python Backend Engineer",
                      company_name="Acme",
                      contact_url="https://jobs.ashbyhq.com/acme/%s" % jid)
            sess.add(job)
            sess.flush()
            app = Application(
                job_id=job.id, status=over.get("status", Status.APPROVED.value),
                score=over.get("score", 80.0),
                gate_passed=over.get("gate_passed", True),
                message_body=over.get("message_body",
                                      "Здравствуйте! Отклик на вакансию."),
                cv_path=over.get("cv_path", str(cv_file)))
            sess.add(app)
            sess.flush()
            return {"app_id": app.id, "job_id": jid}
    return make


def _fake_form(extra=()):
    """fetch_form без сети: базовый набор Ashby плюс extra-поля."""
    from jobhunter.apply.forms import FormField, FormSpec

    def fake(provider, board, job_id, http=None, timeout=None):
        spec = FormSpec(provider="ashby", board=board, job_id=str(job_id),
                        apply_url="https://jobs.ashbyhq.com/%s/%s"
                                  % (board, job_id))
        spec.fields = [
            FormField("_systemfield_name", "Full Name", "input_text", True),
            FormField("_systemfield_email", "Email", "input_text", True),
            FormField("_systemfield_phone", "Phone", "input_text", False),
            FormField("_systemfield_resume", "Resume", "input_file", True),
            *extra,
        ]
        return spec
    return fake


class _NetBanned:
    """Любое обращение к клиенту — провал: dry не имеет права ходить в сеть."""

    def __getattr__(self, name):
        raise AssertionError("сетевой вызов %s в dry-режиме" % name)


class _Resp:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _OkHttp:
    def __init__(self, body=None):
        self.calls = []
        self._body = {"success": True} if body is None else body

    def post(self, url, data=None, files=None, timeout=None):
        self.calls.append({"url": url, "data": data, "files": files})
        return _Resp(self._body)


# ── сборка пейлоада ────────────────────────────────────────────────────

def test_build_submission_fills_profile_fields(db, mk_app, monkeypatch):
    from jobhunter.apply import submit_ashby
    from jobhunter.apply.forms import FormField

    consent = FormField("q_consent",
                        "I consent to the processing of my personal data",
                        "boolean", True)
    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form((consent,)))
    ids = mk_app()
    payload, reason = submit_ashby.build_submission(ids["app_id"])
    assert payload is not None, reason
    form = json.loads(payload["data"]["applicationForm"])
    by_path = {f["path"]: f["value"] for f in form["fieldSubmissions"]}
    assert by_path["_systemfield_name"] == "Suren Hakobyan"
    assert "@" in by_path["_systemfield_email"]
    # Email — с plus-меткой заявки: ответ Ashby придёт на адрес из формы,
    # и только метка позволит почтовому циклу привязать его к заявке.
    from jobhunter.outreach.mailer import parse_reply_to
    assert parse_reply_to(by_path["_systemfield_email"]) == ids["app_id"]
    assert by_path["_systemfield_phone"].startswith("+7")
    # Файл резюме — ссылкой на multipart-часть, как требует формат Ashby.
    assert by_path["_systemfield_resume"] == payload["resume_part"]
    assert Path(payload["resume_path"]).exists()
    # Согласие на обработку данных — True: подачу санкционировал владелец.
    assert by_path["q_consent"] is True
    assert payload["data"]["jobPostingId"] == ids["job_id"]
    assert payload["data"]["organizationHostedJobsPageName"] == "acme"


def test_required_custom_question_stays_with_owner(db, mk_app, monkeypatch):
    """Нет факта — нет подачи: скрининговый вопрос не выдумывается."""
    from jobhunter.apply import submit_ashby
    from jobhunter.apply.forms import FormField
    from jobhunter.models import Application, Status

    visa = FormField("q_visa",
                     "Are you legally authorized to work in the US?",
                     "multi_value_single_select", True,
                     values=[("0", "Yes"), ("1", "No")])
    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form((visa,)))
    ids = mk_app()
    payload, reason = submit_ashby.build_submission(ids["app_id"])
    assert payload is None
    assert "authorized" in reason
    res = submit_ashby.submit(ids["app_id"], dry=False, http=_OkHttp())
    assert res.startswith("skipped:")
    with db.session_scope() as sess:
        app = sess.get(Application, ids["app_id"])
        assert app.status == Status.APPROVED.value, \
            "пропуск не должен трогать статус"


def test_screening_lookalikes_are_not_filled(db, mk_app, monkeypatch):
    """«Relocation» — не location, а селект — не факт из identity.

    Уверенно-неверный ответ закрывает компанию навсегда: город кандидата
    в вопросе о переезде и True в селекте согласия — ровно такие ответы.
    """
    from jobhunter.apply import submit_ashby
    from jobhunter.apply.forms import FormField

    reloc = FormField("q_reloc", "Are you open to relocation?",
                      "input_text", True)
    consent_sel = FormField(
        "q_consent_sel", "Do you consent to the processing of personal data?",
        "multi_value_single_select", True, values=[("0", "Yes"), ("1", "No")])
    bg_check = FormField("q_bg", "I consent to a background check",
                         "boolean", True)
    monkeypatch.setattr(submit_ashby, "fetch_form",
                        _fake_form((reloc, consent_sel, bg_check)))
    ids = mk_app()
    payload, reason = submit_ashby.build_submission(ids["app_id"])
    assert payload is None
    assert "relocation" in reason and "consent" in reason
    assert "background" in reason, \
        "согласие не на обработку данных — решение владельца"


def test_mismatched_mailbox_fails_closed(db, mk_app, monkeypatch):
    """IMAP читает другой ящик — ответ рекрутёра потеряется, не подаём."""
    from jobhunter.apply import submit_ashby
    from jobhunter.config import get_settings

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    monkeypatch.setattr(get_settings(), "smtp_user", "another@example.com")
    ids = mk_app()
    payload, reason = submit_ashby.build_submission(ids["app_id"])
    assert payload is None
    assert "ящик" in reason


# ── dry: ни байта в сеть, ни строчки в базу ────────────────────────────

def test_dry_run_makes_no_network_calls(db, mk_app, monkeypatch, capsys):
    from jobhunter.apply import submit_ashby
    from jobhunter.models import Application, SendLog, Status

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ids = mk_app()
    res = submit_ashby.submit(ids["app_id"], dry=True, http=_NetBanned())
    assert res == "ok"
    assert "applicationForm" in capsys.readouterr().out
    with db.session_scope() as sess:
        app = sess.get(Application, ids["app_id"])
        assert app.status == Status.APPROVED.value
        n = sess.scalar(select(func.count()).select_from(SendLog).where(
            SendLog.application_id == ids["app_id"])) or 0
        assert n == 0, "dry не оставляет следов в SendLog"


# ── боевая подача ──────────────────────────────────────────────────────

def test_successful_submit_moves_status_and_logs(db, mk_app, monkeypatch):
    from jobhunter.apply import submit_ashby
    from jobhunter.models import Application, Message, SendLog, Status

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ids = mk_app()
    http = _OkHttp()
    assert submit_ashby.submit(ids["app_id"], dry=False, http=http) == "ok"
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == submit_ashby.SUBMIT_URL
    assert call["data"]["jobPostingId"] == ids["job_id"]
    assert submit_ashby.RESUME_PART in call["files"]
    with db.session_scope() as sess:
        app = sess.get(Application, ids["app_id"])
        assert app.status == Status.SENT.value
        assert app.sent_at is not None
        logs = sess.scalars(select(SendLog).where(
            SendLog.application_id == ids["app_id"])).all()
        assert [l.result for l in logs] == ["ok"]
        assert logs[0].peer_id == "ashby:acme/%s" % ids["job_id"]
        msg = sess.scalars(select(Message).where(
            Message.application_id == ids["app_id"])).one()
        assert msg.direction == "out"
        assert msg.body == app.message_body


def test_repeat_submission_is_skipped(db, mk_app, monkeypatch):
    from jobhunter.apply import submit_ashby

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ids = mk_app()
    http = _OkHttp()
    assert submit_ashby.submit(ids["app_id"], dry=False, http=http) == "ok"
    res2 = submit_ashby.submit(ids["app_id"], dry=False, http=http)
    assert res2.startswith("skipped")
    assert len(http.calls) == 1, "повторная подача не должна дойти до сети"


def test_daily_limit_enforced(db, mk_app, monkeypatch):
    from jobhunter.apply import submit_ashby
    from jobhunter.config import get_settings
    from jobhunter.models import SendLog

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ids = mk_app()
    with db.session_scope() as sess:
        sess.add(SendLog(result="ok",
                         peer_id="ashby:other/%s" % uuid.uuid4().hex[:8]))
    with db.session_scope() as sess:
        used = submit_ashby._sent_today(sess)
    assert used >= 1
    monkeypatch.setattr(get_settings(), "ats_daily_limit", used)
    http = _OkHttp()
    assert submit_ashby.submit(ids["app_id"], dry=False,
                               http=http) == "skipped:daily_limit"
    assert http.calls == []


def test_rejected_submission_returns_to_owner(db, mk_app, monkeypatch):
    """success=false: доставки точно не было, заявка снова у владельца."""
    from jobhunter.apply import submit_ashby
    from jobhunter.models import Application, SendLog, Status

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ids = mk_app()
    http = _OkHttp(body={"success": False, "errors": ["invalid phone"]})
    res = submit_ashby.submit(ids["app_id"], dry=False, http=http)
    assert res == "error:AshbyRejected"
    with db.session_scope() as sess:
        app = sess.get(Application, ids["app_id"])
        assert app.status == Status.SEND_FAILED.value,             "точный отказ формы -> SEND_FAILED, повтор только с ведома владельца"
        assert app.sent_at is None
        assert app.send_error_class == "AshbyRejected"
        logs = sess.scalars(select(SendLog).where(
            SendLog.application_id == ids["app_id"])).all()
        assert [l.result for l in logs] == ["error"]


def test_network_failure_is_ambiguous_and_blocks_retry(db, mk_app,
                                                       monkeypatch):
    """Обрыв в сети: доказать «не дошло» нельзя — никакого повтора вслепую.

    Захват статуса и ключ идемпотентности пишутся ДО сетевого вызова,
    поэтому даже упавшая посреди POST подача не вернётся в очередь сама —
    только явным решением владельца (requeue_ambiguous).
    """
    from jobhunter.apply import submit_ashby
    from jobhunter.models import Application, Status

    class _BoomHttp:
        calls = 0

        def post(self, url, data=None, files=None, timeout=None):
            self.calls += 1
            raise ConnectionError("обрыв после отправки запроса")

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ids = mk_app()
    http = _BoomHttp()
    res = submit_ashby.submit(ids["app_id"], dry=False, http=http)
    assert res == "error:ConnectionError"
    with db.session_scope() as sess:
        app = sess.get(Application, ids["app_id"])
        assert app.status == Status.SEND_FAILED_AMBIGUOUS.value
        assert app.send_idempotency_key.startswith("ashby:")
    res2 = submit_ashby.submit(ids["app_id"], dry=False, http=http)
    assert res2.startswith("skipped:")
    assert http.calls == 1, "повтор после неоднозначного сбоя — только владелец"


# ── очередь ────────────────────────────────────────────────────────────

def test_run_skips_candidates_without_letter(db, mk_app, monkeypatch):
    """Письмо готовит другой шаг конвейера: нет письма — нет кандидата."""
    from jobhunter.apply import submit_ashby
    from jobhunter.models import Application, Status

    monkeypatch.setattr(submit_ashby, "fetch_form", _fake_form())
    ready = mk_app(score=90.0)
    empty = mk_app(score=88.0, message_body="")
    stats = submit_ashby.run(limit=50, dry=True)
    assert stats["ok"] >= 1
    assert stats["skipped"] >= 1
    with db.session_scope() as sess:
        for ids in (ready, empty):
            app = sess.get(Application, ids["app_id"])
            assert app.status == Status.APPROVED.value, \
                "dry-прогон очереди ничего не меняет"
