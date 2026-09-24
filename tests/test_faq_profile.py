"""FAQ и зарплатное ожидание из profile.yaml.

Оба раздела опциональны, и это главный инвариант: пустой профиль ведёт
себя байт в байт как система до их появления. Заполненный faq.about_me
включает автоответ на «расскажите о себе»; заполненный salary_expectation
разрешает черновику называть цифру — но money остаётся эскалацией, и
отправляет ответ только владелец.
"""
import os
import uuid

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "faq.db")
    os.environ["TELEGRAM_BOT_TOKEN"] = "test:token"
    os.environ["LLM_ENABLED"] = "false"
    os.environ["GCAL_FREEBUSY_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


def _profile(faq=None, salary=""):
    from jobhunter.profile import Profile, get_profile
    raw = dict(get_profile().raw)
    raw["faq"] = faq or []
    raw["salary_expectation"] = salary
    return Profile(raw)


def _app_row(db):
    from jobhunter.models import Application, ContactKind, Job, Status
    with db.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:test",
                  title="Python Backend",
                  contact_kind=ContactKind.USER_HANDLE.value,
                  contact_handle="hr", description_raw="Python")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, score=70,
                          status=Status.AWAITING_REPLY.value)
        sess.add(app)
        sess.flush()
        sess.expunge(app)
        return app


def test_about_without_faq_goes_to_draft(db, monkeypatch):
    """Пустой faq больше не означает эскалацию.

    Раньше «расскажите о себе» без выверенного текста в профиле уходило
    владельцу — и уходило всегда, потому что faq пуст. Теперь ответ пишет
    LLM строго по фактам профиля, и его обязаны пропустить гейт правды и
    самопроверка: needs_draft + needs_review в плане.
    """
    from jobhunter.convo.reply import plan_reply

    # Боевой profile.yaml с 24.09 содержит about_me — пустой faq задаём явно.
    empty = _profile()
    monkeypatch.setattr("jobhunter.profile.get_profile", lambda: empty)
    plan = plan_reply(_app_row(db), "Расскажите о себе, пожалуйста")
    assert plan.should_reply
    assert plan.needs_draft and plan.needs_review,         "текст обязан быть написан LLM и проверен перед отправкой"
    assert plan.text == "", "шаблонного отката у «о себе» нет"


def test_about_answered_from_faq(db, monkeypatch):
    from jobhunter.convo.reply import plan_reply

    # Профиль строится ДО подмены: _profile сам зовёт get_profile, и мок
    # поверх него зацикливается.
    prof = _profile(faq=[{"id": "about_me",
                          "answer_ru": "Семь лет в бэкенде, Python и "
                                       "FastAPI, ищу удалённую роль."}])
    monkeypatch.setattr("jobhunter.profile.get_profile", lambda: prof)
    plan = plan_reply(_app_row(db), "Расскажите о себе, пожалуйста")
    assert plan.should_reply
    assert "Семь лет в бэкенде" in plan.text


def test_salary_number_passes_the_gate(db):
    """Число из salary_expectation гейт пропускает: владелец его разрешил."""
    from jobhunter.tailor.gate import DocModel, check

    p = _profile(salary="от 250000 руб на руки")
    assert "250000" in p.allowed_numbers()
    doc = DocModel(lang="ru", kind="message",
                   free_text="По деньгам ориентируюсь от 250000 руб на руки, "
                             "открыт к обсуждению по итогам разговора.")
    gate = check(doc, jd_text="Python-разработчик, зарплата по итогам",
                 profile=p)
    assert gate.passed, [f.rule_id for f in gate.hard]


def test_money_is_always_escalated(db):
    """Заполненная зарплата НЕ делает money автоответом."""
    from jobhunter.convo.classify import ESCALATE, MONEY, classify
    from jobhunter.convo.reply import plan_reply

    intent = classify("Какие у вас зарплатные ожидания?")
    assert intent.label == MONEY
    assert MONEY in ESCALATE
    plan = plan_reply(_app_row(db), "Какие у вас зарплатные ожидания?")
    assert not plan.should_reply and plan.escalate


