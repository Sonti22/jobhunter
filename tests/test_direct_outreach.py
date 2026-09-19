"""Прямые письма руководителям и рефералам: гейты, потолки, предохранитель.

Владелец выбрал автоотправку без просмотра (18.09), поэтому всё, что стоит между
«нашли адрес» и «ушло», закреплено здесь тестами.
"""
import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

TEAM_PAGE = """<html><body><h3>Anna Lee</h3><p>Co-founder & CEO</p>
<a href="mailto:anna@acme.io">anna@acme.io</a></body></html>"""
JD = ("Acme | Senior Backend Engineer | REMOTE\nWe're hiring. More: https://acme.io/about\n"
      "Requirements: Python, FastAPI, PostgreSQL, Docker. Apply: https://jobs.ashbyhq.com/acme/1")


class FakeFetcher:
    """Вместо сети: страницы по URL. robots и паузы тут не нужны."""
    http = None
    throttle = 0

    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kw):
        return self.pages.get(url, "")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "direct.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_PATH", str(tmp_path / "STOP"))
    monkeypatch.setenv("CV_OUT", str(tmp_path / "cv"))
    monkeypatch.setenv("BASE_CV_PATH", "")
    monkeypatch.setenv("DIRECT_ENABLED", "true")
    monkeypatch.setenv("SMTP_USER", "suren@gmail.com")
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


def _vacancy(db, company="Acme", body=JD, kind="external_url", url="https://jobs.ashbyhq.com/acme/1"):
    from jobhunter.models import Application, Job, utcnow
    with db.session_scope() as sess:
        job = Job(external_uuid=str(random.random()), source="ats:ashby", title="Senior Backend Engineer",
                  company_name=company, description_raw=body, contact_kind=kind, contact_url=url,
                  last_seen_at=utcnow())
        sess.add(job)
        sess.flush()
        sess.add(Application(job_id=job.id, status="HANDLE_MISSING", score=80))
        return job.id


def _direct_app(db, email, status="APPROVED", score=70.0, company="Acme", **kw):
    from jobhunter.models import Application, ContactKind, Employer, Job
    with db.session_scope() as sess:
        emp = Employer(handle_norm=email, handle_kind=ContactKind.EMAIL.value)
        sess.add(emp)
        sess.flush()
        job = Job(external_uuid="direct:" + email, source="direct:exec", title="Platform Engineer",
                  company_name=company, description_raw="Прямое письмо (без вакансии).",
                  contact_kind=ContactKind.EMAIL.value, contact_url=email,
                  raw_json={"email_source_url": "https://%s/team" % email.split("@")[1]})
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, employer_id=emp.id, status=status, score=score,
                          gate_passed=True, cv_lang="en", message_body="Hi, my CV is attached.", **kw)
        sess.add(app)
        sess.flush()
        return app.id


def _log(db, app_id, result, days_ago=0):
    from jobhunter.models import SendLog
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    with db.session_scope() as sess:
        sess.add(SendLog(application_id=app_id, result=result, peer_id="x@acme.io", attempted_at=at))


def test_discover_prepare_and_auto_approve(db):
    from jobhunter.models import Application, Job
    from jobhunter.outreach import direct
    _vacancy(db)
    fetcher = FakeFetcher({"https://acme.io/team": TEAM_PAGE})
    stats = direct.discover(limit=1, fetcher=fetcher)
    assert stats["created"] == 1
    with db.session_scope() as sess:
        job = sess.scalar(select(Job).where(Job.source == "direct:exec"))
        assert job.contact_url == "anna@acme.io" and job.title == "Senior Backend Engineer"
        assert job.raw_json["email_source_url"] == "https://acme.io/team"
        app_id = sess.scalar(select(Application.id).where(Application.job_id == job.id))
    assert direct.prepare_one(app_id, fetcher=fetcher) == "одобрено"
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == "APPROVED" and app.gate_passed and app.cv_lang == "en" and app.cv_path
        assert "reply “no”" in app.message_body and "Acme" in app.message_body
        assert "applying" not in app.message_body            # бот не знает, подавался ли владелец
    # вторая попытка найти адресата в той же компании ничего не заводит
    assert direct.discover(limit=1, fetcher=fetcher)["created"] == 0


def test_address_no_longer_published_is_never_sent(db):
    from jobhunter.models import Application
    from jobhunter.outreach import direct
    _vacancy(db)
    direct.discover(limit=1, fetcher=FakeFetcher({"https://acme.io/team": TEAM_PAGE}))
    with db.session_scope() as sess:
        app_id = sess.scalar(select(Application.id).where(Application.status == "DISCOVERED"))
    verdict = direct.prepare_one(app_id, fetcher=FakeFetcher({"https://acme.io/team": "<p>team</p>"}))
    assert verdict.startswith("закрыто")
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == "WITHDRAWN"


