"""SMTP transport, TLS and cleanup regressions; no network or real credentials."""

import smtplib
import ssl
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jobhunter.outreach import mailer


@pytest.fixture(params=[587, 465])
def transport(request, monkeypatch):
    port = request.param
    settings = SimpleNamespace(
        smtp_host="smtp.example.com", smtp_port=port,
        smtp_user="owner@example.com", smtp_app_password="test-password",
    )
    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    server = Mock(spec=smtplib.SMTP)
    plain = Mock(return_value=server)
    implicit = Mock(return_value=server)
    monkeypatch.setattr(mailer.smtplib, "SMTP", plain)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", implicit)
    return SimpleNamespace(
        port=port, server=server, plain=plain, implicit=implicit,
        selected=implicit if port == 465 else plain,
        unused=plain if port == 465 else implicit,
    )


def test_secure_transport_and_order(transport):
    t = transport
    with mailer.smtp_session() as server:
        assert server is t.server
        t.server.quit.assert_not_called()
        assert [call[0] for call in server.mock_calls] == (
            ["login"] if t.port == 465 else ["starttls", "login"]
        )
    t.selected.assert_called_once()
    t.unused.assert_not_called()
    args, kwargs = t.selected.call_args
    assert args == ("smtp.example.com", t.port)
    assert kwargs["timeout"] == 30
    if t.port == 465:
        t.server.starttls.assert_not_called()
        context = kwargs["context"]
    else:
        assert "context" not in kwargs
        t.server.starttls.assert_called_once()
        context = t.server.starttls.call_args.kwargs["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    t.server.login.assert_called_once_with("owner@example.com", "test-password")
    t.server.quit.assert_called_once_with()
    t.server.close.assert_not_called()
    t.server.send_message.assert_not_called()


def test_connect_failure_never_falls_back(transport):
    t = transport
    t.selected.side_effect = TimeoutError("connect failed")
    with pytest.raises(TimeoutError, match="connect failed"), mailer.smtp_session():
        pytest.fail("Connection failure must not yield a session")
    t.unused.assert_not_called()
    t.server.login.assert_not_called()
    t.server.quit.assert_not_called()


def test_tls_failure_never_authenticates_or_falls_back(transport):
    t = transport
    failure = ssl.SSLCertVerificationError("untrusted certificate")
    if t.port == 465:
        t.implicit.side_effect = failure
    else:
        t.server.starttls.side_effect = failure
    with pytest.raises(ssl.SSLCertVerificationError), mailer.smtp_session():
        pytest.fail("TLS failure must not yield a session")
    t.server.login.assert_not_called()
    t.unused.assert_not_called()
    assert t.server.quit.call_count == int(t.port != 465)


def test_auth_failure_closes_connection(transport):
    t = transport
    failure = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    t.server.login.side_effect = failure
    with pytest.raises(smtplib.SMTPAuthenticationError) as exc, mailer.smtp_session():
        pytest.fail("Authentication failure must not yield a session")
    assert exc.value is failure
    t.server.quit.assert_called_once_with()
    t.unused.assert_not_called()


def test_body_failure_closes_connection_without_masking_error(transport):
    with pytest.raises(ValueError, match="caller failure"), mailer.smtp_session():
        raise ValueError("caller failure")
    transport.server.quit.assert_called_once_with()


def test_failed_quit_forces_socket_close(transport):
    transport.server.quit.side_effect = smtplib.SMTPServerDisconnected("gone")
    with mailer.smtp_session():
        pass
    transport.server.close.assert_called_once_with()


def test_cleanup_failure_preserves_authentication_error(transport):
    t = transport
    failure = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    t.server.login.side_effect = failure
    t.server.quit.side_effect = OSError("quit failed")
    t.server.close.side_effect = OSError("close failed")
    with pytest.raises(smtplib.SMTPAuthenticationError) as exc, mailer.smtp_session():
        pytest.fail("Authentication failure must not yield a session")
    assert exc.value is failure
    t.server.close.assert_called_once_with()
