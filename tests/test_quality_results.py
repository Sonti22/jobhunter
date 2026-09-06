"""Quality analytics use explicit evidence and a single, time-bounded send cohort."""
from __future__ import annotations

import asyncio
import types
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from jobhunter import report, results
from jobhunter.models import Application, Job, Message, ResultEvent, SendLog, Status

NOW = datetime(2026, 9, 6, 12)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from jobhunter import db as dbmod
    from jobhunter.config import get_settings

    monkeypatch.setenv("DB_PATH", str(tmp_path / "quality.db"))
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "77")
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("GCAL_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(dbmod, "_engine", None)
    monkeypatch.setattr(dbmod, "_Session", None)
    monkeypatch.setattr(results, "_now", lambda: NOW)
    yield dbmod
    if dbmod._engine is not None:
        dbmod._engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
def make_app(db):
    def make(*, source="tg:jobs", template="tested", track="income", **fields):
        with db.session_scope() as sess:
            job = Job(external_uuid=uuid.uuid4().hex, source=source,
                      title="Backend engineer", company_name="Example")
            sess.add(job)
            sess.flush()
            values = {"job_id": job.id, "status": Status.AWAITING_REPLY.value,
                      "sent_at": NOW - timedelta(days=30), "message_skeleton_id": template,
                      "score_breakdown_json": {"assessment": {"track": track}}}
            values.update(fields)
            app = Application(**values)
            sess.add(app)
            sess.flush()
            return app.id
    return make


def event(db, app_id, kind, *, source="owner", occurred_at=None, key=None):
    with db.session_scope() as sess:
        return results.record_event(sess, app_id, kind, source,
                                    key or "%s:%s:%s" % (source, app_id, kind), occurred_at)


def test_record_event_deduplicates_without_rolling_back_caller(db, make_app):
    app_id = make_app()
    when = NOW.replace(tzinfo=timezone(timedelta(hours=3)))
    with db.session_scope() as sess:
        sess.get(Application, app_id).message_body = "unrelated caller edit"
        assert results.record_event(sess, app_id, "interested", "classifier", "same", when,
                                    {"message_id": 10})
        assert not results.record_event(sess, app_id, "interested", "classifier", "same")
        assert results.record_event(sess, app_id, "cv_requested", "classifier", "another")
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 2
        saved = sess.scalar(select(ResultEvent).where(ResultEvent.event_key == "same"))
        assert saved.occurred_at == NOW - timedelta(hours=3)
        assert saved.details_json == {"message_id": 10}
        assert sess.get(Application, app_id).message_body == "unrelated caller edit"
        with pytest.raises(ValueError, match="different fact"):
            results.record_event(sess, app_id, "offer", "owner", "same")
        assert sess.scalar(select(func.count(ResultEvent.id))) == 2


def test_record_event_stays_in_callers_transaction(db, make_app):
    app_id = make_app()
    with pytest.raises(RuntimeError):
        with db.session_scope() as sess:
            results.record_event(sess, app_id, "offer", "owner", "rollback")
            raise RuntimeError("abort outer transaction")
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 0


def test_owner_semantic_dedup_audit_and_no_invented_milestones(db, make_app):
    app_id = make_app()
    assert results.owner_record(app_id, "offer", 77, "click-1")[0]
    assert results.owner_record(app_id, "offer", 77, "click-2")[0]
    with db.session_scope() as sess:
        saved = sess.scalars(select(ResultEvent)).one()
        assert saved.kind == "offer" and saved.source == "owner"
        assert saved.occurred_at is None and saved.recorded_at == NOW
        assert saved.details_json["actor_id"] == 77
        assert saved.details_json["request_key"] == "click-1"
        assert sess.get(Application, app_id).status == Status.OFFER.value
    counts = results.aggregate()["counts"]
    assert counts["offer"] == 1
    assert counts["interview_scheduled"] == counts["interview_done"] == 0


def test_owner_concurrent_clicks_record_one_milestone(db, make_app):
    app_id = make_app()
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = list(pool.map(lambda key: results.owner_record(app_id, "interested", 77, key),
                              ["first", "second"]))
    assert all(ok for ok, _ in calls)
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 1


@pytest.mark.parametrize("fields", [
    {"sent_at": None}, {"sent_at": NOW + timedelta(seconds=1)},
    {"status": Status.APPROVED.value}, {"status": Status.WITHDRAWN.value},
    {"status": Status.REJECTED_BY_EMPLOYER.value}, {"outcome": "manual_tg_ready"},
])
def test_owner_requires_sent_active_application(db, make_app, fields):
    assert not results.owner_record(make_app(**fields), "interested", 77, "click")[0]
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 0


def test_owner_authorization_and_invalid_input(db, make_app, monkeypatch):
    from jobhunter.config import get_settings
    app_id = make_app()
    assert not results.owner_record(app_id, "interested", 88, "click")[0]
    assert not results.owner_record(app_id, "interested", True, "click")[0]
    assert not results.owner_record(app_id, "reply", 77, "click")[0]
    assert not results.owner_record(app_id, "interested", 77, "")[0]
    assert not results.owner_record(999999, "interested", 77, "click")[0]
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "")
    get_settings.cache_clear()
    assert not results.owner_record(app_id, "interested", 77, "click")[0]
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 0


