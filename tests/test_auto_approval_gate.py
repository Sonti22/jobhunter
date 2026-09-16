# -*- coding: utf-8 -*-
"""Automatic approval blocks on real mismatches, not on unverifiable words.

16.09: the requirement check treated every word the profile does not name as an
unverified condition. "Контакты:", "API", "JSON", e-mail addresses and phone
numbers blocked all 58 fresh candidates, and the e-mail queue starved. The
owner's review card stays strict; only the automatic-approval gate is relaxed.
"""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunter.match import explain
from jobhunter.profile import Profile

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE_RAW = json.loads((FIXTURES / "match_profile.json").read_text(encoding="utf-8"))


@pytest.fixture
def profile(monkeypatch):
    p = Profile(deepcopy(PROFILE_RAW))
    monkeypatch.setattr(explain, "get_profile", lambda: p)
    return p


def vacancy(title="Backend Engineer", body="Required: Python"):
    return SimpleNamespace(title=title, tag="", description_raw=body, salary_raw="",
                           raw_json={}, external_uuid="fixture", source="fixture",
                           posted_at=0, is_closed=False, contact_kind="email",
                           contact_url="hr@example.org")


APP = SimpleNamespace(review_note="")


@pytest.mark.parametrize("body", [
    "Требования:\n- Python\n- Контакты: hr@company.ru, +79991234567",
    "Required:\n- Python\n- REST API and JSON",
    "Требования:\n- Опыт коммерческой разработки на Python от 3 лет\n- Понимание TLS",
    "Требования:\n- Python\n- Удаленно (гражданство РФ)",
    "Требования:\n- Python\n- JWT, CORS, HTTP/HTTPS, OOP и SOLID\n- График работы: Понедельник",
    "Required:\n- Python 3.10+\n- Contact @hr_lead or https://t.me/hr_lead",
])
def test_unverifiable_words_do_not_block_auto_approval(profile, body):
    job = vacancy(body=body)
    assert explain.approval_problem(APP, job) == "", explain.approval_problem(APP, job)


def test_review_card_stays_strict(profile):
    """The owner's card still lists the unverified clause."""
    job = vacancy(body="Требования:\n- Python\n- Контакты: hr@company.ru")
    assert explain.explain_job(job, profile)["needs_review"]


@pytest.mark.parametrize("body,marker", [
    ("Required:\n- Python and AtlantisDB", "AtlantisDB"),        # product-shaped name
    ("Required:\n- Python\n- LangGraph", "LangGraph"),
    ("Required:\n- Snowflake", "snowflake"),                     # lexicon, not in profile
    ("Required:\n- Terraform", "вне профиля"),                    # never_claim
    ("Required:\n- NoneSDK", ""),                                  # level none
    ("Required:\n- Expert AWS", "знакомство"),                     # familiar only
    ("Required:\n- 5+ years of Kubernetes", "стаж"),              # 2 years in profile
    ("Required:\n- Python\n- Remote (US only)", "географии"),
    ("Required:\n- Experience training neural models", ""),        # ML research
])
def test_real_mismatches_still_block(profile, body, marker):
    problem = explain.approval_problem(APP, vacancy(body=body))
    assert problem, body
    assert marker in problem, problem


def test_non_requirement_reasons_still_block(profile):
    job = vacancy(title="Office manager", body="Требования: Excel")
    assert explain.explain_job(job, profile)["review_reasons"]
    assert explain.approval_problem(APP, job)


def test_review_note_still_blocks(profile):
    app = SimpleNamespace(review_note="самопроверка: письмо невнятное")
    assert "невнятное" in explain.approval_problem(app, vacancy())
