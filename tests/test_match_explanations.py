"""Offline contracts for assessments, approval selection and immutable history.

Fixed vacancy and profile fixtures are intentionally independent of live data.
Integration tests use in-memory SQLite and replace rendering/report side effects.
"""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobhunter.match import explain
from jobhunter.match.role import classify
from jobhunter.match.scorer import score_job
from jobhunter.models import Application, Base, Batch, Job, SendLog, Status, utcnow
from jobhunter.profile import Profile

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "match_explanations.json").read_text(encoding="utf-8"))
PROFILE_RAW = json.loads((FIXTURES / "match_profile.json").read_text(encoding="utf-8"))
KEYS = {"version", "track", "role_family", "needs_review", "review_reasons", "matched",
        "required", "desired", "gaps", "unknowns", "vacancy_url", "role_confidence"}


@pytest.fixture
def profile(monkeypatch):
    p = Profile(deepcopy(PROFILE_RAW))
    monkeypatch.setattr(explain, "get_profile", lambda: p)
    return p


def vacancy(title="Backend Engineer", body="Required: Python", **kw):
    fields = dict(title=title, tag="", description_raw=body, salary_raw="",
                  raw_json={}, external_uuid="fixture", source="fixture", posted_at=0,
                  is_closed=False, contact_kind="email", contact_url="hr@example.org")
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_fixed_corpus_size_and_identity():
    assert len(CASES) >= 60
    assert len({c["id"] for c in CASES}) == len(CASES)
    assert {c["track"] for c in CASES} == {"backend", "ml", "architect", "additional", "unknown"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_fixed_vacancies(case, profile):
    job = vacancy(case["title"], case["body"], tag=case.get("tag", ""))
    a = explain.explain_job(job, profile)
    assert a["track"] == case["track"]
    assert a["needs_review"] is case["review"], a["review_reasons"]
    assert a["role_family"] == classify(job.title, job.tag, job.description_raw).family
    assert set(a) == KEYS
    assert a["version"] == 1
    assert 0 <= a["role_confidence"] <= 1
    assert bool(a["review_reasons"]) == a["needs_review"]
    assert json.loads(json.dumps(a)) == a
    for key in ("required", "desired"):
        if key in case:
            assert a[key] == case[key]
    for key, field in (("unknown", "unknowns"), ("gap", "gaps")):
        if key in case:
            assert case[key] in " ".join(a[field])
    valid = {e.id for e in profile.experience} | set(profile.bullet_by_id)
    for match in a["matched"]:
        assert set(match) == {"skill", "evidence_ids"}
        assert match["evidence_ids"] and set(match["evidence_ids"]) <= valid
        skill = next(s for s in profile.skills if s.canonical == match["skill"])
        assert skill.level != "none"
        assert not skill.terms & profile.forbidden_terms


def test_manual_telegram_python_engineer_contract(profile):
    a = explain.explain_job(vacancy("Python engineer",
                                   "We are hiring a Python engineer. Responsibilities: backend."))
    assert a["track"] == a["role_family"] == "backend"
    assert not a["needs_review"]


def test_aliases_are_one_skill_with_real_evidence(profile):
    a = explain.explain_job(vacancy(body="OpenAI API, OpenAI, vLLM, LangChain"), profile)
    assert a["matched"] == [{"skill": "LLM-инференс", "evidence_ids": ["b_inference", "exp_integration"]}]


def test_unknown_conditions_do_not_invent_salary_or_use_remote_default(profile):
    a = explain.explain_job(vacancy(body="Python", remote=True))
    assert "зарплата не указана" in a["unknowns"]
    assert "формат работы не указан" in a["unknowns"]
    assert "география найма / допустимые страны не указаны" in a["unknowns"]
    assert "999999" not in json.dumps(a)
    assert not a["needs_review"]


@pytest.mark.parametrize("body", ["Python. Remote worldwide", "Python. Remote from any country"])
def test_worldwide_geography_is_explicit(body, profile):
    a = explain.explain_job(vacancy(body=body))
    assert not any("география найма" in s for s in a["unknowns"])


def test_moscow_identity_does_not_prove_hiring_eligibility(profile):
    profile.identity["location"] = "Moscow, Russia"
    job = vacancy(body="Python. Remote (US only)")
    a = explain.explain_job(job)
    assert a["needs_review"]
    assert any("US only" in s for s in a["unknowns"])
    assert not any("география найма" in s for s in a["unknowns"])


@pytest.mark.parametrize("kwargs", [{"salary_raw": "300000 RUB"},
                                   {"body": "Salary: $120000. Python"}])
def test_stated_salary_not_reported_missing(kwargs, profile):
    assert "зарплата не указана" not in explain.explain_job(vacancy(**kwargs))["unknowns"]


@pytest.mark.parametrize("kwargs,expected", [
    ({"raw_json": {"url": "https://example.org/jobs/1"}}, "https://example.org/jobs/1"),
    ({"raw_json": {"absolute_url": "https://example.org/jobs/2"}}, "https://example.org/jobs/2"),
    ({"external_uuid": "tg:pythonjobs/123"}, "https://t.me/pythonjobs/123"),
    ({"contact_url": "mailto:hr@example.org"}, ""),
    ({"raw_json": {"url": "javascript:bad()"}}, ""),
])
def test_vacancy_source_url(kwargs, expected, profile):
    assert explain.explain_job(vacancy(**kwargs))["vacancy_url"] == expected


@pytest.mark.parametrize("breakdown", [None, {}, {"reason": "legacy"},
                                      {"assessment": {"version": 1}},
                                      {"assessment": "broken"}])
def test_legacy_assessment_read_does_not_backfill(breakdown, profile):
    app = SimpleNamespace(score=99, score_breakdown_json=deepcopy(breakdown),
                          status="SENT", message_body="historical")
    before = deepcopy(vars(app))
    assert explain.assessment_for(app, vacancy())["track"] == "backend"
    assert vars(app) == before


def test_saved_assessment_detached_and_preserved(profile):
    saved = explain.explain_job(vacancy())
    saved["version"] = "future-version"
    app = SimpleNamespace(score_breakdown_json={"reason": "old", "assessment": saved})
    job = vacancy("ML Engineer", "Required: training models")
    returned = explain.assessment_for(app, job)
    assert returned == saved
    returned["matched"][0]["evidence_ids"].append("not-real")
    assert "not-real" not in saved["matched"][0]["evidence_ids"]
    assert explain.approval_problem(app, job)  # stale safe snapshot is no bypass


def test_fresh_check_can_clear_stale_review_without_writing(profile):
    saved = explain.explain_job(vacancy(body="Required: AtlantisDB"))
    app = SimpleNamespace(score_breakdown_json={"assessment": saved})
    assert saved["needs_review"]
    assert explain.approval_problem(app, vacancy()) == ""
    assert saved["needs_review"]


def test_raw_profile_supported_and_forbidden_wins(profile):
    raw = deepcopy(PROFILE_RAW)
    raw["never_claim"].append({"canonical": "Python"})
    a = explain.explain_job(vacancy(), raw)
    assert not any(m["skill"] == "Python" for m in a["matched"])
    assert a["needs_review"]


def test_training_requires_specific_skill_and_linked_activity(profile):
    raw = deepcopy(PROFILE_RAW)
    raw["skills"].append(dict(id="training", canonical="Model training", aliases=[],
                              level="working", years=2, evidence_ids=["exp_integration"]))
    job = vacancy("ML Engineer", "Required: Model training")
    assert explain.explain_job(job, raw)["needs_review"]
    raw["experience"][1]["bullets"].append(dict(
        id="b_training", text_en="Trained neural models.", skills=["training"]))
    assert not explain.explain_job(job, raw)["needs_review"]
    raw["experience"][1]["bullets"][-1]["text_en"] = "Did not train neural models."
    assert explain.explain_job(job, raw)["needs_review"]


@pytest.mark.parametrize("title,tag,body,total,recommend", [
    ("Backend Engineer", "", "Python FastAPI Docker", 76.0, True),
    ("Backend Engineer", "", "Python FastAPI Docker Django SQL PostgreSQL", 100.0, True),
    ("Backend Engineer", "", "", 40.0, False),
    ("Product Manager", "product", "SQL", 27.0, False),
    ("Backend Engineer", "", "Python Terraform", 44.0, False),
    ("Backend Engineer", "", "Python FastAPI Docker. Onsite.", 76.0, False),
    ("Junior Backend Engineer", "", "Python FastAPI Docker", 76.0, False),
])
def test_numerical_formula_snapshot(title, tag, body, total, recommend, profile):
    before = score_job(title, tag, body, profile)
    explain.explain_job(vacancy(title, body, tag=tag), profile)
    after = score_job(title, tag, body, profile)
    assert vars(before) == vars(after)
    assert after.total == total
    assert after.recommend is recommend


@pytest.fixture
def db(monkeypatch, profile):
    import jobhunter.db as dbmod
    from jobhunter import report

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "_engine", engine)
    monkeypatch.setattr(dbmod, "_Session", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(report, "source_preferences", lambda **kw: {})
    monkeypatch.setattr(report, "template_preferences", lambda: {})
    yield dbmod
    engine.dispose()


def insert_app(db, title="Backend Engineer", body="Required: Python", **kw):
    with db.session_scope() as sess:
        job = Job(external_uuid="fixture-%s" % (sess.scalar(select(Job.id).order_by(Job.id.desc())) or 0),
                  title=title, description_raw=body, source="fixture", contact_kind="email",
                  contact_url="hr@example.org")
        sess.add(job)
        sess.flush()
        fields = dict(job_id=job.id, score=80, status=Status.PENDING_APPROVAL.value,
                      gate_passed=True, message_body="Original message")
        fields.update(kw)
        app = Application(**fields)
        sess.add(app)
        sess.flush()
        return app.id


def test_autoapproval_fresh_review_threshold_gate_and_owner_preserved(db):
    from jobhunter import autopilot

    ok = insert_app(db, score=55)
    review = insert_app(db, body="Required: Python and AtlantisDB", score=99,
                        score_breakdown_json={"assessment": explain.explain_job(vacancy())})
    low = insert_app(db, score=54.9)
    gate = insert_app(db, gate_passed=False)
    ambiguous = insert_app(db, title="Backend / ML Engineer", body="")
    research = insert_app(db, title="ML Engineer", body="Required: training models")
    owner = insert_app(db, body="Required: AtlantisDB", status=Status.APPROVED.value,
                       approved_at=utcnow())
    note = insert_app(db, review_note="Owner review needed")
    attempted = insert_app(db, send_attempts=1)
    log_only = insert_app(db)
    with db.session_scope() as sess:
        sess.add(SendLog(application_id=log_only, result="error"))
    assert autopilot.step_auto_approve() == 1
    with db.session_scope() as sess:
        assert sess.get(Application, ok).status == Status.APPROVED.value
        assert sess.get(Application, owner).status == Status.APPROVED.value
        for aid in (review, low, gate, ambiguous, research, note, attempted, log_only):
            assert sess.get(Application, aid).status == Status.PENDING_APPROVAL.value
        batch = sess.scalar(select(Batch))
        assert batch.approved_count == batch.planned_count == 1


def test_main_tracks_prioritized_before_limit_additional_kept(db, monkeypatch):
    from jobhunter import autopilot

    monkeypatch.setattr(autopilot, "AUTO_APPROVE_MAX_PER_RUN", 3)
    # More than the previous 4x pre-limit; a main-track row must still get in.
    extras = [insert_app(db, title="DevOps Engineer", score=100) for _ in range(15)]
    mains = [insert_app(db, title=title, score=55) for title in
             ("Backend Engineer", "ML Engineer", "Architect")]
    assert autopilot.step_auto_approve() == 3
    with db.session_scope() as sess:
        assert all(sess.get(Application, aid).status == "APPROVED" for aid in mains)
        assert all(sess.get(Application, aid).status == "PENDING_APPROVAL" for aid in extras)
        assert all(sess.get(Application, aid).score == 100 for aid in extras)
    assert autopilot.step_auto_approve() == 3  # additional is selectable, not excluded


def test_owner_manual_approval_is_not_unconditionally_blocked(db):
    from jobhunter.outreach.approve import cmd_approve

    aid = insert_app(db, body="Required: AtlantisDB")
    assert cmd_approve(ids={aid}) == 0
    with db.session_scope() as sess:
        assert sess.get(Application, aid).status == "APPROVED"


@pytest.mark.parametrize("history", [
    {"status": "SENT"}, {"status": "SENDING"}, {"status": "SEND_FAILED"},
    {"status": "SEND_FAILED_AMBIGUOUS"}, {"status": "APPROVED"},
    {"sent_at": utcnow()}, {"send_attempts": 1}, {"send_last_attempt_at": utcnow()},
    {"approved_at": utcnow()}, {"applied_at": utcnow()}, {"sending_lease_until": utcnow()},
    {"telegram_msg_id": 123}, {"log_only": True},
])
def test_prepare_never_overwrites_history(db, monkeypatch, history):
    from jobhunter import pipeline

    fields = dict(status="DISCOVERED", score=31, score_breakdown_json={"reason": "Historical"},
                  message_body="Historical text", cv_path="historical.pdf")
    fields.update({k: v for k, v in history.items() if k != "log_only"})
    aid = insert_app(db, **fields)
    with db.session_scope() as sess:
        if history.get("log_only"):
            sess.add(SendLog(application_id=aid, result="error"))
        app = sess.get(Application, aid)
        before = {column.key: deepcopy(getattr(app, column.key)) for column in Application.__table__.columns}
    def unexpected(*args, **kwargs):
        pytest.fail("historical application was re-assessed/prepared")
    monkeypatch.setattr(pipeline, "score_job", unexpected)
    monkeypatch.setattr(pipeline, "explain_job", unexpected)
    result = pipeline.prepare_application(aid)
    assert result.score == 31
    assert result.message == "Historical text"
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        assert {key: getattr(app, key) for key in before} == before


def test_pipeline_saves_assessment_and_preserves_legacy_fields(db, monkeypatch):
    from jobhunter import pipeline
    from jobhunter.tailor.gate import GateResult

    job = vacancy(body="Required: Python and AtlantisDB")
    aid = insert_app(db, body=job.description_raw, status="DISCOVERED",
                     score_breakdown_json={"unrelated": {"keep": True}})
    score = score_job(job.title, "", job.description_raw, Profile(PROFILE_RAW))
    monkeypatch.setattr(pipeline, "score_job", lambda *a, **kw: score)
    gate = GateResult(passed=True)
    monkeypatch.setattr(pipeline, "tailor", lambda *a: SimpleNamespace(
        role=classify(job.title, "", job.description_raw), ok=True, gate=gate,
        cv_slug="Backend", render={}, lang="en"))
    monkeypatch.setattr(pipeline, "render_cv", lambda *a, **k: ("fixture.pdf", "hash"))
    monkeypatch.setattr(pipeline, "verify_parsable", lambda *a: {"ok": True})
    monkeypatch.setattr(pipeline, "gen_message", lambda *a, **k: SimpleNamespace(
        ok=True, text="Fixture message", body_hash="hash", skeleton_id="fixture",
        similarity_max=0, gate=gate))
    monkeypatch.setattr(pipeline, "write_message", lambda *a, **k: SimpleNamespace(
        gate_passed=True, text="", source="template", review_done=False))
    monkeypatch.setattr(pipeline, "quality_problem", lambda text: "")
    result = pipeline.prepare_application(aid)
    assert result.status == "PENDING_APPROVAL"  # review does not delete the vacancy
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        b = app.score_breakdown_json
        assert app.score == score.total
        assert b["reason"] == score.reason
        assert b["matched"] == [t for t, _, _ in score.matched_skills]
        assert b["forbidden"] == score.forbidden_demands
        assert b["unrelated"] == {"keep": True}
        assert b["assessment"]["needs_review"]


def test_prepare_batch_prioritizes_main_tracks_without_deleting(db, monkeypatch):
    from jobhunter import pipeline

    extra = insert_app(db, title="DevOps Engineer", status="DISCOVERED")
    main = insert_app(db, title="Backend Engineer", status="DISCOVERED")
    seen = []
    def prepare(aid, **kwargs):
        seen.append(aid)
        return pipeline.Prepared(aid, "PENDING_APPROVAL", 80)
    monkeypatch.setattr(pipeline, "prepare_application", prepare)
    assert pipeline.prepare_all_discovered(limit=1)["processed"] == 1
    assert seen == [main]
    with db.session_scope() as sess:
        assert sess.get(Application, extra).status == "DISCOVERED"


def test_explicit_match_review_keeps_score_text_and_blocks_repeat(db, monkeypatch):
    from jobhunter.config import get_settings
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "1")
    get_settings.cache_clear()
    aid = insert_app(db, body="Required: Python FastAPI Docker and AtlantisDB")
    with db.session_scope() as sess:
        application = sess.get(Application, aid)
        fingerprint = explain.review_fingerprint(application, sess.get(Job, application.job_id))
    assert not explain.approve_reviewed(aid, fingerprint, True)[0]
    assert not explain.approve_reviewed(aid, fingerprint, 2)[0]
    ok, note = explain.approve_reviewed(aid, fingerprint, 1)
    assert ok, note
    assert not explain.approve_reviewed(aid, fingerprint, 1)[0]
    with db.session_scope() as sess:
        application = sess.get(Application, aid)
        assert application.status == "APPROVED" and application.sent_at is None
        assert application.score == 80 and application.message_body == "Original message"


def test_autoapproval_tie_breaks_by_freshness_then_id(db, monkeypatch):
    from jobhunter import autopilot
    monkeypatch.setattr(autopilot, "AUTO_APPROVE_MAX_PER_RUN", 1)
    fresh = insert_app(db)
    stale = insert_app(db)
    with db.session_scope() as sess:
        sess.get(Job, sess.get(Application, fresh).job_id).posted_at = int(utcnow().timestamp())
        sess.get(Job, sess.get(Application, stale).job_id).posted_at = int(utcnow().timestamp()) - 3600
    assert autopilot.step_auto_approve() == 1
    with db.session_scope() as sess:
        assert sess.get(Application, fresh).status == "APPROVED"
        assert sess.get(Application, stale).status == "PENDING_APPROVAL"
