"""Резюме должно доезжать по ВСЕМ четырём путям отправки.

Путей четыре, и каждый прикладывал файл своим кодом: холодный отклик в
Telegram, холодный по почте, ответ в переписке в Telegram, ответ по почте.
Достаточно одному из них проверять путь напрямую — и по этому каналу письмо
уходит без вложения, ничем не сообщая об этом. Так уже случилось с почтовыми
откликами: резолвер добавили в три места из четырёх.

Тест проверяет не «функция вызывается», а факт: во вложении лежит настоящий
файл нужного размера.
"""
import asyncio
import uuid

import pytest

PDF = b"%PDF-1.4\n" + b"x" * 4000 + b"\n%%EOF"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Резюме на диске + база в темпе. Пути заявок намеренно битые."""
    cv = tmp_path / "cv_base" / "Акопян_Сурен_Python.pdf"
    cv.parent.mkdir(parents=True)
    cv.write_bytes(PDF)
    (tmp_path / "cv_out").mkdir()

    monkeypatch.setenv("DB_PATH", str(tmp_path / "cv.db"))
    monkeypatch.setenv("BASE_CV_PATH", str(cv))
    monkeypatch.setenv("CV_OUT", str(tmp_path / "cv_out"))
    monkeypatch.setenv("SMTP_USER", "suren6pro@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "x")
    monkeypatch.setenv("SEND_CV_WITH_FIRST_MESSAGE", "true")
    monkeypatch.setenv("LLM_ENABLED", "false")

    from jobhunter.config import get_settings
    get_settings.cache_clear()
    import jobhunter.db as dbmod
    dbmod._engine = None
    dbmod._Session = None
    yield {"cv": cv, "db": dbmod}
    get_settings.cache_clear()
    dbmod._engine = None
    dbmod._Session = None


def _app(dbmod, kind, *, handle="", url="", status=None, cv_path=None):
    from jobhunter.models import Application, Job, Status
    with dbmod.session_scope() as sess:
        job = Job(external_uuid=str(uuid.uuid4()), source="test",
                  title="Python Backend", company_name="Acme",
                  contact_kind=kind, contact_handle=handle, contact_url=url)
        sess.add(job)
        sess.flush()
        app = Application(
            job_id=job.id, score=80, gate_passed=True,
            message_body="Здравствуйте, отклик.",
            status=(status or Status.APPROVED.value),
            # Путь, какой остался в базе после переезда в контейнер. Диск Z:
            # взят намеренно: путь внутри проекта на машине разработчика
            # существует, и тест молча проверял бы не то.
            cv_path=cv_path if cv_path is not None
            else r"Z:\old\jobhunter\cv_base\Акопян_Сурен_Python.pdf")
        sess.add(app)
        sess.flush()
        return app.id


# ── холодная почта ─────────────────────────────────────────────────────

def test_cold_email_attaches_cv(env):
    """Именно здесь и была дыра: почта проверяла путь напрямую."""
    from jobhunter.outreach.mailer import build_message

    msg = build_message(to="hr@acme.ru", subject="Отклик", body="текст",
                        cv_path=r"C:\старый\путь\Акопян_Сурен_Python.pdf",
                        app_id=1)
    parts = [p for p in msg.iter_attachments()]
    assert parts, "письмо ушло БЕЗ резюме"
    assert parts[0].get_content_type() == "application/pdf"
    assert parts[0].get_payload(decode=True) == PDF
    assert parts[0].get_filename().endswith(".pdf")


def test_cold_email_without_cv_path_has_no_attachment(env):
    """Пустой путь — не повод подсовывать базовое резюме молча."""
    from jobhunter.outreach.mailer import build_message

    msg = build_message(to="hr@acme.ru", subject="Отклик", body="текст",
                        cv_path="", app_id=1)
    assert list(msg.iter_attachments()) == []


# ── ответ по почте ─────────────────────────────────────────────────────

def test_email_reply_attaches_cv(env, monkeypatch):
    """«Пришлите резюме» в почтовом треде — файл должен приложиться."""
    from jobhunter.convo.send import send_reply
    from jobhunter.models import ContactKind, Status
    from jobhunter.outreach import mailer

    sent = []

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(mailer, "smtp_session", lambda: _S())
    app_id = _app(env["db"], ContactKind.EMAIL.value, url="hr@acme.ru",
                  status=Status.AWAITING_REPLY.value)

    res = asyncio.run(send_reply(None, app_id, "Прикладываю резюме",
                                 attach_cv=True, is_auto=False))
    assert res == "ok", res
    parts = list(sent[-1].iter_attachments())
    assert parts and parts[0].get_payload(decode=True) == PDF


# ── Telegram ───────────────────────────────────────────────────────────

class FakeTelethon:
    """Клиент, запоминающий, какой файл ему велели отправить."""

    def __init__(self):
        self.files = []
        self.texts = []

    def action(self, *a, **k):
        class _A:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *e):
                return False
        return _A()

    async def send_file(self, peer, path, caption="", force_document=True):
        self.files.append(path)
        return type("M", (), {"id": 1})()

    async def send_message(self, peer, text):
        self.texts.append(text)
        return type("M", (), {"id": 2})()


def test_telegram_reply_attaches_cv(env, monkeypatch):
    from jobhunter.convo import send as send_mod
    from jobhunter.models import ContactKind, Status

    app_id = _app(env["db"], ContactKind.USER_HANDLE.value, handle="hr_acme",
                  status=Status.AWAITING_REPLY.value)

    class _Peer:
        user_id = 111

    async def _resolve(client, sess, handle):
        return _Peer()

    monkeypatch.setattr(send_mod, "resolve", _resolve)
    monkeypatch.setattr(send_mod.policy, "typing_seconds", lambda *a, **k: 0)

    async def _noop_folder(*a, **k):
        return "skip"

    from jobhunter.outreach import folder
    monkeypatch.setattr(folder, "add_to_folder", _noop_folder)

    client = FakeTelethon()
    res = asyncio.run(send_mod.send_reply(client, app_id, "Резюме во вложении",
                                          attach_cv=True, is_auto=False))
    assert res == "ok", res
    assert client.files, "резюме не отправлено, ушёл только текст"
    from pathlib import Path
    assert Path(client.files[0]).read_bytes() == PDF


def test_telegram_cold_send_stops_without_cv(env, monkeypatch):
    """Нет резюме вообще — отклик не уходит, владелец получает сигнал.

    Пустое вложение хуже пропущенной отправки: рекрутёр читает «прикладываю
    резюме» и не находит его, а второго письма по этой вакансии не будет.
    """
    from jobhunter import notify
    from jobhunter.config import get_settings
    from jobhunter.models import ContactKind

    monkeypatch.setenv("BASE_CV_PATH", str(env["cv"].parent / "нет.pdf"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("BOT_ALLOWED_USER_IDS", "1")
    get_settings.cache_clear()

    from jobhunter.outreach import sender

    app_id = _app(env["db"], ContactKind.USER_HANDLE.value, handle="hr_acme",
                  cv_path="C:/нет/такого.pdf")

    class _Peer:
        user_id = 111

    async def _resolve(client, sess, handle):
        return _Peer()

    monkeypatch.setattr(sender, "resolve", _resolve)
    monkeypatch.setattr(sender.policy, "typing_seconds", lambda *a, **k: 0)

    client = FakeTelethon()
    item = {"app_id": app_id, "handle": "hr_acme", "title": "Python",
            "company": "Acme", "cv_path": "C:/нет/такого.pdf", "score": 80,
            "employer_id": None, "job_id": 1, "text": "текст", "tag": ""}
    import random
    res = asyncio.run(sender.send_one(client, item, random.Random(1), dry=False))

    assert res == "skipped:нет резюме", res
    assert client.files == [] and client.texts == []
    assert any(r["kind"] == "error" for r in notify.pending(20)), (
        "владелец должен узнать, что отправка встала из-за резюме")
