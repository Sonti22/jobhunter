"""Только удалённая работа — жёсткое условие владельца.

Два независимых уровня: вакансия с офисом и без единого упоминания удалёнки
не попадает в отклики вовсе, а в каждом письме условие названо прямо. Второй
уровень нужен потому, что 29% собранных вакансий формат не указывают: их не
режем, но рекрутёр узнаёт ограничение из первой же строки, а не после созвона.
"""
from jobhunter.match import workformat
from jobhunter.match.scorer import score_job
from jobhunter.tailor.message import FORMAT_LINE, generate

PY_JD = ("Требуется Python-разработчик. Опыт работы с FastAPI, PostgreSQL, "
         "Docker и Kubernetes. Проектирование архитектуры сервисов.")


def test_explicit_remote():
    assert workformat.detect("Формат работы: удалённо") == workformat.REMOTE
    assert workformat.detect("Fully remote position") == workformat.REMOTE
    assert workformat.detect("работа из дома") == workformat.REMOTE


def test_explicit_onsite():
    assert workformat.detect("Формат работы: в офисе (м. Павелецкая)") \
        == workformat.ONSITE
    assert workformat.detect("Помогают с релокацией (Сингапур)") \
        == workformat.ONSITE
    assert workformat.detect("гибрид/офис по желанию (Мск)") == workformat.ONSITE


def test_mixed_counts_as_remote():
    """«Удалённо/Гибрид/Офис» — удалёнку предлагают, выбор за кандидатом.

    В базе таких 493 против 397 чисто офисных: считать их офисом значило бы
    выбросить больше подходящих вакансий, чем отсечь неподходящих.
    """
    assert workformat.detect("Формат работы: Удаленно/Гибрид/Офис") \
        == workformat.REMOTE
    assert workformat.detect(
        "Удаленная работа полностью или гибридный вариант, Барселона") \
        == workformat.REMOTE


def test_silence_is_not_onsite():
    """Молчание о формате — не отказ: так написаны 29% вакансий."""
    assert workformat.detect("Python Backend, FastAPI, зарплата по итогам") \
        == workformat.UNKNOWN


def test_moscow_office_is_recommended_since_24_09():
    """Резюме владельца 24.09: живёт в Москве, офис и гибрид в Москве подходят."""
    for text in (" Формат работы: в офисе, м. Павелецкая.",
                 " Гибрид, офис в Москве."):
        moscow = score_job("Python-разработчик", "", PY_JD + text)
        assert not moscow.onsite_only, text
        assert moscow.recommend, text
        assert "Москве — подходит" in moscow.reason


def test_office_elsewhere_or_relocation_is_not_recommended():
    """К переезду владелец не готов: чужой город и релокация — отказ."""
    for text in (" Формат работы: в офисе в Санкт-Петербурге.",
                 " Офис в Москве, помогаем с релокацией из регионов.",
                 " Onsite in Belgrade, relocation package."):
        job = score_job("Python-разработчик", "", PY_JD + text)
        assert job.onsite_only, text
        assert not job.recommend, text


def test_remote_job_still_recommended():
    remote = score_job("Python-разработчик", "",
                       PY_JD + " Формат работы: удалённо.")
    assert not remote.onsite_only
    assert remote.recommend


def test_silent_job_still_recommended():
    """Не указан формат — отклик уходит, ограничение называет письмо."""
    quiet = score_job("Python-разработчик", "", PY_JD)
    assert quiet.work_format == workformat.UNKNOWN
    assert quiet.recommend


def test_letter_always_states_the_constraint():
    score = score_job("Python-разработчик", "", PY_JD)
    for seed in ("a1", "b2", "c3", "d4", "e5"):
        msg = generate("Python-разработчик", PY_JD, score, seed_str=seed,
                       source="ваш пост в @pyjobs")
        assert FORMAT_LINE in msg.text, "условие пропало при seed=%s" % seed
        assert "удал" in msg.text.lower()
        assert "C2" in msg.text


def test_unknown_source_leaves_no_empty_parens():
    """Раньше пустой источник давал «(вакансия)» — сломанный шаблон на виду."""
    score = score_job("Программист", "", PY_JD)
    msg = generate("Программист", PY_JD, score, seed_str="x", source="")
    assert "()" not in msg.text
    assert "(вакансия)" not in msg.text


def test_remote_mention_is_not_remote_format():
    """«Удалённый доступ» и «Remote: No» — не удалёнка (найдено аудитом)."""
    assert workformat.detect(
        "настройка удалённого доступа к серверам, офис м. Павелецкая")         == workformat.ONSITE
    assert workformat.detect("работа с удаленными филиалами, офисный формат")         == workformat.ONSITE
    assert workformat.detect("Remote: No. Onsite in Berlin") == workformat.ONSITE
    assert workformat.detect("no remote work allowed") == workformat.ONSITE
    assert workformat.detect("полностью удалённая работа") == workformat.REMOTE
