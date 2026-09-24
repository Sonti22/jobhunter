"""Смелый режим: бот сам ведёт переписку до созвона.

Владелец разрешил боту отвечать на технические вопросы и на вопрос о
формате работы. Это расширение доверия, и цена ошибки растёт вместе с ним,
поэтому здесь проверяются прежде всего ГРАНИЦЫ:

  - деньги, оффер и предложенное время не автоматизируются никогда,
    ни при каком флаге;
  - у техвопроса нет шаблонного отката: не написался черновик или не прошла
    самопроверка — уходит карточка владельцу, а не «что-нибудь»;
  - техответы считаются отдельно и не съедают общий запас автоответов.
"""
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "bold.db")
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


def _app(db, **kw):
    from jobhunter.models import Application, ContactKind, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:t",
                  title="Python Developer",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr", description_raw="Python, FastAPI")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=80, gate_passed=True,
                          status=Status.AWAITING_REPLY.value, **kw)
        sess.add(app)
        sess.flush()
        sess.expunge(app)
        return app


def test_money_never_auto_even_in_bold(db):
    """Деньги остаются владельцу при любом флаге — решение владельца."""
    from jobhunter.convo.reply import plan_reply

    for bold in (False, True):
        plan = plan_reply(_app(db), "Какие у вас зарплатные ожидания?",
                          bold=bold)
        assert not plan.should_reply, "bold=%s" % bold
        assert plan.escalate


def test_offer_and_slots_never_auto(db):
    from jobhunter.convo.reply import plan_reply

    for text in ("Готовы сделать вам оффер",
                 "Давайте созвонимся в четверг в 15:00"):
        plan = plan_reply(_app(db), text, bold=True)
        assert not plan.should_reply, text
        assert plan.escalate


def test_tech_question_escalates_without_bold(db):
    from jobhunter.convo.reply import plan_reply

    plan = plan_reply(_app(db), "Расскажите, как бы вы построили API",
                      bold=False)
    assert not plan.should_reply
    assert plan.escalate


def test_tech_question_drafts_in_bold(db):
    """В смелом режиме техвопрос идёт в черновик — но с обязательной проверкой."""
    from jobhunter.convo.reply import plan_reply

    plan = plan_reply(_app(db), "Расскажите, как бы вы построили API",
                      bold=True)
    assert plan.should_reply
    assert plan.needs_draft, "шаблона у техвопроса нет и быть не может"
    assert plan.needs_review, "непроверенный техответ отправлять нельзя"
    assert plan.text == ""


def test_tech_replies_have_own_limit(db):
    """Два техответа на тред — дальше владелец. Общий лимит не тратится."""
    from jobhunter.convo.reply import MAX_AUTO_TECH_REPLIES, plan_reply

    app = _app(db, auto_tech_replies_count=MAX_AUTO_TECH_REPLIES)
    plan = plan_reply(app, "Расскажите, как бы вы построили API", bold=True)
    assert not plan.should_reply
    assert "лимит техответов" in plan.reason
    # Общий запас при этом цел: на «когда созвонимся» ответить ещё можно.
    ok = plan_reply(app, "Когда вам удобно созвониться?", bold=True)
    assert ok.should_reply


def test_work_format_answered_from_profile(db):
    """«Готовы в офис?» — ответ известен жёстко, LLM не нужна."""
    from jobhunter.convo.reply import plan_reply

    plan = plan_reply(_app(db), "Можешь работать в офисе в Москве?", bold=True)
    assert plan.should_reply
    assert not plan.needs_draft, "ответ шаблонный: только удалёнка"
    assert "удал" in plan.text.lower()


def test_work_format_escalates_without_bold(db):
    from jobhunter.convo.reply import plan_reply

    plan = plan_reply(_app(db), "Готовы к переезду?", bold=False)
    assert not plan.should_reply


def test_needs_human_latch_holds_in_bold(db):
    """Тред у человека — смелость ничего не меняет."""
    from jobhunter.convo.reply import plan_reply
    from jobhunter.models import Status

    app = _app(db)
    app.status = Status.NEEDS_HUMAN.value
    plan = plan_reply(app, "Расскажите, как бы вы построили API", bold=True)
    assert not plan.should_reply
    assert "у человека" in plan.reason


def test_general_reply_limit_still_caps_bold(db):
    from jobhunter.convo.reply import MAX_AUTO_REPLIES, plan_reply

    app = _app(db, auto_replies_count=MAX_AUTO_REPLIES)
    plan = plan_reply(app, "Расскажите, как бы вы построили API", bold=True)
    assert not plan.should_reply
    assert "лимит автоответов" in plan.reason


def test_tech_question_draft_is_actually_written(monkeypatch):
    """План помечал техвопрос «черновиком по фактам», а черновик писать было
    нечем: в задачах draft_routine_reply не было tech_question, и 24.09 каждый
    такой вопрос (вопросы GDL IT — месяц) уходил владельцу карточкой."""
    from types import SimpleNamespace

    from jobhunter.config import get_settings
    from jobhunter.convo import draft

    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    answer = ("Последний проект — Integration Manager в Linkero на FastAPI и "
              "PostgreSQL. Живу в Москве, подходит и офис. Есть опыт платёжных "
              "интеграций — Stripe и PayPal.")
    monkeypatch.setattr(draft, "generate",
                        lambda prompt: SimpleNamespace(ok=True, text=answer,
                                                       provider="test", error=""))
    d = draft.draft_routine_reply("tech_question", "Python Backend", "Python, FastAPI",
                                  "Расскажите о последнем проекте? Есть опыт в финтехе?", [])
    assert d.ok, d.problem
    assert d.text == answer


def test_tech_question_draft_still_goes_through_the_truth_gate(monkeypatch):
    """Текст уходит без владельца — выдуманная технология обязана резаться."""
    from types import SimpleNamespace

    from jobhunter.config import get_settings
    from jobhunter.convo import draft

    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    invented = "Да, пять лет пишу на Rust и Haskell, строил блокчейн на Solidity."
    monkeypatch.setattr(draft, "generate",
                        lambda prompt: SimpleNamespace(ok=True, text=invented,
                                                       provider="test", error=""))
    d = draft.draft_routine_reply("tech_question", "Python Backend", "Python",
                                  "Есть опыт с Rust?", [])
    assert not d.ok and d.problem.startswith("гейт"), d.problem