def test_company_or_person_named_like_a_technology_passes_truth_gate(db):
    """Письмо в Kong падало на «kong»: гейт принял название компании за навык (18.09)."""
    from jobhunter.tailor.direct_letter import compose
    letter = compose("exec", company="Kong", person="Ruby Stone", lang="en", seed="a")
    assert letter.ok and "Kong" in letter.text and "Ruby" in letter.text
    # а приписанный себе навык гейт по-прежнему ловит: маскируется только адресат
    from jobhunter.tailor.gate import DocModel, check
    bad = check(DocModel(lang="en", kind="message", rendered_bullets=[],
                         free_text="I have 5 years of production Kong and Haskell experience."))
    assert not bad.passed


def test_language_repair_leaves_direct_letters_alone(db):
    """19.09: ручная подготовка сбросила все 16 готовых писем — описание у них русское
    служебное, а письмо английское, и «исправление языка» считало это ошибкой."""
    from jobhunter.models import Application
    from jobhunter.repair_queue import reset_email_language
    app_id = _direct_app(db, "marco@konghq.com", status="APPROVED", company="Kong")
    assert reset_email_language(dry=False).get("сброшено", 0) == 0
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == "APPROVED" and app.message_body and app.cv_lang == "en"


def test_lowercase_company_from_github_login_is_capitalised(db):
    from jobhunter.ingest import people
    from jobhunter.models import Job
    from jobhunter.outreach import direct
    with db.session_scope() as sess:
        for email, company in (("cto@inato.com", "inato"), ("cto@konghq.com", "Kong"),
                               ("a@eye2gene.com", "eye2gene")):
            direct._create(sess, people.Contact(email=email, kind=people.EXEC, company=company,
                                                source_url="https://api.github.com/users/x"))
    with db.session_scope() as sess:
        names = sorted(n for n, in sess.execute(select(Job.company_name).where(Job.source == "direct:exec")))
        assert names == ["Eye2gene", "Inato", "Kong"]


def test_general_inbox_waits_for_owner(db):
    """hello@/info@ читает поддержка: такое письмо само не уходит (сухой прогон 18.09)."""
    from jobhunter.models import Application
    from jobhunter.outreach import direct
    _vacancy(db)
    fetcher = FakeFetcher({"https://acme.io/contact":
                           '<p>Say hi: <a href="mailto:hello@acme.io">hello@acme.io</a></p>'})
    assert direct.discover(limit=1, fetcher=fetcher)["created"] == 1
    with db.session_scope() as sess:
        app_id = sess.scalar(select(Application.id).where(Application.status == "DISCOVERED"))
    assert direct.prepare_one(app_id, fetcher=fetcher).startswith("удержано: общий ящик")
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == "PENDING_APPROVAL"


def test_irrelevant_or_onsite_company_is_not_a_target(db):
    from jobhunter.outreach import direct
    _vacancy(db, company="Sales Inc", body="Account Executive. https://salesinc.com Quota, cold calls.")
    _vacancy(db, company="Office Ltd", body="Backend Engineer, Python. https://officeltd.com "
                                            "Remote: no. Office in Berlin only.")
    with db.session_scope() as sess:
        assert direct.company_targets(sess) == []


def test_regular_auto_approval_never_touches_direct_letters(db):
    from jobhunter import autopilot
    from jobhunter.models import Application
    app_id = _direct_app(db, "ceo@held.io", status="PENDING_APPROVAL", score=95.0)
    autopilot.step_auto_approve()
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == "PENDING_APPROVAL"


def test_rampup_then_full_limit(db):
    from jobhunter.outreach import direct
    a = _direct_app(db, "a@one.io", status="AWAITING_REPLY")
    with db.session_scope() as sess:
        assert direct.daily_limit(sess) == 5 and direct.room(sess) == 5
    _log(db, a, "ok", days_ago=1)
    _log(db, a, "ok", days_ago=2)
    with db.session_scope() as sess:
        assert direct.daily_limit(sess) == 10
    _log(db, a, "ok", days_ago=0)
    with db.session_scope() as sess:
        assert direct.room(sess) == 9


def test_two_bounces_pause_direct_but_not_all_mail(db):
    from jobhunter.outreach import direct, policy
    a = _direct_app(db, "a@one.io", status="AWAITING_REPLY")
    _log(db, a, "bounce")
    with db.session_scope() as sess:
        assert direct.room(sess) == 5                         # одна отбивка — ещё не пауза
    _log(db, a, "bounce")
    with db.session_scope() as sess:
        assert direct.room(sess) == 0
    with db.session_scope() as sess:
        assert "отбивок" in direct.paused_reason(sess)
        assert policy.can_send_email(sess).allowed            # общая почта работает
    direct.resume()
    assert not direct.is_paused()


