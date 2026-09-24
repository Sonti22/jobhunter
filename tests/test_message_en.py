"""Английское сопроводительное письмо.

Вакансии с 11 международных площадок шли в систему давно, но письмо к ним
писалось по-русски, а вложением уходил русский базовый PDF. Эти тесты
запрещают возврат обоих дефектов.
"""
import re

from jobhunter.match.scorer import score_job
from jobhunter.tailor.message import FORMAT_LINE_EN, generate

EN_JD = ("We are hiring a Senior Python Engineer. Experience with FastAPI, "
         "PostgreSQL, Docker and Kubernetes required. You will design "
         "service architecture and own technical decisions.")

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


def _score():
    return score_job("Senior Python Engineer", "", EN_JD)


def test_en_letter_has_no_cyrillic():
    for seed in ("a1", "b2", "c3", "d4"):
        msg = generate("Senior Python Engineer", EN_JD, _score(),
                       seed_str=seed, source="We Work Remotely", lang="en")
        assert not CYRILLIC.search(msg.text), \
            "кириллица в EN-письме (seed=%s): %r" % (seed, msg.text)


def test_en_letter_states_remote_constraint():
    msg = generate("Senior Python Engineer", EN_JD, _score(),
                   seed_str="x1", source="himalayas.app", lang="en")
    assert FORMAT_LINE_EN in msg.text
    assert "remote" in msg.text.lower()


def test_en_letter_passes_gate_and_length():
    msg = generate("Senior Python Engineer", EN_JD, _score(),
                   seed_str="x2", source="remoteok.com", lang="en")
    assert msg.gate.passed
    assert msg.ok


def test_en_letter_is_deterministic():
    a = generate("Senior Python Engineer", EN_JD, _score(),
                 seed_str="same", source="x", lang="en")
    b = generate("Senior Python Engineer", EN_JD, _score(),
                 seed_str="same", source="x", lang="en")
    assert a.text == b.text


def test_ru_letter_unchanged():
    """Регресс: русская ветка не задета языковым параметром."""
    jd = "Требуется Python-разработчик. Опыт FastAPI, PostgreSQL, Docker."
    sc = score_job("Python-разработчик", "", jd)
    msg = generate("Python-разработчик", jd, sc, seed_str="r1",
                   source="ваш пост в @pyjobs")
    assert "удалённо" in msg.text or "удаленно" in msg.text
    assert CYRILLIC.search(msg.text)


def test_base_cv_only_replaces_russian(tmp_path, monkeypatch):
    """Базовый русский PDF не подменяет английское резюме.

    До фикса BASE_CV_PATH подменял файл БЕЗ разбора языка, и англоязычный
    рекрутёр получал русское резюме.
    """
    import jobhunter.pipeline as pl

    src = (tmp_path / "base.pdf")
    src.write_bytes(b"%PDF-1.4 fake")

    text = open(pl.__file__, encoding="utf-8").read()
    assert 'res.lang == "ru"' in text.split("base_cv_path")[1][:200], \
        "подмена базового резюме обязана проверять язык"


def test_en_letter_never_quotes_cyrillic_requirement():
    """Смешанный пост: EN-тело + русские строки — цитата только латиницей.

    Реальный случай из очереди: «Matches “…обязательно”» и процитированная
    реклама «Подпишись на Python Jobs» посреди английского письма.
    """
    from jobhunter.profile import get_profile
    from jobhunter.tailor.message import _extract_requirement

    jd = ("Python + PyTest, backend/API automation experience is a must.\n"
          "Опыт работы с Python и PyTest — обязательно.\n"
          "Подпишись на Python Jobs!\n")
    sc = score_job("QA Automation Engineer", "", jd)
    req = _extract_requirement(jd, sc.matched_skills, get_profile(),
                               "QA Automation Engineer", lang="en")
    assert not re.search("[а-яА-ЯёЁ]", req), req
    msg = generate("QA Automation Engineer", jd, sc, seed_str="mix1",
                   source="your post in @pyjobs", lang="en")
    assert not CYRILLIC.search(msg.text), msg.text