def test_money_hint_switches_with_salary(db):
    from jobhunter.convo.draft import _MONEY_HINT_WITH_FIGURE, facts_block, prompt_reply

    p = _profile(salary="от 250000 руб")
    facts = facts_block(p)
    assert "Зарплатное ожидание" in facts
    prompt = prompt_reply("X", "", "", "Какая у вас вилка?", facts, "", "",
                          "money")
    assert _MONEY_HINT_WITH_FIGURE in prompt
    # без зарплаты — старый хинт «не называй сумму»
    prompt2 = prompt_reply("X", "", "", "Какая у вас вилка?",
                           facts_block(_profile()), "", "", "money")
    assert "Не называй сумму" in prompt2


FAQ_FIXTURE = [
    {"id": "last_project", "answer_ru": "Последний проект — Integration Manager в Linkero."},
    {"id": "location_and_format", "answer_ru": "Живу в Москве. Работаю только удалённо."},
    {"id": "payments_experience", "answer_ru": "Есть опыт платёжных интеграций — Stripe, PayPal."},
    {"id": "start_date", "answer_ru": "Могу выйти сразу."},
]
GDL_IT = ("Добрый день! Спасибо за отклик. Ответьте пожалуйста на вопросы: "
          "1) Расскажите о вашем последнем проекте, какие технологии там использовались? "
          "2) Готовы ли к посещению офиса в Москве? 3) Есть ли опыт в финтехе? "
          "Я вернусь с обратной связью в течении недели.")


def test_every_question_covered_by_faq_is_answered_verbatim_in_order(monkeypatch):
    """24.09: LLM-пересказ на вопросы GDL IT написал «переезд в офис обсуждаем» —
    а у владельца на все три вопроса есть выверенные ответы."""
    from jobhunter.convo.reply import faq_reply
    prof = _profile(faq=FAQ_FIXTURE)
    monkeypatch.setattr("jobhunter.profile.get_profile", lambda: prof)
    text, topics = faq_reply(GDL_IT)
    assert topics == ["last_project", "location_and_format", "payments_experience"]
    assert text == ("Последний проект — Integration Manager в Linkero. Живу в Москве. "
                    "Работаю только удалённо. Есть опыт платёжных интеграций — Stripe, PayPal.")
    assert faq_reply("Когда сможете выйти?") == ("Могу выйти сразу.", ["start_date"])


def test_a_question_outside_faq_falls_back_to_the_checked_draft(monkeypatch):
    from jobhunter.convo.reply import faq_reply
    prof = _profile(faq=FAQ_FIXTURE)
    monkeypatch.setattr("jobhunter.profile.get_profile", lambda: prof)
    assert faq_reply("Когда сможете выйти? И какой у вас опыт с Rust?") == ("", [])


def test_tech_question_plan_uses_faq_verbatim(db, monkeypatch):
    from jobhunter.convo.reply import plan_reply
    prof = _profile(faq=FAQ_FIXTURE)
    monkeypatch.setattr("jobhunter.profile.get_profile", lambda: prof)
    plan = plan_reply(_app_row(db), GDL_IT, bold=True)
    assert plan.should_reply and plan.verbatim and not plan.needs_draft
    assert "только удалённо" in plan.text


@pytest.mark.parametrize("incoming, draft_text, bad", [
    ("Готовы ли к посещению офиса в Москве?", "Живу в Москве, переезд в офис обсуждаем.", True),
    # с 24.09 офис в Москве подходит — «только удалённо» противоречит резюме
    ("Готовы ли к посещению офиса в Москве?", "Работаю только удалённо.", True),
    ("Готовы ли к посещению офиса в Москве?", "Да, живу в Москве — офис подходит.", False),
    ("Готовы к переезду?", "К переезду не готов, командировки возможны.", False),
    ("Готовы к переезду?", "Переезд не рассматриваю.", False),
    ("Готовы к переезду?", "Готов к переезду.", True),
    ("Есть опыт в финтехе?", "Есть опыт финтех-интеграций: Stripe.", True),
    ("Есть опыт в финтехе?", "Есть опыт платёжных интеграций: Stripe.", False),
])
def test_llm_tech_draft_keeps_the_owner_positions(incoming, draft_text, bad):
    from jobhunter.convo.draft import _tech_stance_problem
    assert bool(_tech_stance_problem(incoming, draft_text)) == bad
