"""Тексты о кандидате — из профиля, а не из кода.

До 30.09 имя владельца, стаж, «Москва» и «C2» были зашиты в ~20 файлах: тема
письма, вступления, ответ про формат, шапка резюме, имя файла. Любой другой
человек, запустивший бота, писал бы работодателям от имени владельца.
"""
import copy

import pytest

from jobhunter import persona
from jobhunter.profile import Profile, get_profile


def _profile(**identity) -> Profile:
    raw = copy.deepcopy(get_profile().raw)
    raw.pop("outreach", None)
    raw["identity"].update(identity)
    return Profile(raw)


def test_every_outreach_text_comes_from_profile_section():
    p = get_profile()
    sec = p.raw["outreach"]
    assert persona.text("format_line", "ru", p) == sec["format_line_ru"]
    assert persona.pool("intros", "en", p) == sec["intros_en"]
    assert persona.pool("format_replies", "ru", p) == sec["format_replies_ru"]
    assert persona.cv_file_prefix(p) == sec["cv_file_prefix"]


def test_without_section_texts_are_neutral_and_use_only_identity():
    p = _profile(full_name_ru="Петров Иван Сергеевич", full_name_en="Ivan Petrov",
                 location="Казань, Россия", work_formats=["remote", "office_kazan"],
                 relocation=False)
    assert persona.subject("apply", "ru", p, role="Backend") == "Отклик: Backend — Иван Петров"
    assert persona.subject("direct", "en", p, about="QA at Acme") == "QA at Acme — Ivan Petrov"
    line = persona.text("format_line", "ru", p)
    assert "удалённо" in line and "офис" in line and "Казань" in line
    assert "Москв" not in line
    assert persona.cv_file_prefix(p) == "Petrov"
    assert persona.location("ru", p) == "Казань, Россия"
    years = p.claims.get("total_years_software")
    assert persona.pool("intros", "ru", p) == ["%d+ лет в разработке." % years]


def test_office_city_follows_profile(monkeypatch):
    from jobhunter.match import workformat
    kazan = _profile(location="Казань, Россия", work_formats=["remote", "office"], relocation=False)
    monkeypatch.setattr(persona, "get_profile", lambda: kazan)
    assert workformat.onsite_ok("Офис в Казани, гибрид")
    assert not workformat.onsite_ok("Офис в Москве")
    assert not workformat.onsite_ok("Офис в Казани, релокация в Дубай")


def test_remote_only_candidate_accepts_no_office(monkeypatch):
    from jobhunter.match import workformat
    remote = _profile(work_formats=["remote"], relocation=True)
    monkeypatch.setattr(persona, "get_profile", lambda: remote)
    assert not workformat.onsite_ok("Офис в Москве, м. Павелецкая")


def test_relocation_ready_candidate_accepts_any_office(monkeypatch):
    from jobhunter.match import workformat
    mover = _profile(work_formats=["remote", "office"], relocation=True)
    monkeypatch.setattr(persona, "get_profile", lambda: mover)
    assert workformat.onsite_ok("Onsite in Belgrade, relocation package")


@pytest.mark.parametrize("draft, bad", [
    ("Готов к переезду, если потребуется.", True),
    ("Работаю только удалённо.", True),
    ("Работаю удалённо или в офисе.", False),
])
def test_stance_guard_reads_relocation_and_formats(draft, bad):
    from jobhunter.convo.draft import _tech_stance_problem
    assert bool(_tech_stance_problem("Готовы к офису?", draft)) == bad


def test_cold_telegram_is_off_unless_enabled(monkeypatch, tmp_path):
    from jobhunter.config import get_settings
    from jobhunter.outreach import policy
    monkeypatch.setenv("KILL_SWITCH_PATH", str(tmp_path / "no_stop.flag"))
    monkeypatch.setenv("TELEGRAM_COLD_ENABLED", "false")
    get_settings.cache_clear()
    # Выключатель срабатывает раньше любого обращения к базе.
    v = policy.can_send_cold(None)
    assert not v.allowed and "TELEGRAM_COLD_ENABLED" in v.reason
    assert get_settings().telegram_cold_enabled is False
