# -*- coding: utf-8 -*-
"""A letter's first line names a job title, not a Telegram post's hashtags.

16.09, before re-enabling Telegram after five PeerFlood strikes, the queued
letters opened with «По вакансии «удаленно #DevOps»» and «Пишу по «devops #sre
#kubernetes #cicd…»» — bulk-mail tone that recipients report.
"""
from types import SimpleNamespace

import pytest

from jobhunter.tailor.roletitle import clean_title, display_role


@pytest.mark.parametrize("title,tag,want", [
    ("разработчик #python #remote #fulltime", "", "Backend-разработчик"),
    ("удаленно #DevOps", "DevOps", "DevOps-инженер"),
    ("devops #sre #kubernetes #cicd #observability", "DevOps", "DevOps-инженер"),
    ("backend #python #highload #remote #fintech", "Backend", "Backend-разработчик"),
    ("lead #гибрид #нижнийновгород", "DevOps", "DevOps-инженер"),
    ("120k #удаленка #офис #тюмень", "IT", "Backend-разработчик"),
])
def test_hashtag_titles_become_a_job_title(title, tag, want):
    assert display_role(title, tag, "python backend", "ru") == want


@pytest.mark.parametrize("title", [
    "Senior Python Engineer #remote", "Python-разработчик",
    "Главный системный администратор", "BACKEND DEVELOPER (PYTHON)",
])
def test_real_titles_are_kept(title):
    assert display_role(title, "", "", "ru") == title.replace(" #remote", "")


@pytest.mark.parametrize("raw", ["удаленно", "remote", "senior", "разработчик", "120k"])
def test_format_grade_and_bare_words_are_not_titles(raw):
    assert clean_title(raw) == ""


def test_generated_letter_never_quotes_hashtags(monkeypatch):
    from jobhunter.match.scorer import score_job
    from jobhunter.tailor.message import generate, source_label

    title = "удаленно #DevOps #kubernetes"
    body = "Ищем DevOps-инженера. Требования: Kubernetes, Docker, Python, CI/CD."
    score = score_job(title, "DevOps", body)
    role = display_role(title, "DevOps", body, "ru")
    msg = generate(role, body, score, seed_str="t1",
                   source=source_label("tg:devops_jobs_feed", lang="ru"), lang="ru")
    assert "#" not in msg.text.split(".")[0], msg.text
    assert "DevOps-инженер" in msg.text


def test_sender_skips_cached_group_chats(tmp_path, monkeypatch):
    import uuid

    monkeypatch.setenv("DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    from jobhunter.models import Application, HandleCache, Job, Status
    from jobhunter.outreach import sender

    monkeypatch.setattr(sender.eligibility, "check",
                        lambda *a, **kw: SimpleNamespace(allowed=True))
    with dbmod.session_scope() as sess:
        for handle in ("it_kz_chat", "real_recruiter"):
            job = Job(external_uuid=str(uuid.uuid4()), source="tg:x", title="Python",
                      contact_kind="user_handle", contact_handle=handle)
            sess.add(job)
            sess.flush()
            sess.add(Application(job_id=job.id, status=Status.APPROVED.value,
                                 score=90, gate_passed=True, message_body="Hi"))
        sess.add(HandleCache(handle_norm="it_kz_chat", last_error="not_a_user"))
    try:
        handles = [it["handle"] for it in sender.pick_batch(10)]
        assert handles == ["real_recruiter"], handles
    finally:
        get_settings.cache_clear()
        dbmod._engine = None
        dbmod._Session = None


def test_followup_quotes_job_title_not_hashtags(tmp_path, monkeypatch):
    """Напоминание «Поднимаю своё сообщение по «удаленно #DevOps»» — брак."""
    import uuid
    from datetime import timedelta

    monkeypatch.setenv("DB_PATH", str(tmp_path / "f.db"))
    monkeypatch.setenv("LLM_ENABLED", "false")
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    from jobhunter.models import Application, Job, Status, utcnow
    from jobhunter.outreach import followup

    with dbmod.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="tg:devops_jobs",
                  title="удаленно #DevOps", tag="DevOps", description_raw="kubernetes",
                  contact_kind="user_handle", contact_handle="recruiter")
        sess.add(job)
        sess.flush()
        app = Application(job_id=job.id, status=Status.AWAITING_REPLY.value,
                          score=80, gate_passed=True, message_body="Здравствуйте",
                          sent_at=utcnow() - timedelta(days=10), cv_lang="ru")
        sess.add(app)
        sess.flush()
        aid = app.id
    try:
        followup.prepare(dry=False)
        with dbmod.session_scope() as sess:
            body = sess.get(Application, aid).followup_body or ""
        assert body, "напоминание должно подготовиться"
        assert "#" not in body and "DevOps-инженер" in body, body
    finally:
        get_settings.cache_clear()
        dbmod._engine = None
        dbmod._Session = None


