"""Ответы работодателей в Gmail не теряются.

Диагностика 16.09 на живой базе: Astoria AI 09.09 позвали на следующий этап
(«Next Steps: пришлите work sample»), SearchAtlas попросили подать отклик по
ссылке — оба письма неделю лежали «в обработке» без ответа и без карточки.
Причин было четыре: антиспам Telegram блокировал и почтовые ответы,
классификатор принимал такие письма за «спасибо», заблокированный автоответ
не повторялся и не эскалировался, а письмо с другого адреса компании не
привязывалось к заявке.
"""
import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from jobhunter.convo import mailmatch
from jobhunter.convo.classify import classify

ASTORIA = """Dear Suren,

Thank you for your interest in Astoria AI. We reviewed your application and we believe that there could be a potential fit.

Next Steps

To continue the process, we would like to get a better understanding of your practical experience. Please share the following:

Work sample or project
Present an existing work sample or a meaningful project that best represents your skills and experience (Full Stack and/or GenAI) relevant for Astoria AI. Please share the GitHub repository or your portfolio, with a short explanation or demo (video preferred).

After reviewing your submission, we will schedule a conversation with the CTO and the founders to discuss your experience and potential collaboration.

Happy to answer any questions. Please feel free to reach out.

Konstantin
Astoria AI"""

SEARCH_ATLAS = """Hi!
Thanks for reaching out!
You can apply to the role directly here:
https://searchatlas.na.teamtailor.com/jobs/595729-senior-full-stack-engineer-django-next-js

Wishing you all the very best,"""


# ── классификатор ──

@pytest.mark.parametrize("text,expected", [
    (ASTORIA, "task_request"),
    (SEARCH_ATLAS, "apply_link"),
    ("Сурен, добрый день!\nНа данный момент мы остановили поиск на данную вакансию, "
     "т.к. определились с финальными кандидатами.\nБольшое спасибо за ваш интерес!",
     "rejection"),
    ("Hi Suren,\n\nThanks for your interest! We are only hiring in the U.S. right now so\n"
     "this is not a fit.", "rejection"),
    ("Пройдите AI-интервью по ссылке, это займёт 20 минут", "task_request"),
    ("Could you complete a take-home assignment?", "task_request"),
    ("Пришлите тестовое до пятницы", "task_request"),   # срок сдачи — не слот
])
def test_live_employer_emails(text, expected):
    assert classify(text).label == expected


def test_thanks_with_question_is_not_routine():
    """«Благодарю за отклик. Был ли опыт с LLM?» получал шаблонное «спасибо, жду»."""
    it = classify("Добрый вечер\n\nБлагодарю за отклик\n"
                  "Подскажите, был ли у вас коммерческий опыт в направлении AI/LLM?")
    assert it.label != "ack" and it.needs_human


def test_plain_thanks_is_still_ack():
    assert classify("Спасибо, посмотрим").label == "ack"
    assert classify("Добрый день! Благодарю за отклик! Передам информацию "
                    "нанимающему менеджеру и вернусь к Вам с обратной связью)").label == "ack"


def test_task_and_apply_link_never_auto_even_in_bold():
    from jobhunter.convo.reply import plan_reply
    from jobhunter.models import Application, Status

    app = Application(id=1, status=Status.AWAITING_REPLY.value, cv_lang="en")
    for text in (ASTORIA, SEARCH_ATLAS):
        plan = plan_reply(app, text, bold=True)
        assert not plan.should_reply and plan.escalate


# ── привязка по домену ──

def test_org_domain():
    assert mailmatch.org_domain("hrplatform@sberbank.ru") == "sberbank.ru"
    assert mailmatch.org_domain("hr@mail.sberbank.ru") == "sberbank.ru"
    assert mailmatch.org_domain("jobs@acme.co.uk") == "acme.co.uk"
    assert mailmatch.org_domain("someone@gmail.com") == ""
    assert mailmatch.org_domain("hr@yandex.ru") == ""


