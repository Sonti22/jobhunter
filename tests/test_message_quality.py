"""Честность и читаемость сопроводительного письма.

Гейт проверяет факты. Эти тесты — про два других способа испортить отклик:
подтвердить требование, которого за кандидатом нет, и отправить косноязычный
машинный текст. Оба случая взяты из реально сгенерированных писем.

Запуск:  python -m pytest tests/test_message_quality.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from jobhunter.match.scorer import score_job
from jobhunter.profile import get_profile
from jobhunter.tailor.gate import _find_terms
from jobhunter.tailor.llm_writer import quality_problem
from jobhunter.tailor.message import _extract_requirement

P = get_profile()


# ── цитата требования подтверждается опытом ──────────────────────────────

def _quote(jd: str, title: str = "") -> str:
    score = score_job(title, "", jd, P)
    return _extract_requirement(jd, score.matched_skills, P, title)


def test_quote_requires_known_term():
    """Требование без знакомого термина не цитируется.

    Реальный случай: «Зацепило требование: "Опыт вайбкодинга, подтверждённый
    портфолио" — это ровно то, чем занимался последние годы». Запрещённых
    технологий в строке нет, гейт молчит, а заявление ложное.
    """
    jd = ("Требования:\n"
          "- Опыт вайбкодинга, подтверждённый портфолио\n"
          "- Опыт работы Product Manager / Product Owner\n")
    assert _quote(jd) == ""


def test_quote_picked_when_backed():
    jd = ("Требования:\n"
          "- Опыт вайбкодинга, подтверждённый портфолио\n"
          "- Уверенное владение Python и PostgreSQL, опыт с Docker\n")
    q = _quote(jd)
    assert q, "строка с Python/PostgreSQL обязана подойти в цитату"
    assert _find_terms(q) & P.allowed_terms


def test_quote_never_contains_forbidden():
    jd = ("Требования:\n"
          "- Опыт коммерческой разработки на C# и .NET, знание Python\n")
    q = _quote(jd)
    assert "c#" not in q.lower() and ".net" not in q.lower()


def test_quote_skips_company_type_requirement():
    """Тип компании — не навык.

    «опыт работы в продуктовой компании, чей основной продукт — скрапер,
    парсер или агрегатор данных»: знакомый термин в строке есть («данных»),
    запрещённых нет, но ответ «работал с этим» — ложь про место работы.
    """
    jd = ("Требования:\n"
          "- опыт работы в продуктовой компании, чей основной продукт - "
          "скрапер, парсер или агрегатор данных\n")
    assert _quote(jd) == ""

    jd2 = jd + "- Уверенный Python и PostgreSQL, опыт с Docker\n"
    assert "Python" in _quote(jd2)


def test_quote_skips_tenure_requirement():
    """Стаж не цитируем: ответ «делал это» превращается в заявление о годах."""
    jd = "Требования:\n- Опыт коммерческой разработки на Python от 10 лет\n"
    assert _quote(jd) == ""


# ── читаемость текста ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,marker", [
    ("У меня есть опыт работы с технологиями如 Python, Django.", "чужое письмо"),
    ('Работал с "SQL" и подтверждаю, что это технология, с которой я знаком.',
     "пустая формулировка"),
    ('Работал с требованиями, что соответствует одному из требований вакансии.',
     "пустая формулировка"),
    ("Есть опыт (в бэкенде. Актуальна ли вакансия?", "непарные"),
    ('Работал с требованиями, как указано в требовании "Работа с требованиями".',
     "пустая формулировка"),
])
def test_quality_rejects(text, marker):
    problem = quality_problem(text)
    assert problem, "текст должен быть отклонён: %r" % text
    assert marker in problem


@pytest.mark.parametrize("text", [
    "По вакансии «Senior Python Developer» (ваш пост в @python_djangojobs). "
    "7 лет в бэкенде, последние годы — Tech Lead. Актуальна ли вакансия?",
    "Мой стек по вашим требованиям: Django, FastAPI, PostgreSQL. "
    "Готов прислать резюме и обсудить детали — когда удобно?",
])
def test_quality_accepts_normal_text(text):
    assert quality_problem(text) == ""
