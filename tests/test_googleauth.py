"""Общий вход в Google: календарь и почта на одном токене."""
import json

import pytest


@pytest.fixture()
def token(tmp_path, monkeypatch):
    path = tmp_path / "google_token.json"
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(path))
    from jobhunter.config import get_settings
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def _write(path, scopes):
    path.write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes,
                                "expiry": "2020-01-01T00:00:00Z"}), encoding="utf-8")


def test_old_calendar_only_token_cannot_send_mail(token):
    """20.09: у владельца токен только с календарём — почте нужен повторный вход, а не сбой в полёте."""
    from jobhunter import googleauth
    _write(token, [googleauth.CALENDAR])
    assert googleauth.granted() == {googleauth.CALENDAR}
    with pytest.raises(googleauth.GoogleUnavailable) as err:
        googleauth.credentials(need=(googleauth.GMAIL_SEND,))
    assert "gmail.send" in str(err.value) and "повторный вход" in str(err.value)


def test_revoked_token_says_login_again_instead_of_raw_google_error(token, monkeypatch):
    """Токен от 26.08 умер с invalid_grant (проект в статусе «Тестирование» — 7 дней жизни)."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    from jobhunter import googleauth
    _write(token, googleauth.SCOPES)

    def dead(self, request):
        raise RefreshError("invalid_grant: Bad Request")
    monkeypatch.setattr(Credentials, "refresh", dead)
    with pytest.raises(googleauth.GoogleUnavailable) as err:
        googleauth.credentials(need=(googleauth.GMAIL_SEND,))
    assert "--login" in str(err.value)
    state = googleauth.check()
    assert state["token"] and state["gmail_send"] and not state["alive"]


def test_no_token_at_all(token):
    from jobhunter import googleauth
    assert googleauth.granted() == set()
    state = googleauth.check()
    assert state["token"] is False and state["alive"] is False and state["mailbox"] == ""
    assert not state["calendar"] and not state["gmail_send"] and not state["gmail_read"]


def test_google_login_ignores_the_system_proxy():
    """20.09: в Windows остался системный прокси VPN (socks=127.0.0.1:10808), и обмен кода на
    токен падал с SOCKSHTTPSConnectionPool, хотя Google доступен напрямую."""
    from jobhunter import googleauth
    assert googleauth._direct_request().session.trust_env is False


def test_unwritable_token_file_does_not_disable_the_api(token, monkeypatch):
    """21.09: файл токена остался у root после docker cp, бот работает под app. Обновлённый токен не
    записывался, исключение всплывало, и почта молча переходила на SMTP/IMAP (8 писем ушли так)."""
    from pathlib import Path

    from google.oauth2.credentials import Credentials

    from jobhunter import googleauth
    _write(token, googleauth.SCOPES)

    def refresh(self, request):
        self.token = "fresh-access-token"
        import datetime
        self.expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
    monkeypatch.setattr(Credentials, "refresh", refresh)

    real_write = Path.write_text

    def deny(self, *a, **kw):
        if self.name == "google_token.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real_write(self, *a, **kw)
    monkeypatch.setattr(Path, "write_text", deny)
    creds = googleauth.credentials(need=(googleauth.GMAIL_SEND,))
    assert creds.token == "fresh-access-token"                        # токен получен и годен в памяти


def test_step_google_token_warns_when_the_token_cannot_be_saved(token, monkeypatch):
    from jobhunter import autopilot, googleauth
    sent = []
    monkeypatch.setattr("jobhunter.notify.push_once", lambda kind, text, **kw: sent.append(kind) or True)
    monkeypatch.setattr(googleauth, "check", lambda: {
        "token": True, "alive": True, "writable": False, "calendar": True, "gmail_send": True,
        "gmail_read": True, "mailbox": "s***@gmail.com"})
    result = autopilot.step_google_token()
    assert sent == ["google_token_readonly"] and result["writable"] is False
