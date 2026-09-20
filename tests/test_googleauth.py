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
    assert googleauth.check() == {"token": False, "calendar": False, "gmail_send": False,
                                  "gmail_read": False, "alive": False, "mailbox": ""}