def test_other_address_of_same_company_binds_to_single_application():
    ctx = mailmatch.MatchContext(by_domain={"sberbank.ru": [42]},
                                 known_domains={"sberbank.ru"})
    cand = mailmatch.match_by_headers(
        {"from": "HR Platform <hrplatform@sberbank.ru>",
         "subject": "Пройдите AI-интервью"}, ctx)
    assert (cand.app_id, cand.rule, cand.need_body) == (42, "domain", True)


def test_freemail_never_binds_by_domain():
    ctx = mailmatch.MatchContext(by_domain={"gmail.com": [42]})
    cand = mailmatch.match_by_headers({"from": "stranger@gmail.com", "subject": "hi"}, ctx)
    assert not cand.matched and not cand.need_body


def test_several_applications_on_domain_go_to_owner_not_guess():
    ctx = mailmatch.MatchContext(by_domain={"acme.com": [1, 2]},
                                 subjects={1: "Python Engineer", 2: "Data Engineer"})
    cand = mailmatch.match_by_headers({"from": "talent@acme.com", "subject": "Hello"}, ctx)
    assert not cand.matched and cand.need_body and cand.unresolved == [1, 2]
    by_subject = mailmatch.match_by_headers(
        {"from": "talent@acme.com", "subject": "Re: Data Engineer"}, ctx)
    assert (by_subject.app_id, by_subject.rule) == (2, "domain+subject")


# ── живая база ──

@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "gmail.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("KILL_SWITCH_PATH", str(tmp_path / "STOP"))
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


def _email_app(db, addr="konstantin.mueller@astoria.ai", status="AWAITING_REPLY"):
    from jobhunter.models import Application, ContactKind, Job
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db.session_scope() as sess:
        job = Job(external_uuid=str(random.random()), source="hn", title="Full Stack Engineer",
                  company_name="Astoria AI", description_raw="Python, GenAI",
                  contact_kind=ContactKind.EMAIL.value, contact_url="mailto:" + addr)
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=status, score=80, gate_passed=True,
                          cv_lang="en", message_body="Hello", sent_at=now - timedelta(days=8))
        sess.add(app)
        sess.flush()
        return app.id


def test_telegram_antispam_does_not_block_email_replies(db):
    from jobhunter.convo import route
    from jobhunter.convo.send import can_reply
    from jobhunter.outreach import policy
    with db.session_scope() as sess:
        policy.get_state(sess).manual_only = True
    with db.session_scope() as sess:
        assert can_reply(sess, route.EMAIL)[0]
        allowed, why = can_reply(sess, route.TELEGRAM)
        assert not allowed and "PeerFlood" in why


def test_blocked_auto_reply_becomes_owner_card(db, monkeypatch):
    from jobhunter.convo import engine
    from jobhunter.models import Application, OwnerRequest

    async def blocked(*args, **kwargs):
        return "stop:стоп-кран"
    monkeypatch.setattr(engine, "send_reply", blocked)
    monkeypatch.setattr(engine, "within_reply_window", lambda: True)
    monkeypatch.setattr(engine, "reply_delay_seconds", lambda: 0)
    app_id = _email_app(db)
    verdict = asyncio.run(engine.handle_message(None, app_id, "Please send your CV"))
    assert verdict.startswith("эскалация") and "не ушёл" not in verdict
    with db.session_scope() as sess:
        assert sess.get(Application, app_id).status == "NEEDS_HUMAN"
        card = sess.scalar(select(OwnerRequest))
        assert "заблокирован" in card.payload_json["reason"]


def test_stuck_emails_are_reprocessed_into_cards(db):
    from jobhunter.convo import inbox_email
    from jobhunter.models import Message, OwnerRequest, utcnow

    astoria = _email_app(db)
    atlas = _email_app(db, addr="joao.pedro@searchatlas.com")
    with db.session_scope() as sess:
        for app_id, body in ((astoria, ASTORIA), (atlas, SEARCH_ATLAS)):
            sess.add(Message(application_id=app_id, direction="in", body=body,
                             received_at=utcnow(), processing_pending=True,
                             processing_error="автоответ не ушёл: stop:ручной режим после двух PeerFlood"))
    assert asyncio.run(inbox_email.retry_stuck()) == 2
    with db.session_scope() as sess:
        assert not sess.scalars(select(Message).where(Message.processing_pending.is_(True))).all()
        cards = {c.application_id: c for c in sess.scalars(select(OwnerRequest))}
        assert "следующий этап" in cards[astoria].payload_json["reason"]
        assert cards[atlas].payload_json["apply_url"].startswith("https://searchatlas.")
        assert "joao.pedro@searchatlas.com" in cards[atlas].question
        ttl = cards[astoria].expires_at - cards[astoria].created_at
        assert ttl >= timedelta(hours=71), "почтовая карточка живёт 72 часа"
    # повтор не зацикливается: зависших больше нет
    assert asyncio.run(inbox_email.retry_stuck()) == 0


