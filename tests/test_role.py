"""Классификатор роли и пресеты резюме.

Проверяем то, что реально сломалось в проде: вакансия дата-аналитика получала
backend-резюме, а имя файла бралось из тега вакансии, а не из роли.

Запуск:  python -m pytest tests/test_role.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from jobhunter.match.role import SUPPORTED, classify
from jobhunter.match.scorer import score_job
from jobhunter.profile import get_profile
from jobhunter.tailor import presets
from jobhunter.tailor.select import tailor

P = get_profile()


# ── классификатор ────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,tag,body,expected", [
    ("Аналитик данных / Data Analyst", "analytics",
     "Уверенный SQL, PostgreSQL, витрины, Airflow, DWH", "data_engineer"),
    ("Системный аналитик", "analytics",
     "描述: BPMN, UML, постановка задач разработчикам, OpenAPI", "analyst"),
    ("Senior DevOps Engineer", "devops",
     "Kubernetes, Helm, GitLab CI, Prometheus, дежурства", "devops"),
    ("Product Manager", "product",
     "Roadmap, discovery, продуктовые метрики, гипотезы", "product"),
    ("QA Automation Engineer", "qa",
     "Автотесты на pytest, selenium, регресс", "qa"),
    ("Руководитель проекта", "management",
     "Сроки проекта, проектная документация, координация команд", "project"),
    ("Senior Python Backend Developer", "python",
     "FastAPI, Django, PostgreSQL, REST API, микросервисы", "backend"),
    ("Team Lead / Архитектор", "python",
     "Проектирование систем, техдолг, ревью, менторинг", "architect"),
    ("ML Engineer", "ml",
     "PyTorch, инференс, computer vision, YOLO", "ml"),
])
def test_family(title, tag, body, expected):
    assert classify(title, tag, body).family == expected


@pytest.mark.parametrize("title,body", [
    ("Senior Frontend Developer", "React, Redux, TypeScript, вёрстка"),
    ("iOS-разработчик", "Swift, SwiftUI, App Store"),
    ("Аналитик ИБ", "SIEM, уязвимости, пентест, OWASP"),
    ("Разработчик 1С", "1С, Битрикс, РСБУ"),
    ("Product Designer", "Figma, UX, прототипы интерфейсов"),
    ("Account Executive", "Sales manager, business development, pre-sales"),
    ("Специалист службы поддержки", "Техническая поддержка пользователей"),
])
def test_unsupported_not_sent(title, body):
    """Роли без базы не должны попадать в SUPPORTED — отклик не уйдёт."""
    m = classify(title, "", body)
    assert m.family not in SUPPORTED, "%s классифицирован как %s" % (title, m.family)
    assert m.reason(), "у неподдерживаемой роли должна быть причина отказа"


def test_weak_signal_is_unknown():
    """Одно случайное слово в теле — не роль."""
    m = classify("Специалист", "", "У нас есть микросервисы и мы любим кофе")
    assert m.family == "unknown"


def test_title_outweighs_body():
    """Упоминание Kubernetes в теле не должно уводить бэкенд в DevOps.

    Ровно этот баг и был: _pick_headline проверял DevOps первым по цепочке
    if/elif, поэтому любое совпадение выигрывало.
    """
    m = classify("Senior Backend Developer (Python)", "python",
                 "FastAPI, PostgreSQL. Сервисы катятся в Kubernetes, CI в GitLab.")
    assert m.family == "backend"


def test_product_role_does_not_get_engineering_role_bonus():
    """Product остаётся допустимым семейством, но не поднимается как backend."""
    product = score_job("Product Manager", "product", "SQL, REST API")
    backend = score_job("Backend Engineer", "backend", "SQL, REST API")
    assert product.total < backend.total


# ── пресеты ──────────────────────────────────────────────────────────────

def test_every_supported_family_has_preset():
    for family in SUPPORTED:
        assert presets.get(family), "нет пресета под семейство %s" % family


def test_preset_headlines_exist_in_profile():
    """Заголовок берётся только из списка, одобренного кандидатом."""
    allowed = {h.lower() for h in P.identity["headline_variants"]}
    for key, pr in presets.PRESETS.items():
        assert pr.headline.lower() in allowed, \
            "%s: заголовка %r нет в headline_variants" % (key, pr.headline)


def test_preset_skills_exist_in_profile():
    for key, pr in presets.PRESETS.items():
        for sid in list(pr.boost_skills) + list(pr.skill_order):
            assert sid in P.skill_by_id, "%s: навыка %r нет в profile.yaml" % (key, sid)


def test_preset_slugs_unique():
    slugs = [pr.cv_slug for pr in presets.PRESETS.values()]
    assert len(slugs) == len(set(slugs))


# ── сквозная сборка ──────────────────────────────────────────────────────

JD = {
    "data_engineer": ("Data Engineer", "data",
                      "Требования: продвинутый SQL, PostgreSQL, DWH, Kafka, "
                      "миграции, оптимизация запросов, Python."),
    "devops": ("DevOps Engineer", "devops",
               "Kubernetes, Helm, GitLab CI, Prometheus, Grafana, Linux, дежурства."),
    "product": ("Technical Product Manager", "product",
                "Roadmap, приоритизация, работа со стейкхолдерами, метрики, REST API."),
    "qa": ("QA Automation Engineer", "qa",
           "Автотесты pytest, API-тесты, CI, регрессионное тестирование."),
    "analyst": ("Системный аналитик", "analytics",
                "Требования, OpenAPI, интеграции, техническая документация, SQL."),
    "architect": ("Software Architect", "python",
                  "Микросервисы, DDD, CQRS, event sourcing, ревью, менторинг."),
    "backend": ("Senior Python Developer", "python",
                "FastAPI, Django, PostgreSQL, REST API, Docker, микросервисы."),
    "ml": ("ML Systems Engineer", "ml",
           "Инференс LLM, computer vision, OpenCV, YOLO, видео-пайплайны, профилирование."),
    "project": ("Руководитель проектов", "management",
                "Планирование, сроки, риски, координация команды, Agile."),
}


@pytest.mark.parametrize("family", sorted(JD))
def test_tailor_passes_gate_for_every_preset(family):
    title, tag, body = JD[family]
    res = tailor(title, tag, body)
    assert res.role.family == family, "%s → %s" % (family, res.role.family)
    assert res.ok, "гейт уронил резюме под %s: %s" % (
        family, [f.rule_id for f in res.gate.hard])
    assert res.cv_slug == presets.PRESETS[family].cv_slug
    assert res.render["headline"] == presets.PRESETS[family].headline


def test_cv_slug_not_taken_from_tag():
    """Тег источника не должен попадать в имя файла.

    Канал python_djangojobs ставит тег «Python» продуктовым вакансиям — из-за
    этого PM-резюме уходило рекрутёру файлом Hakobyan_Python_*.pdf.
    """
    res = tailor("Product Manager", "Python",
                 "Roadmap, discovery, приоритизация, работа со стейкхолдерами.")
    assert res.cv_slug == "ProductManager"


def test_middle_headline_has_no_senior():
    res = tailor("Middle Python Developer", "python",
                 "Middle разработчик. FastAPI, PostgreSQL, Docker.")
    assert res.score.is_middle
    assert not any(w in res.render["headline"].lower()
                   for w in ("senior", "lead", "architect", "manager"))
