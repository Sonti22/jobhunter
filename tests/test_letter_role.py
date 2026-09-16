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
