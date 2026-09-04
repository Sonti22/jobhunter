"""Самопроверка текста перед отправкой человеку.

Гейт отвечает «не выдумано ли». Здесь второй вопрос — «читается ли»:
правдивое косноязычие тратит единственную попытку у рекрутёра так же, как
выдумка, и второго письма по той же вакансии не будет.

Тесты не ходят в сеть: ответ модели подменяется.
"""
import json

import pytest


@pytest.fixture()
def llm(monkeypatch):
    """Подменяет ответ модели. Возвращает список заданных ответов."""
    from jobhunter import llm as llm_mod
    from jobhunter.config import get_settings
    from jobhunter.tailor import review as review_mod

    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()

    queue = []

    def fake_generate(prompt, timeout=30.0):
        payload = queue.pop(0) if queue else {"ok": True, "reason": "", "improved": ""}
        if isinstance(payload, Exception):
            raise payload
        return llm_mod.LLMResult(json.dumps(payload, ensure_ascii=False),
                                 provider="fake")

    monkeypatch.setattr(review_mod, "generate", fake_generate)
    yield queue
    get_settings.cache_clear()


GOOD = ("Здравствуйте! Пишу по вакансии Python Backend. Семь лет на "
        "FastAPI и PostgreSQL, последние два — техлид. Актуальна ли позиция?")


def test_good_text_passes(llm):
    from jobhunter.tailor.review import review

    llm.append({"ok": True, "reason": "по делу", "improved": ""})
    v = review(GOOD, role="Python Backend")
    assert v.ok and v.checked


def test_bad_text_rejected_with_reason(llm):
    from jobhunter.tailor.review import review

    llm.append({"ok": False, "reason": "мысль обрывается", "improved": ""})
    v = review("Здравствуйте, я по поводу и", role="Python Backend")
    assert not v.ok and v.needs_owner
    assert "обрывается" in v.reason


def test_llm_off_means_not_checked_not_bad(monkeypatch):
    """Выключенная модель не должна останавливать рассылку."""
    from jobhunter.config import get_settings
    from jobhunter.tailor.review import review

    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()
    try:
        v = review(GOOD)
        assert v.ok and not v.checked and not v.needs_owner
    finally:
        get_settings.cache_clear()


def test_unparseable_answer_is_not_a_verdict(llm, monkeypatch):
    """Модель ответила мусором — это не приговор тексту."""
    from jobhunter import llm as llm_mod
    from jobhunter.tailor import review as review_mod

    monkeypatch.setattr(review_mod, "generate",
                        lambda *a, **k: llm_mod.LLMResult("извините, не могу",
                                                          provider="fake"))
    v = review_mod.review(GOOD)
    assert v.ok and not v.checked


def test_improved_text_must_pass_gate(llm):
    """Переписанный вариант — такой же подозреваемый, как исходный.

    Иначе самопроверка становится дырой, через которую выдумки попадают в
    письмо в обход единственной защиты от вранья.
    """
    from jobhunter.tailor.review import review_with_retry

    llm.append({"ok": False, "reason": "сухо",
                "improved": "Здравствуйте! 15 лет на Kubernetes и Rust."})
    text, verdict = review_with_retry(GOOD, role="Python Backend",
                                      gate_check=lambda t: False)
    assert text == GOOD, "текст с выдумкой не должен подменить оригинал"
    assert not verdict.ok
    assert "гейт" in verdict.reason


def test_improved_text_accepted_when_clean(llm):
    from jobhunter.tailor.review import review_with_retry

    better = "Здравствуйте! Семь лет на FastAPI. Актуальна ли вакансия?"
    llm.append({"ok": False, "reason": "длинно", "improved": better})
    llm.append({"ok": True, "reason": "теперь ясно", "improved": ""})
    text, verdict = review_with_retry(GOOD, role="Python Backend",
                                      gate_check=lambda t: True)
    assert text == better
    assert verdict.ok


def test_two_rejections_escalate(llm):
    """Дважды забраковали и улучшить нечем — решает владелец."""
    from jobhunter.tailor.review import review_with_retry

    llm.append({"ok": False, "reason": "непонятно", "improved": ""})
    text, verdict = review_with_retry("бла бла", role="Python Backend",
                                      gate_check=lambda t: True)
    assert text == "бла бла"
    assert verdict.needs_owner


def test_json_in_code_fence_parsed(llm, monkeypatch):
    """Модели любят обрамлять JSON тройными кавычками."""
    from jobhunter import llm as llm_mod
    from jobhunter.tailor import review as review_mod

    raw = '```json\n{"ok": false, "reason": "вода", "improved": ""}\n```'
    monkeypatch.setattr(review_mod, "generate",
                        lambda *a, **k: llm_mod.LLMResult(raw, provider="fake"))
    v = review_mod.review(GOOD)
    assert not v.ok and v.reason == "вода"


def test_review_failure_blocks_auto_approve(tmp_path, monkeypatch):
    """Забракованная заявка не должна уходить автопилотом."""
    import uuid

    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None

    from sqlalchemy import select

    from jobhunter.models import Application, ContactKind, Job, Status

    try:
        with dbmod.session_scope() as sess:
            for note in ("", "мысль обрывается"):
                job = Job(external_uuid=str(uuid.uuid4()), source="test",
                          title="Python", contact_kind=ContactKind.USER_HANDLE.value,
                          contact_handle="hr")
                sess.add(job)
                sess.flush()
                sess.add(Application(job_id=job.id, score=90, gate_passed=True,
                                     status=Status.PENDING_APPROVAL.value,
                                     review_note=note))

        from jobhunter.autopilot import AUTO_APPROVE_MIN_SCORE
        with dbmod.session_scope() as sess:
            eligible = sess.scalars(
                select(Application)
                .where(Application.status == Status.PENDING_APPROVAL.value,
                       Application.score >= AUTO_APPROVE_MIN_SCORE,
                       Application.review_note == "")).all()
        assert len(eligible) == 1
        assert eligible[0].review_note == ""
    finally:
        get_settings.cache_clear()
        dbmod._engine = None
        dbmod._Session = None