def test_mailer_sends_vacancies_first_and_caps_direct(db, monkeypatch):
    from jobhunter.models import Application, ContactKind, Job
    from jobhunter.outreach import mailer
    monkeypatch.setenv("DIRECT_RAMPUP_LIMIT", "2")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    for i in range(4):
        _direct_app(db, "ceo@corp%d.io" % i, score=99.0)
    with db.session_scope() as sess:
        job = Job(external_uuid="v1", source="hn", title="Backend Engineer", company_name="V",
                  description_raw="We are hiring a backend engineer. Remote. jobs@vacancy.dev",
                  contact_kind=ContactKind.EMAIL.value, contact_url="jobs@vacancy.dev")
        sess.add(job)
        sess.flush()
        sess.add(Application(job_id=job.id, status="APPROVED", score=60, gate_passed=True,
                             cv_lang="en", message_body="Hello"))
    batch = mailer.pick_batch(10)
    assert batch[0]["email"] == "jobs@vacancy.dev"            # отклик на вакансию — первым
    assert len([b for b in batch if b["email"].startswith("ceo@")]) == 2


def test_subject_is_personal_not_an_application_form(db):
    from jobhunter.models import Job
    from jobhunter.outreach.mailer import _subject
    job = Job(source="direct:exec", title="Platform Engineer", company_name="Acme")
    assert _subject(job, "en") == "Platform Engineer at Acme — Suren Hakobyan (backend / tech lead, 7+ yrs)"
    assert "Application:" not in _subject(Job(source="direct:referral", company_name="BigCo"), "en")


def test_reply_to_direct_letter_goes_to_owner_only(db, monkeypatch):
    from jobhunter.convo import engine
    from jobhunter.models import OwnerRequest

    async def must_not_send(*a, **kw):
        pytest.fail("автоответ руководителю недопустим")
    monkeypatch.setattr(engine, "send_reply", must_not_send)
    monkeypatch.setattr(engine, "within_reply_window", lambda: True)
    app_id = _direct_app(db, "anna@acme.io", status="AWAITING_REPLY")
    verdict = asyncio.run(engine.handle_message(None, app_id, "Thanks! Please send your CV"))
    assert verdict.startswith("эскалация")
    with db.session_scope() as sess:
        assert "прямое письмо" in sess.scalar(select(OwnerRequest)).payload_json["reason"]


def test_followup_waits_a_week_for_direct(db):
    from jobhunter.models import Application, utcnow
    from jobhunter.outreach import followup
    early = _direct_app(db, "a@one.io", status="AWAITING_REPLY")
    late = _direct_app(db, "b@two.io", status="AWAITING_REPLY", company="Two")
    with db.session_scope() as sess:
        sess.get(Application, early).sent_at = utcnow() - timedelta(days=4)
        sess.get(Application, late).sent_at = utcnow() - timedelta(days=8)
    with db.session_scope() as sess:
        assert [a.id for a, _, _ in followup.due(sess)] == [late]


def test_catch_up_prepares_direct_before_email():
    from jobhunter.autopilot import email_after_approve
    assert email_after_approve(["ingest", "approve", "direct", "manual_prep"]) == \
        ["ingest", "approve", "direct", "email", "manual_prep"]


def test_owner_can_block_company_and_screen_renders(db):
    from jobhunter.bot import screens
    from jobhunter.models import Application, Employer
    from jobhunter.outreach import direct
    app_id = _direct_app(db, "ceo@blocked.io", status="PENDING_APPROVAL", company="Blocked",
                         review_note="прямое письмо: письмо слишком похоже на соседние")
    text, kb = screens.direct()
    assert "ПРЯМЫЕ ПИСЬМА" in text and "Blocked" in text
    flat = [b["callback_data"] for row in kb["inline_keyboard"] for b in row if "callback_data" in b]
    assert "q:ddnc%d" % app_id in flat and "q:dappr%d" % app_id in flat and "q:dpause" in flat
    assert direct.block_company(app_id).startswith("компания в стоп-листе")
    with db.session_scope() as sess:
        app = sess.get(Application, app_id)
        assert app.status == "WITHDRAWN" and sess.get(Employer, app.employer_id).do_not_contact


def test_disabled_source_is_neither_sent_nor_collected(db, monkeypatch):
    """Проверка 19.09: trudvsem — 12 писем и ни одного ответа; ergodotisi — всё отсеяно."""
    from jobhunter import autopilot
    from jobhunter.models import Application, ContactKind, Job
    from jobhunter.outreach import mailer
    assert {"trudvsem", "ergodotisi"} <= autopilot.disabled_sources()
    with db.session_scope() as sess:
        for src, addr in (("trudvsem", "hr@zavod.ru"), ("hn", "jobs@startup.dev")):
            job = Job(external_uuid=src + "1", source=src, title="Backend Engineer", company_name=src,
                      description_raw="We are hiring a backend engineer. Remote.",
                      contact_kind=ContactKind.EMAIL.value, contact_url=addr)
            sess.add(job)
            sess.flush()
            sess.add(Application(job_id=job.id, status="APPROVED", score=70, gate_passed=True,
                                 cv_lang="en", message_body="Hello"))
    assert [b["email"] for b in mailer.pick_batch(10)] == ["jobs@startup.dev"]
    monkeypatch.setenv("DISABLED_SOURCES", "")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    assert autopilot.disabled_sources() == set()
    assert len(mailer.pick_batch(10)) == 2
