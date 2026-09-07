# -*- coding: utf-8 -*-
"""SMTP-сессия умирает между письмами партии — Gmail закрывает её на паузах.

Инцидент 04–06.09: 20 писем получили SMTPServerDisconnected("please run
connect() first") — сокет был закрыт ДО письма, передачи не было, — но
были заморожены как «неоднозначная доставка» и не повторялись.
"""
import smtplib

from jobhunter.outreach import mailer


def test_dead_socket_is_definite_non_delivery():
    dead = smtplib.SMTPServerDisconnected("please run connect() first")
    mid = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
    assert mailer._socket_already_dead(dead)
    assert not mailer._socket_already_dead(mid)
    assert not mailer._smtp_delivery_ambiguous(dead), \
        "сокет умер до письма — доставки не было, это не «неоднозначно»"
    assert mailer._smtp_delivery_ambiguous(mid), \
        "обрыв посреди передачи остаётся неоднозначным"


def test_batch_reconnects_once_on_dead_socket(monkeypatch):
    """Первая попытка — мёртвый сокет, переподключение, повтор — ушло."""
    calls = {"connect": 0, "sent": []}

    class Dead:
        def send_message(self, msg):
            raise smtplib.SMTPServerDisconnected("please run connect() first")

        def quit(self):
            pass

    class Alive:
        def send_message(self, msg):
            calls["sent"].append(msg)

        def quit(self):
            pass

    def fake_connect():
        calls["connect"] += 1
        return Alive()

    monkeypatch.setattr(mailer, "_smtp_connect", fake_connect)
    # Тот же путь, что в send_batch: мёртвый сокет → connect → повтор.
    server = Dead()
    msg = object()
    try:
        server.send_message(msg)
    except smtplib.SMTPServerDisconnected as e:
        assert mailer._socket_already_dead(e)
        mailer._smtp_close(server)
        server = mailer._smtp_connect()
        server.send_message(msg)
    assert calls["connect"] == 1 and calls["sent"] == [msg]