def test_refusals_and_ordinary_replies_are_not_positive_preferences(db, make_app):
    for i in range(20):
        app_id = make_app(first_reply_at=NOW - timedelta(days=3))
        if i % 2:
            event(db, app_id, "rejected")
        else:
            with db.session_scope() as sess:
                sess.add(Message(application_id=app_id, direction="in", body="Thanks",
                                 received_at=NOW - timedelta(days=3),
                                 classifier_label="ack", classifier_confidence=0.95))
    data = results.aggregate()
    assert data["counts"]["replied"] == 20
    assert data["counts"]["positive"] == data["counts"]["interested"] == 0
    assert data["categories"]["rejected"] == data["categories"]["other_reply"] == 10
    assert report.source_preferences(min_sent=5) == {"tg": 0.0}
    assert report.template_preferences(min_sent=5) == {"tested": 0.0}
    row = report.by_source()[0]
    assert row["rate"] == 100.0 and row["quality_rate"] == 0.0
    assert row["sent"] == row["replied"] == 20


def test_interview_history_persists_after_refusal(db, make_app):
    app_id = make_app(status=Status.REJECTED_BY_EMPLOYER.value)
    event(db, app_id, "interview_scheduled", source="calendar",
          occurred_at=NOW - timedelta(days=20))
    event(db, app_id, "interview_done", occurred_at=NOW - timedelta(days=18))
    event(db, app_id, "rejected", occurred_at=NOW - timedelta(days=16))
    data = results.aggregate()
    assert data["categories"]["rejected"] == 1
    assert data["milestones"]["interview_scheduled"] == 1
    assert data["milestones"]["interview_done"] == 1
    assert data["milestones"]["rejected"] == 1
    assert data["counts"]["positive"] == 1
    assert {e["kind"] for e in data["history"]} == {
        "interview_scheduled", "interview_done", "rejected"}
    assert report.conversion_metrics()["interviews"] == 1


def test_legacy_evidence_never_reconstructs_held_interview(db, make_app):
    make_app(status=Status.OFFER.value, interview_at_utc=NOW - timedelta(days=10))
    make_app(status=Status.INTERVIEW_DONE.value)
    data = results.aggregate()
    assert data["counts"]["interview_scheduled"] == data["counts"]["offer"] == 1
    assert data["counts"]["interview_done"] == 0
    assert all(e["source"] == "legacy" and e["historical"] for e in data["history"])
    assert all(e["occurred_at"] is None for e in data["history"])
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 0


def test_preferences_require_twenty_mature_observations_even_if_callers_pass_five(db, make_app):
    for _ in range(19):
        event(db, make_app(), "interested")
    event(db, make_app(sent_at=NOW - timedelta(days=14) + timedelta(microseconds=1)), "offer")
    event(db, make_app(sent_at=NOW - timedelta(days=90, microseconds=1)), "offer")
    assert report.template_preferences(min_sent=5) == {}
    assert report.source_preferences(min_sent=5) == {}
    assert report.source_priority_penalty("tg:jobs", report.source_preferences(5)) == 0.0
    event(db, make_app(sent_at=NOW - timedelta(days=14)), "cv_requested")
    assert report.template_preferences(min_sent=5) == {"tested": 0.95}
    assert report.source_preferences(min_sent=5) == {"tg": 0.95}
    assert report.source_preferences(min_sent=21) == {}
    assert results.aggregate()["group_quality"]["by_source"][0]["sent"] == 20