def test_apply_link_card_has_url_button():
    from types import SimpleNamespace

    from jobhunter.bot.cards import keyboard_for
    req = SimpleNamespace(id=7, application_id=1, kind="needs_human",
                          payload_json={"apply_url": "https://jobs.example.com/1", "reason": "x"})
    rows = keyboard_for(req)["inline_keyboard"]
    assert rows[0][0]["url"] == "https://jobs.example.com/1"


def test_build_context_indexes_company_domain(db):
    from jobhunter.convo.inbox_email import build_context
    app_id = _email_app(db, addr="hr@sberbank.ru")
    ctx = build_context()
    assert ctx.by_domain["sberbank.ru"] == [app_id]
    cand = mailmatch.match_by_headers({"from": "hrplatform@sberbank.ru",
                                       "subject": "Пройдите AI-интервью"}, ctx)
    assert (cand.app_id, cand.rule) == (app_id, "domain")


# ── находки 18.09 ──

ZAPIER = """Hi Suren,

Thanks so much for reaching out and for your interest in Zapier! We really
appreciate the initiative.

At Zapier, all candidate journeys start with an application through our
jobs page. I'd encourage you to check out our current openings at
zapier.com/jobs
<https://www.google.com/url?q=https://zapier.com/jobs&source=gmail&ust=1789736726823000&sa=E>
and apply to any roles that match your background and interests.

From there, the hiring team will review your application and reach out with
next steps if there's a fit.

Best,
Raluca"""


def test_apply_through_jobs_page_is_apply_link_not_a_task():
    assert classify(ZAPIER).label == "apply_link"


def test_apply_url_unwraps_google_redirect_and_bare_domain():
    from jobhunter.convo.engine import find_apply_url
    assert find_apply_url(ZAPIER) == "https://zapier.com/jobs"
    assert find_apply_url("Please apply at acme.io/careers/backend.") == "https://acme.io/careers/backend"
    assert find_apply_url("Apply here: https://jobs.acme.com/1.") == "https://jobs.acme.com/1"
    assert find_apply_url("Write to hr@acme.com (acme.com)") == ""


def test_email_step_follows_caught_up_approval():
    """18.09: почта сработала в 10:30 по пустой очереди, пока догон ещё собирал вакансии."""
    from jobhunter.autopilot import email_after_approve
    assert email_after_approve(["ingest", "prepare", "approve", "manual_prep"]) == \
        ["ingest", "prepare", "approve", "email", "manual_prep"]
    assert email_after_approve(["ingest", "approve", "email"]) == ["ingest", "approve", "email"]
    assert email_after_approve(["manual_prep"]) == ["manual_prep"]


def test_domain_only_mail_notifies_once_per_sender_per_day(monkeypatch):
    from jobhunter import notify
    from jobhunter.convo import inbox_email
    keys = []
    monkeypatch.setattr(notify, "push", lambda kind, text, **kw: keys.append((kw.get("dedup"), text)))
    for mid in ("<a@x>", "<b@x>"):
        inbox_email._notify_ambiguous({"from": "hrplatform@sberbank.ru", "message-id": mid,
                                       "subject": "Пройдите AI-интервью"}, [6030, 9789], by_domain=True)
    assert keys[0][0] == keys[1][0] and keys[0][0].startswith("ambig_domain:hrplatform@sberbank.ru:")
    assert "не относится" in keys[0][1]
    inbox_email._notify_ambiguous({"from": "hr@acme.com", "message-id": "<c@x>", "subject": "Hi"}, [1, 2])
    assert keys[2][0] == "ambig:<c@x>"