# ── 18.09: подписи с хештегами и должность из второй строки поста ──

@pytest.mark.parametrize("raw", ["limassol #cyprus #fintech #sysadmin", "воронеж", "астана #оффлайн #workITkz"])
def test_captions_and_locations_are_not_titles(raw):
    assert clean_title(raw) == ""


@pytest.mark.parametrize("title,tag,body,lang,want", [
    ("удаленно #fulltime #senior #python #backend #ai #финтех", "DevOps",
     "#вакансия #удаленно #fulltime\nИнженер-разработчик полного цикла (Python Backend + AI-агенты)\nОплата: 180 000",
     "ru", "Инженер-разработчик полного цикла"),          # хвост в скобках срезан: длиннее 60
    ("limassol #cyprus #fintech #sysadmin", "Python",
     "#vacancy #limassol #cyprus\n🚀 We’re hiring a Senior System Administrator | Cyprus\nWe’re looking for a strong admin.",
     "en", "Senior System Administrator"),
    ("devops #kubernetes #postgresql #middle #remote", "DevOps",
     "#вакансия #devops\nMiddle DevOps Engineer ×2 - Emerging Travel Group (RateHawk / Ostrovok)\nМеждународный travel-tech.",
     "ru", "DevOps-инженер"),                       # «devops» — известная короткая роль, тело не нужно
    ("воронеж", "DevOps", "#воронеж\nРУКОВОДИТЕЛЬ ИТ-ОТДЕЛА В ГК \"ПОРЯДОК\"\nОбязанности", "ru",
     "Руководитель ИТ-отдела"),
])
def test_role_comes_from_post_body_not_channel_tag(title, tag, body, lang, want):
    assert display_role(title, tag, body, lang) == want


def test_sentence_in_body_is_not_a_role():
    from jobhunter.tailor.roletitle import role_from_body
    assert role_from_body("Ищем DevOps-инженера. Требования: Kubernetes, Docker, Python.") == ""
    assert role_from_body("Middle DevOps Engineer ×2 - Emerging Travel Group") == "Middle DevOps Engineer"


def test_telegram_title_skips_hashtag_caption():
    from jobhunter.ingest.tgchannels import _first_line
    post = ("#вакансия #удаленно #fulltime #senior #python #backend\n"
            "Инженер-разработчик полного цикла (Python Backend + AI-агенты)\nОплата: 180 000 – 250 000 ₽")
    assert _first_line(post).startswith("Инженер-разработчик полного цикла")
    assert "РУКОВОДИТЕЛЬ ИТ-ОТДЕЛА" in _first_line("#воронеж\nРУКОВОДИТЕЛЬ ИТ-ОТДЕЛА В ГК \"ПОРЯДОК\"\nОбязанности")
    # подпись с должностью остаётся заголовком; пост из одних хештегов не теряет заголовок совсем
    assert _first_line("Senior Python Engineer #remote\nWe build things") == "Senior Python Engineer #remote"
    assert _first_line("#вакансия #москва #офис") != ""


def test_title_labels_and_emoji_tail_are_stripped():
    from jobhunter.ingest.tgchannels import _first_line
    assert clean_title("️Позиция: Data Science (Senior)") == "Data Science (Senior)"
    assert clean_title("Должность: Middle software engineer") == "Middle software engineer"
    # после подписи служебная строка заголовком не становится
    post = "#vacancy #python #poland\nEmployment: fulltime\nSenior Python Developer\nStack: Django"
    assert _first_line(post) == "Senior Python Developer"
    # должности в посте нет вовсе — остаётся прежний запасной заголовок из подписи
    assert _first_line("#vacancy #python\nEmployment: fulltime\nStack: Django, DRF") == "python"