def test_send_cohort_includes_exact_boundaries_and_excludes_old_recent_replies(db, make_app):
    oldest = make_app(sent_at=NOW - timedelta(days=90), first_reply_at=NOW - timedelta(days=1))
    newest = make_app(sent_at=NOW, first_reply_at=NOW)
    make_app(sent_at=NOW - timedelta(days=90, microseconds=1), first_reply_at=NOW)
    make_app(sent_at=NOW + timedelta(microseconds=1), first_reply_at=NOW)
    make_app(sent_at=None, first_reply_at=NOW)
    data = results.aggregate()
    assert {a["id"] for a in data["applications"]} == {oldest, newest}
    assert data["counts"]["sent"] == data["counts"]["replied"] == 2
    conversion = report.conversion_metrics()
    assert conversion["sent"] == conversion["replied"] == 2
    assert conversion["reply_rate"] == 100.0
    assert conversion["avg_response_hours"] == 89 * 24 / 2
    assert conversion["responses_measured"] == 2


def test_outcome_and_reply_time_boundaries(db, make_app):
    app_id = make_app(first_reply_at=NOW + timedelta(seconds=1))
    event(db, app_id, "offer", occurred_at=NOW + timedelta(microseconds=1))
    event(db, app_id, "interested", occurred_at=NOW - timedelta(days=31))
    event(db, app_id, "cv_requested", occurred_at=NOW)
    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="in", received_at=NOW + timedelta(days=1),
                         classifier_label="ask_call", classifier_confidence=0.95))
    data = results.aggregate()
    assert data["counts"]["offer"] == data["counts"]["interested"] == 0
    assert data["counts"]["cv_requested"] == 1
    assert data["counts"]["replied"] == 0


def test_manual_send_reports_are_separate_from_transport_and_unknown_template(db, make_app):
    manual = make_app(send_channel="telegram_manual", outcome="manual_tg_sent",
                      message_body="a prepared draft", template="not-proven")
    confirmed = make_app(send_channel="email")
    unknown = make_app(send_channel="email")
    with db.session_scope() as sess:
        # A later manual-thread reply cannot prove the initial draft was sent.
        sess.add(Message(application_id=manual, direction="out", body="later reply",
                         sent_at=NOW - timedelta(days=1), telegram_msg_id=20))
        sess.add(Message(application_id=confirmed, direction="out", email_message_id="<sent>",
                         sent_at=NOW - timedelta(days=30)))
        # SMTP reserves a message id before trying the transport: no sent_at.
        sess.add(Message(application_id=unknown, direction="out", email_message_id="<reserved>"))
        sess.add(SendLog(application_id=unknown, result="ambiguous", attempted_at=NOW))
    data = results.aggregate()
    assert data["delivery"] == {"owner_reported": 1, "transport_confirmed": 1, "unknown": 1}
    row = next(a for a in data["applications"] if a["id"] == manual)
    assert row["template"] is None and not row["template_known"]
    assert {r["key"] for r in report.by_template()} == {"tested"}
    assert report.by_source()[0]["sent"] == 3
    assert results.owner_record(manual, "interested", 77, "manual-result")[0]
    assert results.aggregate()["delivery"]["transport_confirmed"] == 1


def test_unknown_manual_templates_never_enter_automatic_comparisons(db, make_app):
    for _ in range(20):
        event(db, make_app(outcome="manual_tg_sent", send_channel="telegram_manual"), "interested")
    assert report.template_preferences(5) == {}
    assert report.by_template() == []
    assert report.source_preferences(5) == {"tg": 1.0}


def test_saved_track_wins_and_historical_fallback_is_read_only(db, make_app, monkeypatch):
    def fallback(app, job):
        assert app.score_breakdown_json == {}
        return {"track": "growth"}
    monkeypatch.setattr("jobhunter.match.explain.assessment_for", fallback)
    saved = make_app(track="income")
    inferred = make_app(score_breakdown_json={})
    data = results.aggregate(track="growth", limit=5)
    assert data["sent"] == 1 and data["applications"][0]["id"] == inferred
    assert data["applications"][0]["track_source"] == "inferred"
    assert data["applications"][0]["track_inferred"]
    assert data["by_track"]["income"]["sent"] == data["by_track"]["growth"]["sent"] == 1
    with db.session_scope() as sess:
        assert sess.get(Application, inferred).score_breakdown_json == {}
        assert sess.get(Application, saved).score_breakdown_json["assessment"]["track"] == "income"


