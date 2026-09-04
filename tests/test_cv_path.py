"""Резюме должно доезжать до рекрутёра.

Путь к файлу записывается в заявку при подготовке и живёт в базе месяцами.
Между подготовкой и отправкой корень проекта может смениться — так и вышло
при переезде в Docker: в заявках остались пути от корня диска Windows,
невидимые изнутри контейнера. Проверка exists() тихо давала False, и отклик
ушёл бы БЕЗ резюме, ничем не сообщив об этом. Отказ в самую неприятную
сторону: сообщение выглядит отправленным, а главного вложения в нём нет.
"""

import pytest


@pytest.fixture()
def cv(tmp_path, monkeypatch):
    base = tmp_path / "cv_base" / "resume.pdf"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "cv_out"
    out.mkdir()
    monkeypatch.setenv("BASE_CV_PATH", str(base))
    monkeypatch.setenv("CV_OUT", str(out))
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    yield {"base": base, "out": out}
    get_settings.cache_clear()


def test_existing_path_returned_as_is(cv):
    from jobhunter.tailor.render import resolve_cv
    assert resolve_cv(str(cv["base"])) == str(cv["base"])


def test_broken_path_falls_back_to_base(cv):
    """Ровно тот случай, что был в проде: windows-путь внутри контейнера."""
    from jobhunter.tailor.render import resolve_cv
    broken = "C:/Users/User/Desktop/jobhunter/cv_base/resume.pdf"
    assert resolve_cv(broken) == str(cv["base"])


def test_file_found_in_current_cv_out(cv):
    """Подогнанное резюме переехало вместе с каталогом — находим по имени."""
    from jobhunter.tailor.render import resolve_cv
    tailored = cv["out"] / "Hakobyan_Backend_ab12.pdf"
    tailored.write_bytes(b"%PDF-1.4 tailored")
    broken = "D:/old/place/Hakobyan_Backend_ab12.pdf"
    assert resolve_cv(broken) == str(tailored)


def test_no_cv_anywhere_returns_empty(tmp_path, monkeypatch):
    """Нет файла — пустая строка, а не путь в никуда."""
    monkeypatch.setenv("BASE_CV_PATH", str(tmp_path / "missing.pdf"))
    monkeypatch.setenv("CV_OUT", str(tmp_path))
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    try:
        from jobhunter.tailor.render import resolve_cv
        assert resolve_cv("C:/nope/none.pdf") == ""
        assert resolve_cv("") == ""
    finally:
        get_settings.cache_clear()


def test_senders_use_resolver(cv):
    """Отправители обязаны звать resolve_cv, а не проверять путь сами.

    Проверка статическая: прямой Path(cv_path).exists() перед отправкой —
    это возврат к тихой потере вложения.
    """
    from pathlib import Path as P

    root = P(__file__).resolve().parent.parent / "jobhunter"
    for rel in ("outreach/sender.py", "convo/send.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "resolve_cv(" in src, "%s должен звать resolve_cv" % rel
        assert "Path(cv_path).exists()" not in src, (
            "%s снова проверяет путь напрямую — вложение потеряется при "
            "смене корня проекта" % rel)
