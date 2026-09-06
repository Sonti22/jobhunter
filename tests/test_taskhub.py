"""Shared task operations: atomic handoff, stale cards, privacy and populated web."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from jobhunter import dashboard, taskhub
from jobhunter.models import Application, BotOutbox, Job, Message, OwnerRequest, ResultEvent, utcnow
from tests.test_bot_workbench import button, manual_dialogue
from tests.test_manual_telegram import db as db
from tests.test_manual_telegram import make_app


def test_simultaneous_open_and_restart_reuse_one_card(db):
    aid = manual_dialogue(db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        opened = list(pool.map(taskhub.open_card, [aid, aid]))
    assert all(row['ok'] for row in opened)
    assert opened[0]['request_id'] == opened[1]['request_id']
    db._engine.dispose()
    db._engine = db._Session = None
    assert taskhub.open_card(aid)['request_id'] == opened[0]['request_id']
    with db.session_scope() as sess:
        assert sess.scalar(select(func.count(OwnerRequest.id))) == 1
        assert not sess.scalar(select(Message.processing_pending))


def test_failed_handoff_rolls_back_input_and_card_and_outbox(db, monkeypatch):
    aid = manual_dialogue(db)
    with db.session_scope() as sess:
        previous = sess.scalar(select(func.count(BotOutbox.id)))
    def fail(*args, **kwargs):
        raise RuntimeError('outbox storage unavailable')
    monkeypatch.setattr(taskhub, '_queue_manual', fail)
    with pytest.raises(RuntimeError, match='storage'):
        taskhub.open_card(aid)
    with db.session_scope() as sess:
        assert sess.scalar(select(Message.processing_pending))
        assert sess.scalar(select(func.count(OwnerRequest.id))) == 0
        assert sess.scalar(select(func.count(BotOutbox.id))) == previous


def test_expired_card_restored_once_and_old_button_cannot_ack(db):
    aid = manual_dialogue(db)
    old = taskhub.open_card(aid)['request_id']
    with db.session_scope() as sess:
        sess.get(OwnerRequest, old).expires_at = utcnow() - timedelta(seconds=1)
    assert taskhub.detail(aid)['category'] == 'expired'
    new = taskhub.open_card(aid)['request_id']
    assert old != new
    assert taskhub.open_card(aid)['request_id'] == new
    assert not taskhub.complete_manual(aid, old, 1)[0]
    assert taskhub.complete_manual(aid, new, 1)[0]
    assert dashboard.attention()['total'] == 0


def test_new_incoming_invalidates_draft_and_requires_refresh_before_ack(db):
    aid = manual_dialogue(db)
    rid = taskhub.open_card(aid)['request_id']
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, rid)
        req.payload_json = dict(req.payload_json, draft='Old answer')
        sess.add(Message(application_id=aid, direction='in', body='Changed question',
                         received_at=utcnow(), processing_pending=True))
    assert taskhub.detail(aid)['draft'] == ''
    assert not taskhub.complete_manual(aid, rid, 1)[0]
    assert taskhub.open_card(aid)['request_id'] == rid
    view = taskhub.detail(aid)
    assert view['incoming'] == 'Changed question' and view['draft'] == ''
    assert taskhub.complete_manual(aid, rid, 1)[0]


def test_unconfirmed_delivery_never_restored_or_acked(db):
    aid = manual_dialogue(db)
    rid = taskhub.open_card(aid)['request_id']
    with db.session_scope() as sess:
        sess.get(OwnerRequest, rid).apply_error = 'delivery_unconfirmed: check dialogue'
    assert dashboard.attention()['items'][0]['category'] == 'error'
    assert not taskhub.open_card(aid)['ok']
    assert not taskhub.complete_manual(aid, rid, 1)[0]
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, rid).decision == ''


def test_web_and_bot_share_page_priority_oldest_first_and_total(db):
    from jobhunter.bot.workbench import tasks
    ids = [make_app(db, handle=f'recruiter_{i}', status='NEEDS_HUMAN',
                    last_inbound_at=utcnow() - timedelta(hours=i)) for i in range(7)]
    interview = make_app(db, handle='interview_recruiter', status='INTERVIEW_PROPOSED')
    failed = make_app(db, handle='failed_recruiter', status='SEND_FAILED')
    expected = [interview] + ids[::-1] + [failed]
    from jobhunter.web.server import app
    client = TestClient(app)
    for offset in (0, 5):
        data = client.get(f'/api/attention?offset={offset}&limit=5').json()
        assert data['total'] == 9
        assert [r['id'] for r in data['items']] == expected[offset:offset + 5]
        _, markup = tasks(offset)
        targets = [b['callback_data'] for row in markup['inline_keyboard'] for b in row
                   if b.get('callback_data', '').startswith('s:work_task_')]
        assert targets == [f's:work_task_{aid}' for aid in expected[offset:offset + 5]]


def test_owner_engine_card_is_manual_and_legacy_send_is_not_queued(db):
    from jobhunter.bot.handlers import handle
    from jobhunter.owner import create_human_request
    aid = manual_dialogue(db)
    with db.session_scope() as sess:
        application = sess.get(Application, aid)
        req = create_human_request(sess, application, sess.get(Job, application.job_id),
                                   'Question', 'Review', 'Manual draft')
        rid = req.id
        assert req.payload_json['manual_reply'] and req.payload_json['incoming_message_ids']
    for action in ('send', 'ok', 'say'):
        handle(button(f'd:{rid}:{action}'))
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, rid).decision == ''


def test_stale_legacy_nonmanual_button_does_not_claim_or_reuse_old_draft(db):
    from jobhunter.bot.handlers import handle
    aid = make_app(db, status='NEEDS_HUMAN', sent_at=utcnow() - timedelta(days=1))
    rid = taskhub.open_card(aid)['request_id']
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, rid)
        req.payload_json = dict(req.payload_json, draft='Old draft')
        sess.add(Message(application_id=aid, direction='in', body='New question',
                         received_at=utcnow(), processing_pending=True))
    handle(button(f'd:{rid}:send'))
    with db.session_scope() as sess:
        assert sess.get(OwnerRequest, rid).decision == ''
    restored = taskhub.open_card(aid)
    assert restored['ok'] and restored['request_id'] != rid
    assert taskhub.detail(aid)['draft'] == ''


def test_populated_results_html_and_api_history_contract(db):
    from jobhunter.results import owner_record
    from jobhunter.web.server import app
    aid = make_app(db, status='AWAITING_REPLY', sent_at=utcnow() - timedelta(days=20))
    assert owner_record(aid, 'interview_done', 1, 'click')[0]
    assert owner_record(aid, 'rejected', 1, 'refusal')[0]
    client = TestClient(app)
    for route in ('/results', '/outcomes'):
        response = client.get(route)
        assert response.status_code == 200
        assert 'Интервью проведено' in response.text and f'#{aid}' in response.text
    data = client.get('/api/outcomes?limit=5').json()
    assert data['milestones']['interview_done'] == data['milestones']['rejected'] == 1
    assert data['applications'][0]['id'] == aid
    assert {row['application_id'] for row in data['history']} == {aid}


def test_matching_review_visible_shared_and_old_confirmation_rejected(db, monkeypatch):
    from jobhunter.match import explain
    from jobhunter.web.server import app
    aid = make_app(db, status='PENDING_APPROVAL')
    assessment = dict(explain.explain_job(Job(title='Python Backend Engineer')),
                      needs_review=True, review_reasons=['Unproven requirement'])
    monkeypatch.setattr(explain, 'explain_job', lambda *a, **k: assessment)
    with db.session_scope() as sess:
        application = sess.get(Application, aid)
        fingerprint = explain.review_fingerprint(application, sess.get(Job, application.job_id))
    assert dashboard.attention()['items'][0]['can_review_match']
    assert not taskhub.open_card(aid)['ok']
    client = TestClient(app)
    assert 'Проверить соответствие' in client.get('/attention').text
    assert fingerprint in client.get(f'/attention/{aid}/match').text
    with db.session_scope() as sess:
        sess.get(Application, aid).message_body = 'Changed text'
    response = client.post(f'/attention/{aid}/match/confirm', data={'fingerprint': fingerprint})
    assert response.status_code == 409
    assert client.post(f'/attention/{aid}/match/confirm', data={'fingerprint': fingerprint},
                       headers={'Origin': 'https://attacker.test'}).status_code == 403
    with db.session_scope() as sess:
        assert sess.get(Application, aid).status == 'PENDING_APPROVAL'
        assert sess.scalar(select(func.count(OwnerRequest.id))) == 0


def test_owner_refusal_stops_pending_decision_but_retains_delivery_evidence(db):
    from jobhunter.results import owner_record
    aid = manual_dialogue(db)
    rid = taskhub.open_card(aid)['request_id']
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, rid)
        req.decision, req.apply_error = 'send', 'delivery_unconfirmed: unknown'
    assert owner_record(aid, 'rejected', 1, 'refusal')[0]
    with db.session_scope() as sess:
        req = sess.get(OwnerRequest, rid)
        assert req.decision == 'skip' and req.applied_at
        assert req.apply_error.startswith('delivery_unconfirmed')
        assert sess.scalar(select(func.count(ResultEvent.id))) == 1


def test_booking_records_only_scheduled_event_and_deduplicates(db):
    from jobhunter.schedule.book import confirm
    aid = make_app(db, status='IN_DIALOGUE', sent_at=utcnow() - timedelta(days=1))
    at = utcnow() + timedelta(days=2)
    confirm(aid, at)
    confirm(aid, at)
    with db.session_scope() as sess:
        event = sess.scalars(select(ResultEvent)).one()
        assert event.kind == 'interview_scheduled' and event.source == 'calendar'