def test_results_pagination_keeps_totals_and_history_tied_to_page(db, make_app):
    ids = [make_app() for _ in range(7)]
    for app_id in ids:
        event(db, app_id, "interested")
    first = results.aggregate(limit=5)
    second = results.aggregate(offset=5, limit=5)
    assert first["total"] == second["total"] == first["sent"] == 7
    assert first["has_more"] and not second["has_more"]
    assert [row["id"] for row in first["applications"]] == ids[::-1][:5]
    assert [row["id"] for row in second["applications"]] == ids[::-1][5:]
    assert {e["application_id"] for e in second["history"]} == set(ids[:2])
    assert sum(first["categories"].values()) == 7


def test_final_classifier_evidence_overrides_positive_regex_and_does_not_duplicate(db, make_app):
    app_id = make_app()
    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="in", received_at=NOW,
                         classifier_label="ask_call", classifier_confidence=0.95,
                         llm_label="rejection", llm_confidence=0.99))
    assert results.aggregate()["counts"]["positive"] == 0
    with db.session_scope() as sess:
        msg = Message(application_id=app_id, direction="in", received_at=NOW,
                      classifier_label="ask_cv", classifier_confidence=0.95)
        sess.add(msg)
        sess.flush()
        results.record_event(sess, app_id, "cv_requested", "classifier",
                             "message:%d:cv_requested" % msg.id, NOW, {"message_id": msg.id})
    assert len(results.aggregate()["history"]) == 1


def test_engine_records_verified_refusal_once_without_intermediate_results(db, make_app, monkeypatch):
    from jobhunter.convo import engine
    app_id = make_app()
    text = "Спасибо, но мы выбрали другого кандидата."
    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="in", body=text, received_at=NOW,
                         classifier_label="rejection", classifier_confidence=0.95))
    monkeypatch.setattr("jobhunter.convo.verify.verify_intent", lambda *a: types.SimpleNamespace(
        label="rejection", confidence=0.99, reason="confirmed"))
    for _ in range(2):
        assert "подтверждён" in asyncio.run(engine.handle_message(None, app_id, text))
    with db.session_scope() as sess:
        saved = sess.scalars(select(ResultEvent)).one()
        assert saved.kind == "rejected" and saved.source == "classifier"
        assert saved.occurred_at == NOW
    assert results.aggregate()["counts"]["positive"] == 0


@pytest.mark.parametrize("dry", [False, True])
def test_engine_observes_cv_request_without_requiring_outbound_send(db, make_app, monkeypatch, dry):
    from jobhunter.convo import engine
    app_id = make_app(status=Status.NEEDS_HUMAN.value)
    text = "Пришлите, пожалуйста, резюме"
    with db.session_scope() as sess:
        sess.add(Message(application_id=app_id, direction="in", body=text, received_at=NOW,
                         classifier_label="ask_cv", classifier_confidence=0.95))
    # The human latch prevents a send, but does not erase the recruiter's request.
    for _ in range(2):
        asyncio.run(engine.handle_message(None, app_id, text, dry=dry))
    with db.session_scope() as sess:
        rows = sess.scalars(select(ResultEvent)).all()
        assert [row.kind for row in rows] == ([] if dry else ["cv_requested"])


def test_offer_classifier_signal_is_not_owner_confirmed_offer(db, make_app):
    aid = make_app()
    with db.session_scope() as sess:
        sess.add(Message(application_id=aid, direction="in", received_at=NOW,
                         classifier_label="offer", classifier_confidence=0.99))
    data = results.aggregate()
    assert data["counts"]["offer"] == 0 and data["counts"]["interested"] == 1
    assert results.owner_record(aid, "offer", 77, "confirm")[0]
    assert results.aggregate()["counts"]["offer"] == 1


def test_cv_only_does_not_prefer_template_or_source(db, make_app):
    for _ in range(20):
        event(db, make_app(), "cv_requested", source="classifier")
    assert results.aggregate()["counts"]["cv_requested"] == 20
    assert report.template_preferences(5) == {"tested": 0.0}
    assert report.source_preferences(5) == {"tg": 0.0}


def test_old_application_history_outside_cohort_is_still_readable(db, make_app):
    aid = make_app(sent_at=NOW - timedelta(days=100))
    event(db, aid, "interview_done")
    assert results.aggregate()["sent"] == 0
    assert [e["kind"] for e in results.application_history(aid)] == ["interview_done"]
