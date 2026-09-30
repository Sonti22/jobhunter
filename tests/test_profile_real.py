"""Настоящий profile.yaml владельца, если он есть в этой копии.

Остальные тесты работают на вымышленном profile.example.yaml. Этот — единственный,
который смотрит на живой профиль: тексты о кандидате из outreach должны
проходить тот же гейт правды, что и письма. В копии без личного профиля
(открытый репозиторий, CI) тест пропускается.
"""
from pathlib import Path

import pytest
import yaml

from jobhunter import persona
from jobhunter.profile import Profile
from jobhunter.tailor.gate import DocModel, check

REAL = Path(__file__).resolve().parents[1] / "profile.yaml"
pytestmark = pytest.mark.skipif(not REAL.exists(), reason="нет profile.yaml — копия без личного профиля")


@pytest.mark.parametrize("key", ["intros", "intros_short", "intros_middle", "format_replies"])
@pytest.mark.parametrize("lang", ["ru", "en"])
def test_real_profile_texts_pass_the_truth_gate(key, lang):
    p = Profile(yaml.safe_load(REAL.read_text(encoding="utf-8")))
    for text in persona.pool(key, lang, p):
        doc = DocModel(lang=lang, kind="message", free_text=text, rendered_bullets=[])
        res = check(doc, jd_text="", profile=p)
        assert res.passed, (text, res)
