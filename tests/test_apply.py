"""Подготовка анкеты для ручного отклика.

Главный инвариант пакета `apply`: система подаёт заявки только там, где ATS
сам это документирует, — сейчас это один Ashby с его публичным Posting API
(submit_ashby.py). POST-ручки остальных ATS требуют ключ работодателя, а
headless-браузер запрещён их пользовательскими соглашениями: там только
чтение публичной схемы и сборка ответов из фактов — Submit жмёт владелец.

Второй инвариант: ничего не выдумывать. Нет источника для ответа — вопрос
уходит владельцу. Неверный ответ на скрининг закрывает компанию навсегда.
"""
import os
import pathlib
import re

import pytest


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "apply.db")
    os.environ["LLM_ENABLED"] = "false"
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield dbmod
    get_settings.cache_clear()


# Фикстура снята с живого ответа Greenhouse (gitlab, 29.08.2026).
GREENHOUSE_JSON = {
    "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/8592950002",
    "title": "Engineering Manager, Data Foundations",
    "company_name": "GitLab",
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"name": "first_name", "type": "input_text"}]},
        {"label": "Last Name", "required": True,
         "fields": [{"name": "last_name", "type": "input_text"}]},
        {"label": "Email", "required": True,
         "fields": [{"name": "email", "type": "input_text"}]},
        {"label": "Phone", "required": False,
         "fields": [{"name": "phone", "type": "input_text"}]},
        # Два поля в одном вопросе: файл и его текстовый двойник.
        {"label": "Resume/CV", "required": True,
         "fields": [{"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"}]},
        {"label": "Have you previously worked at or consulted for GitLab?",
         "required": True,
         "fields": [{"name": "question_1", "type": "multi_value_single_select",
                     "values": [{"value": 0, "label": "Yes"},
                                {"value": 1, "label": "No"}]}]},
        {"label": "What are your salary expectations?", "required": True,
         "fields": [{"name": "question_2", "type": "input_text"}]},
        {"label": "Will you now or in the future require sponsorship for a visa?",
         "required": True,
         "fields": [{"name": "question_3", "type": "multi_value_single_select",
                     "values": [{"value": 0, "label": "No"},
                                {"value": 1, "label": "Yes"}]}]},
    ],
    # Эти секции не должны попадать в анкету вообще.
    "demographic_questions": {"questions": [{"label": "Gender"}]},
    "compliance": [{"type": "eeoc"}],
}


def _spec_from_fixture(monkeypatch):
    from jobhunter.apply import forms

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return GREENHOUSE_JSON

    class _Http:
        def get(self, *a, **kw):
            return _Resp()

    return forms.fetch_form("greenhouse", "gitlab", "8592950002", http=_Http())


def test_form_schema_is_parsed(monkeypatch):
    spec = _spec_from_fixture(monkeypatch)
    names = [f.name for f in spec.fields]
    assert "first_name" in names and "email" in names
    # Текстовый двойник файла не должен становиться отдельным вопросом.
    assert "resume_text" not in names
    assert any(f.is_file for f in spec.fields)
    # Демография и compliance не разбираются вовсе.
    assert not any("gender" in (f.label or "").lower() for f in spec.fields)


def test_fingerprint_ignores_order(monkeypatch):
    spec = _spec_from_fixture(monkeypatch)
    before = spec.fingerprint()
    spec.fields.reverse()
    assert spec.fingerprint() == before, "перестановка полей — не изменение формы"


def test_identity_is_filled_money_is_not(db, monkeypatch):
    from jobhunter.apply.answers import answer_all

    spec = _spec_from_fixture(monkeypatch)
    answers, unresolved = answer_all(spec)
    filled = {a.label: a.value for a in answers}
    assert filled.get("First Name") == "Suren"
    assert "@" in filled.get("Email", "")
    labels_left = [f.label for f in unresolved]
    assert any("salary" in l.lower() for l in labels_left), \
        "деньги обязаны остаться владельцу"


def test_known_company_question_is_derived(db, monkeypatch):
    """«Работали ли вы в GitLab» — проверяемый факт, а не догадка."""
    from jobhunter.apply.answers import answer_all

    spec = _spec_from_fixture(monkeypatch)
    answers, _ = answer_all(spec)
    worked = next((a for a in answers if "previously worked" in a.label), None)
    assert worked is not None
    assert worked.value == "No", "владелец в GitLab не работал — это из профиля"
    assert worked.source == "derived"


def test_choice_answers_are_human_readable(db, monkeypatch):
    """В анкету идёт текст варианта, а не внутренний id Greenhouse."""
    from jobhunter.apply.answers import answer_all

    answers, _ = answer_all(_spec_from_fixture(monkeypatch))
    for a in answers:
        assert not re.fullmatch(r"\d+", a.value), \
            "владелец выбирает пункт глазами: %r" % a.value


def test_unsupported_provider_degrades_gracefully():
    from jobhunter.apply.forms import fetch_form

    spec = fetch_form("lever", "acme", "123")
    assert not spec.supported
    assert "lever" in spec.note.lower()
    assert spec.fields == []


def test_answer_bank_reuses_owner_answer(db, monkeypatch):
    """Ответив один раз, владелец закрывает вопрос для всех форм."""
    from jobhunter.apply.answers import answer_all, remember

    label = "Will you now or in the future require sponsorship for a visa?"
    remember(label, "No", provider="greenhouse")
    answers, unresolved = answer_all(_spec_from_fixture(monkeypatch))
    visa = next((a for a in answers if "sponsorship" in a.label), None)
    assert visa is not None and visa.value == "No"
    assert visa.source == "bank"
    assert not any("sponsorship" in f.label for f in unresolved)


def test_package_submits_only_where_documented():
    """Сторож: подача заявок — только submit_ashby и только эндпоинт Ashby.

    Если этот тест краснеет — кто-то добавил POST к ATS вне разрешённого
    списка. Ключ там принадлежит работодателю, и такой запрос это не
    «серая зона», а несанкционированный доступ. У Ashby подача разрешена:
    его Posting API документирован и не требует ключа работодателя.
    """
    import jobhunter.apply as apply_pkg

    assert tuple(apply_pkg.SUBMIT_SUPPORTED) == ("ashby",)
    root = pathlib.Path(apply_pkg.__file__).parent
    bad = []
    for path in root.glob("*.py"):
        if path.name == "submit_ashby.py":
            continue
        src = path.read_text(encoding="utf-8")
        # Ищем только вызовы, не упоминания в комментариях и докстрингах.
        code = re.sub(r'"""[\s\S]*?"""|#.*', "", src)
        if re.search(r"\.post\s*\(", code):
            bad.append(path.name)
    assert not bad, "POST в модулях подготовки анкеты: %s" % bad
    # Единственный сетевой POST пакета бьёт в документированный эндпоинт.
    from jobhunter.apply.submit_ashby import SUBMIT_URL
    assert SUBMIT_URL == \
        "https://api.ashbyhq.com/posting-api/application-form/submit"


def test_no_browser_automation_dependency():
    """playwright/selenium не появляются в зависимостях.

    Headless-сабмит формы прямо запрещён соглашением Greenhouse.
    """
    req = pathlib.Path(__file__).parent.parent / "requirements.txt"
    text = req.read_text(encoding="utf-8").lower()
    for banned in ("playwright", "selenium", "undetected-chromedriver"):
        assert banned not in text, banned
