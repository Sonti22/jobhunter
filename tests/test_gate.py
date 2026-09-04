"""Состязательные тесты гейта. Каждый кейс обязан уронить конкретный rule_id.

Запуск:  python -m pytest tests/test_gate.py -q
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from jobhunter.profile import get_profile
from jobhunter.tailor.gate import DocModel, check

P = get_profile()


def rule_ids(res):
    return {f.rule_id for f in res.failures}


# ── helper: валидный буллет из профиля ──
def real_bullet(sid, lang="ru"):
    b = P.bullet_by_id[sid]
    return {"source_id": sid, "text": (b.text_ru if lang == "ru" else b.text_en),
            "section": "experience"}


# ═══════════════════════ ЧИСТЫЕ (должны пройти) ═══════════════════════

def test_clean_cv_passes():
    doc = DocModel(lang="ru", headline="Senior Technical Product Manager",
                   summary="Backend-разработчик, вырос в Tech Lead.",
                   rendered_bullets=[real_bullet("b_1xb_1"), real_bullet("b_dmg_1")],
                   companies=["1xbet", "DominiGames"])
    res = check(doc)
    assert res.passed, rule_ids(res)


def test_clean_paraphrase_passes():
    # переформулировка в пределах порога провенанса
    doc = DocModel(lang="ru",
                   rendered_bullets=[{"source_id": "b_1xb_1",
                       "text": "Писал и расширял REST API для внутренних модулей "
                               "(проекты, сотрудники, локации) на FastAPI и Django REST Framework.",
                       "section": "experience"}],
                   companies=["1xbet"])
    res = check(doc)
    assert res.passed, rule_ids(res)


# ═══════════════════════ ФАБРИКАЦИИ (должны упасть) ═══════════════════════

def test_forbidden_csharp():
    doc = DocModel(lang="ru", headline="C# / .NET разработчик",
                   rendered_bullets=[real_bullet("b_ksp_1")], companies=["Лаборатория Касперского"])
    res = check(doc)
    assert not res.passed
    assert "lexicon.forbidden" in rule_ids(res)


def test_forbidden_dotnet_synonym():
    doc = DocModel(lang="en", summary="Built services in ASP.NET Core.",
                   rendered_bullets=[real_bullet("b_ksp_1", "en")],
                   companies=["Kaspersky Lab"])
    res = check(doc)
    assert "lexicon.forbidden" in rule_ids(res)


def test_forbidden_github_actions():
    doc = DocModel(lang="ru", summary="Настраивал GitHub Actions для CI.",
                   rendered_bullets=[real_bullet("b_dmg_4")], companies=["DominiGames"])
    res = check(doc)
    assert "lexicon.forbidden" in rule_ids(res)


def test_forbidden_java_spring():
    doc = DocModel(lang="ru", headline="Java / Spring Boot Engineer",
                   rendered_bullets=[real_bullet("b_dmg_1")], companies=["DominiGames"])
    assert "lexicon.forbidden" in rule_ids(check(doc))


def test_unknown_tech_terraform():
    # terraform в never_claim → forbidden; проверим именно unknown на чём-то вне обоих
    doc = DocModel(lang="ru", summary="Работал с CockroachDB в проде.",
                   rendered_bullets=[real_bullet("b_dmg_1")], companies=["DominiGames"])
    res = check(doc)
    assert "lexicon.unknown" in rule_ids(res)


def test_invented_number_metric():
    doc = DocModel(lang="ru",
                   rendered_bullets=[{"source_id": "b_dmg_5",
                       "text": "Профилировал код и увеличил выручку на 47%.",
                       "section": "experience"}], companies=["DominiGames"])
    res = check(doc)
    assert "numbers.unbacked" in rule_ids(res)


def test_invented_multiplier():
    # множитель «в 8 раз» не подтверждён метрикой-множителем (есть только 2x у p95)
    doc = DocModel(lang="ru", summary="Вырастил выручку в 8 раз.",
                   rendered_bullets=[real_bullet("b_dmg_1")], companies=["DominiGames"])
    res = check(doc)
    assert "numbers.unbacked" in rule_ids(res)


def test_backed_multiplier_2x_passes():
    # «в 2 раза» подтверждён метрикой p95 (value 2, unit x)
    doc = DocModel(lang="ru", summary="Сократил p95 в 2 раза.",
                   rendered_bullets=[real_bullet("b_dmg_5")], companies=["DominiGames"])
    res = check(doc)
    assert "numbers.unbacked" not in rule_ids(res), rule_ids(res)


def test_backed_number_passes():
    # p95 в 2 раза — есть в metrics b_dmg_5
    doc = DocModel(lang="ru", rendered_bullets=[real_bullet("b_dmg_5")],
                   companies=["DominiGames"])
    res = check(doc)
    assert "numbers.unbacked" not in rule_ids(res)


def test_skill_years_inflation():
    doc = DocModel(lang="ru", summary="10 лет Kubernetes в проде.",
                   rendered_bullets=[real_bullet("b_dmg_4")], companies=["DominiGames"])
    res = check(doc)
    assert "years.skill_inflation" in rule_ids(res)


def test_level_inflation_familiar_in_headline():
    # cuda — familiar; в заголовке нельзя
    doc = DocModel(lang="ru", headline="Эксперт по CUDA и TensorRT",
                   rendered_bullets=[real_bullet("b_ksp_8")], companies=["Лаборатория Касперского"])
    res = check(doc)
    assert "lexicon.level_inflation" in rule_ids(res)


def test_level_inflation_superlative():
    # mcp — familiar; рядом «глубокий»
    doc = DocModel(lang="ru",
                   rendered_bullets=[{"source_id": "b_rnd_3",
                       "text": "Реализовал обработку команд через websocket-адаптер; "
                               "глубокий MCP-опыт.", "section": "experience"}],
                   companies=[])
    res = check(doc)
    assert "lexicon.level_inflation" in rule_ids(res)


def test_invented_company():
    doc = DocModel(lang="ru", rendered_bullets=[real_bullet("b_ksp_1")],
                   companies=["Яндекс"])
    res = check(doc)
    assert "entity.unknown_company" in rule_ids(res)


def test_bullet_without_source():
    doc = DocModel(lang="ru",
                   rendered_bullets=[{"text": "Руководил командой из 20 человек.",
                                      "section": "experience"}], companies=[])
    res = check(doc)
    assert "provenance.no_source" in rule_ids(res)


def test_bullet_bad_source():
    doc = DocModel(lang="ru",
                   rendered_bullets=[{"source_id": "b_nonexistent",
                                      "text": "Что-то делал.", "section": "experience"}])
    res = check(doc)
    assert "provenance.bad_source" in rule_ids(res)


def test_provenance_drift():
    # source_id настоящий, но текст не имеет к нему отношения
    doc = DocModel(lang="ru",
                   rendered_bullets=[{"source_id": "b_dmg_1",
                       "text": "Организовывал корпоративы и заказывал пиццу для офиса.",
                       "section": "experience"}], companies=["DominiGames"])
    res = check(doc)
    assert "provenance.drift" in rule_ids(res)


def test_dm_inherits_gate_forbidden():
    # письмо (kind=message) тоже не может заявить C#
    doc = DocModel(lang="ru", kind="message",
                   free_text="Здравствуйте! По вакансии PM. Пишу на Python и C#.",
                   rendered_bullets=[])
    res = check(doc)
    assert "lexicon.forbidden" in rule_ids(res)


def test_promotion_recorded():
    # JD просит Kafka (working у Сурена) — промоушен фиксируется, не хард
    jd = "We need strong Kafka and PostgreSQL experience."
    doc = DocModel(lang="en", summary="Set up interaction with Kafka and PostgreSQL.",
                   rendered_bullets=[real_bullet("b_ksp_6", "en")],
                   companies=["Kaspersky Lab"])
    res = check(doc, jd_text=jd)
    assert res.passed, rule_ids(res)
    assert "kafka" in res.promoted_terms


def test_jd_wants_csharp_but_cv_stays_clean():
    # ключевой кейс: JD требует C#/.NET, но резюме их НЕ содержит → проходит
    jd = "Senior role. Stack: C#, .NET, PostgreSQL, GitHub Actions."
    doc = DocModel(lang="ru", headline="Senior Technical Product Manager",
                   summary="Владею PostgreSQL, проектирую API.",
                   rendered_bullets=[real_bullet("b_1xb_4")], companies=["1xbet"])
    res = check(doc, jd_text=jd)
    assert res.passed, rule_ids(res)
    # и наоборот: если бы просочился C#, упало бы
    doc2 = DocModel(lang="ru", headline="C# Technical Product Manager",
                    rendered_bullets=[real_bullet("b_1xb_4")], companies=["1xbet"])
    assert not check(doc2, jd_text=jd).passed


# ═════════════ ПОДГОНКА ПОД ВАКАНСИЮ НЕ ЛОМАЕТ ЧЕСТНОСТЬ ═════════════
# Пользователь просил «дописывать под вакансию». Эти тесты фиксируют, что
# подгонка = переранжирование, а не фабрикация: движок tailor() на любой
# вакансии обязан выдать документ, проходящий гейт.

def test_tailor_csharp_job_stays_clean():
    from jobhunter.tailor.select import tailor
    jd = ("Senior Technical Product Manager. Stack: C#, .NET, API, PostgreSQL, "
          "GitHub Actions, Kibana, Grafana. 6+ years in software development. "
          "Experience with SaaS products: API, webhooks, JSON payloads, logs, "
          "error codes, permissions and integrations.")
    res = tailor("Senior Technical Product Manager", "Product Manager", jd)
    assert res.gate.passed, rule_ids(res.gate)
    text = " ".join([res.render["headline"], res.render["summary"]]
                    + res.render["skills"]).lower()
    for banned in ("c#", ".net", "dotnet", "github actions"):
        assert banned not in text, "просочилось: %s" % banned


def test_tailor_ml_job_uses_honest_headline():
    from jobhunter.tailor.select import tailor
    jd = ("ML Engineer. We need production ML: model inference, computer vision, "
          "OpenCV, CUDA, TensorRT, RTSP video pipelines, Python, Docker.")
    res = tailor("ML Engineer", "DS / ML", jd)
    assert res.gate.passed, rule_ids(res.gate)
    # заголовок честный: интегратор ML-систем, не «7 лет ML-инженер»
    assert "ML Systems Integrator" in res.render["headline"]


def test_tailor_never_claims_seven_years_ml():
    from jobhunter.tailor.select import tailor
    jd = "ML Engineer with 7+ years of machine learning experience required."
    res = tailor("ML Engineer", "DS / ML", jd)
    assert res.gate.passed, rule_ids(res.gate)
    blob = (res.render["summary"] + " " + res.render["headline"]).lower()
    # не заявляем 7 лет именно ML — 7 лет относится к разработке ПО
    assert "7+ years in software engineering" in blob or "7+ лет в разработке" in blob


def test_pm_skills_now_present_in_profile():
    # регресс: до расширения профиля продуктовых компетенций было 0
    ids = {s.id for s in P.skills}
    for need in ("prioritization", "planning", "stakeholder_mgmt",
                 "team_leadership", "requirements"):
        assert need in ids, "нет навыка %s" % need


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
