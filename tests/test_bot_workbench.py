"""Owner workbench integration; isolated DB, no real transports."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.test_manual_telegram import db as db
from tests.test_manual_telegram import make_app


def message(text, owner=1, chat=1):
    return {"message": {"message_id": 777, "text": text,
                        "from": {"id": owner}, "chat": {"id": chat}}}


def button(data, owner=1, chat=1):
    return {"callback_query": {"id": "test-callback", "data": data,
                              "from": {"id": owner},
                              "message": {"message_id": 778, "chat": {"id": chat}}}}


def manual_dialogue(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, Message, utcnow
    aid = make_app(db)
    mt.issue(deliver=False)
    assert mt.mark(aid, "sent")[0]
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        app.status = "NEEDS_HUMAN"
        app.last_inbound_at = utcnow()
        sess.add(Message(application_id=aid, direction="in", body="Когда удобно обсудить?",
                         received_at=utcnow(), processing_pending=True))
    return aid


def test_filter_preserves_current_and_survives_state_reload(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, Job
    first = make_app(db)
    second = make_app(db, handle="second_recruiter")
    with db.session_scope() as sess:
        job = sess.get(Job, sess.get(Application, second).job_id)
        job.title = "ML Engineer"
        job.description_raw = "We are hiring ML Engineer for inference and model integration."
    assert mt.queue_current(1) == second  # equal score: newest first
    mt.set_track(1, "backend")
    assert mt.selected_track(1) == "backend"
    assert mt.queue_current(1) == second
    assert mt.listing(track="backend")["ready"] == 1
    assert mt.mark(second, "skip", next_chat_id=1)[0]
    assert mt.queue_current(1) == first


def test_additional_roles_kept_outside_main_queue(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, Job
    aid = make_app(db)
    with db.session_scope() as sess:
        job = sess.get(Job, sess.get(Application, aid).job_id)
        job.title, job.description_raw = "Product Manager", "Product strategy, roadmap and discovery."
    assert mt.issue(deliver=False)["issued"] == 0
    mt.set_track(1, "additional")
    assert mt.issue(deliver=False)["ids"] == [aid]


def test_skip_feedback_atomic_and_deduplicated(db, monkeypatch):
    from jobhunter import feedback
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application
    aid = make_app(db)
    mt.issue(deliver=False)
    real = mt._queue_current
    monkeypatch.setattr(mt, "_queue_current", lambda *a: (_ for _ in ()).throw(RuntimeError("rollback")))
    with pytest.raises(RuntimeError, match="rollback"):
        mt.mark(aid, "skip", next_chat_id=1, reason="stack")
    assert feedback.summary()["total"] == 0
    with db.session_scope() as sess:
        assert sess.get(Application, aid).outcome == mt.READY
    monkeypatch.setattr(mt, "_queue_current", real)
    assert mt.mark(aid, "skip", next_chat_id=1, reason="stack")[0]
    assert not mt.mark(aid, "skip", next_chat_id=1, reason="salary")[0]
    assert feedback.summary()["reasons"] == {"stack": 1}


def test_skip_reason_button_does_not_mark_until_chosen(db):
    from jobhunter import feedback
    from jobhunter import manual_telegram as mt
    from jobhunter.bot.handlers import handle
    aid = make_app(db)
    mt.issue(deliver=False)
    acts = handle(button(f"t:{aid}:skip_reason"))
    assert any("Причин" in str(a) or "Почему" in str(a) for a in acts)
    assert feedback.summary()["total"] == 0
    handle(button(f"w:skip:{aid}:salary"))
    assert feedback.summary()["reasons"] == {"salary": 1}


def test_why_does_not_rewrite_historical_send(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, utcnow
    aid = make_app(db, sent_at=utcnow(), score=71, score_breakdown_json={"old": "keep"})
    text = mt.explain_card(aid)
    assert "не вероятность" in text and "Версия" in text
    with db.session_scope() as sess:
        app = sess.get(Application, aid)
        assert app.score == 71 and app.score_breakdown_json == {"old": "keep"}
        assert app.message_body == "Hello, Python engineer here."


@pytest.mark.parametrize("command", ["tasks", "results", "reading", "sending", "tracks", "feedback"])
def test_workbench_commands_and_keyboards(db, command):
    from jobhunter.bot import cards, handlers, screens
    acts = handlers.handle(message("/" + command))
    text, markup = screens.render(acts[0]["name"])
    assert 0 < len(text) <= 4096
    for row in markup["inline_keyboard"]:
        for entry in row:
            if "callback_data" in entry:
                assert len(entry["callback_data"].encode()) <= 64
                assert cards.parse_cb(entry["callback_data"])


@pytest.mark.parametrize("payload", [message("/tasks", chat=-100), message("/results", owner=2, chat=2),
                                    button("s:work_tasks_0", chat=-100),
                                    button("w:event_yes:1:offer", owner=2, chat=2)])
def test_no_private_data_or_actions_outside_owner_chat(db, payload, monkeypatch):
    from jobhunter.bot.handlers import handle
    monkeypatch.setattr("jobhunter.bot.workbench.tasks", lambda *a: pytest.fail("Private data read"))
    acts = handle(payload)
    assert not any(a["do"] in ("screen", "edit", "task") for a in acts)


def test_main_counts_ambiguous_without_open_card(db):
    from jobhunter.bot.screens import main
    make_app(db, status="SEND_FAILED_AMBIGUOUS")
    text, _ = main()
    assert "Незавершённых дел: 1" in text
    assert "ничего не нужно" not in text


def test_manual_request_buttons_and_legacy_send_are_safe(db):
    from jobhunter import taskhub
    from jobhunter.bot import cards, handlers
    from jobhunter.models import OwnerRequest
    aid = manual_dialogue(db)
    opened = taskhub.open_card(aid)
    assert opened["ok"]
    rid = opened["request_id"]
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, rid)
        markup = cards.keyboard_for(req)
        assert req.payload_json["manual_reply"]
        assert all(not b.get("callback_data", "").startswith("d:")
                   for row in markup["inline_keyboard"] for b in row)
    acts = handlers.handle(button(f"d:{rid}:send"))
    assert any(a["do"] == "screen" for a in acts)
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, rid).decision == ""


def test_manual_reply_confirmation_does_not_invent_outgoing(db):
    from jobhunter import taskhub
    from jobhunter.bot.handlers import handle
    from jobhunter.models import Message, OwnerRequest, SendLog
    aid = manual_dialogue(db)
    rid = taskhub.open_card(aid)["request_id"]
    handle(button(f"w:manual:{aid}:{rid}"))
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, rid).decision == ""
    handle(button(f"w:manual_yes:{aid}:{rid}"))
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, rid).decision == "manual_sent"
        assert sess.scalar(select(func.count(Message.id)).where(Message.direction == "out")) == 0
        assert sess.scalar(select(func.count(SendLog.id))) == 0


def test_result_requires_confirmation_and_survives_later_rejection(db):
    from jobhunter.bot.handlers import handle
    from jobhunter.models import ResultEvent, utcnow
    from jobhunter.results import aggregate
    aid = make_app(db, status="INTERVIEW_CONFIRMED", sent_at=utcnow() - timedelta(days=20))
    handle(button(f"w:event:{aid}:interview_done"))
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 0
    handle(button(f"w:event_yes:{aid}:interview_done"))
    handle(button(f"w:event_yes:{aid}:interview_done"))
    handle(button(f"w:event_yes:{aid}:rejected"))
    data = aggregate()
    assert data["milestones"]["interview_done"] == 1
    assert data["milestones"]["rejected"] == 1
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(ResultEvent.id))) == 2


def test_reading_unknown_remaining_is_not_zero(db, monkeypatch):
    from jobhunter.bot.workbench import reading
    monkeypatch.setattr("jobhunter.dashboard.reading", lambda: {
        "gmail": {"details": {"remaining": None}},
        "telegram_inbox": {"details": {"remaining": 0}}, "channels": []})
    text, _ = reading()
    assert "Осталось загрузить: неизвестно" in text
    assert "Осталось загрузить: 0" in text


def test_one_tap_skip_then_optional_reason_does_not_repeat_next_card(db):
    from jobhunter import feedback
    from jobhunter import manual_telegram as mt
    from jobhunter.bot.handlers import handle
    from jobhunter.models import Application, BotOutbox
    aid = make_app(db)
    mt.issue(deliver=False)
    acts = handle(button(f"t:{aid}:skip"))
    assert f"w:feedback:{aid}" in str(acts)
    assert feedback.summary()["reasons"] == {"unspecified": 1}
    with db.session_scope() as sess:
        queued = sess.scalar(select(func.count(BotOutbox.id)))
    handle(button(f"w:feedback:{aid}"))
    handle(button(f"w:reason:{aid}:stack"))
    handle(button(f"w:reason:{aid}:stack"))
    assert feedback.summary()["reasons"] == {"stack": 1}
    with db.session_scope() as sess:
        assert sess.get(Application, aid).outcome == mt.SKIPPED
        assert sess.scalar(select(func.count(BotOutbox.id))) == queued
    assert not feedback.complete_reason(aid, "salary", 2)[0]


def test_long_screen_from_pdf_card_is_sent_in_full(db, monkeypatch):
    from jobhunter.bot import api, runner, screens
    calls = []
    body = "Task details " * 180
    def call(method, **kwargs):
        calls.append((method, kwargs))
        if method == "editMessageText":
            raise RuntimeError("Bad Request: there is no text in the message to edit")
        return {"message_id": 987}
    monkeypatch.setattr(api, "call", call)
    monkeypatch.setattr(screens, "render", lambda name: (body, {"inline_keyboard": []}))
    runner._show_screen({"chat_id": 1, "msg_id": 20, "name": "work_tasks_0"}, None)
    assert any(method == "sendMessage" and params["text"] == body for method, params in calls)
    assert not any(method == "editMessageCaption" and params.get("caption") == body[:1024]
                   for method, params in calls)


def test_manual_undo_cannot_erase_a_confirmed_result(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import utcnow
    from jobhunter.results import owner_record
    aid = make_app(db, status="AWAITING_REPLY", outcome=mt.SENT,
                   send_channel="telegram_manual", sent_at=utcnow())
    assert owner_record(aid, "interview_done", 1, "confirmation")[0]
    assert not mt.unmark(aid, 1)[0]


def test_manual_undo_restores_previous_contact_history(db):
    from jobhunter import manual_telegram as mt
    from jobhunter.models import Application, Employer, utcnow
    aid = make_app(db)
    previous = utcnow() - timedelta(days=60)
    with db.session_scope() as sess:
        employer = sess.get(Employer, sess.get(Application, aid).employer_id)
        employer.last_contacted_at, employer.total_messages_sent = previous, 2
    mt.issue(deliver=False)
    assert mt.mark(aid, "sent")[0]
    assert mt.unmark(aid, 1)[0]
    with db.session_scope() as sess:
        employer = sess.get(Employer, sess.get(Application, aid).employer_id)
        assert employer.last_contacted_at == previous.replace(tzinfo=None)
        assert employer.total_messages_sent == 2
