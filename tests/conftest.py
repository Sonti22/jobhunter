"""Страховочная сетка всего тестового набора.

Аудит нашёл три способа, которыми «зелёные» тесты врали:
  - тесты с забытым моком LLM молча ходили в настоящие облачные провайдеры
    (ключи подтягивались из боевого .env) — жгли квоту и флапали от сети;
  - один тест дёргал настоящий Google Calendar API;
  - четыре теста падали только ночью: within_reply_window() смотрел на
    реальные часы машины.

Правило: тест, которому нужны сеть, включённая LLM или конкретное время
суток, обязан попросить это ЯВНО (monkeypatch поверх этих фикстур).
Всё, что тест не попросил, — детерминированно и офлайн.
"""
import os
import socket

import pytest

# LLM выключена до того, как какой-либо модуль прочитает настройки: env
# перекрывает боевой .env, который pydantic читает напрямую с диска.
os.environ.setdefault("LLM_ENABLED", "false")


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    """Любой сокет наружу — падение с понятным текстом, а не тихий запрос.

    Локальные адреса разрешены: их используют in-process клиенты тестов.
    """
    real_connect = socket.socket.connect

    def guarded(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if isinstance(host, bytes):
            host = host.decode("utf-8", "replace")
        if host in ("127.0.0.1", "localhost", "::1") or host.startswith("/"):
            return real_connect(self, address, *a, **kw)
        raise RuntimeError(
            "тест полез в сеть: connect(%r). Замокай клиента явно." % (host,))

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def _daytime(monkeypatch):
    """Окно ответов «открыто» по умолчанию.

    Тесты про ночь мокают within_reply_window сами — их setattr выполняется
    позже этого и побеждает.
    """
    try:
        monkeypatch.setattr("jobhunter.convo.reply.within_reply_window",
                            lambda *a, **kw: True)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _no_gcal(monkeypatch):
    """Google Calendar в тестах недоступен, пока тест не скажет иное."""
    try:
        monkeypatch.setattr("jobhunter.schedule.gcal.available",
                            lambda: (False, "тесты"))
    except Exception:
        pass
