"""Клиент careered.io.

Ключевые факты (см. probe_out/GATE0_VERDICT.md):
  - авторизация: заголовок Authorization: Bearer <access_token>
  - mode:"full" → ссылки раскрыты; mode:"preview" → все "#", это VIP, пропускаем
  - list: GET /api/jobs?<query>&offset=N (шаг 20); detail: GET /api/jobs/{uuid}

Дисциплина:
  - троттлинг 1 req / 2 s, без параллели
  - канарейка авторизации: если detail отдаёт preview там, где ждём доступ, и
    /api/users/me не подтверждает токен — AuthLostError, ничего не пишем
  - TLS: verify=False + пиннинг SPKI (сертификат протухал)
"""
import re
import ssl
import time
from dataclasses import dataclass, field

import httpx

from ..config import get_settings
from ..models import ContactKind
from ..textutil import norm_keep_digits, sha256


class AuthLostError(RuntimeError):
    """Токен не подтверждён — прекращаем, чтобы не отравить БД анонимными данными."""


class TlsPinError(RuntimeError):
    """SPKI сертификата не совпал с закреплённым — возможен MITM."""


@dataclass
class JobRecord:
    uuid: str
    title: str
    company: str
    tag: str
    content: str
    mode: str
    posted_at: int
    links: list = field(default_factory=list)         # [{key,value}]
    contact_kind: str = ContactKind.UNKNOWN.value
    contact_handle: str = ""
    contact_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def has_telegram_user(self) -> bool:
        return self.contact_kind == ContactKind.USER_HANDLE.value and bool(self.contact_handle)


# t.me/<handle>, @handle, tg://resolve?domain=<handle>
_TME = re.compile(r"(?:https?://)?t(?:elegram)?\.me/(?P<h>[^/?#\s]+)", re.I)
_AT = re.compile(r"^@([A-Za-z0-9_]{4,})$")


def classify_contact(url: str) -> tuple:
    """(kind, handle, normalized_url). Автоотправка только для USER_HANDLE."""
    u = (url or "").strip()
    if not u or u == "#":
        return ContactKind.UNKNOWN.value, "", ""
    low = u.lower()
    if low.startswith("mailto:") or re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", u):
        return ContactKind.EMAIL.value, "", u
    m = _TME.search(u)
    if m:
        h = m.group("h")
        if h.startswith("+") or re.match(r"^\+?\d{6,}$", h):
            return ContactKind.PHONE_LINK.value, "", u            # НЕ резолвить
        if h.lower() in ("joinchat", "share") or h.startswith("+"):
            return ContactKind.GROUP_INVITE.value, "", u
        if h.lower().endswith("bot"):
            return ContactKind.BOT.value, h, u
        # публичный канал vs юзер по URL не отличить надёжно; трактуем как user,
        # но резолв на этапе отправки проверит тип сущности.
        return ContactKind.USER_HANDLE.value, h, u
    m = _AT.match(u)
    if m:
        return ContactKind.USER_HANDLE.value, m.group(1), "https://t.me/" + m.group(1)
    if low.startswith("http"):
        return ContactKind.EXTERNAL_URL.value, "", u
    return ContactKind.UNKNOWN.value, "", u


def _spki_of(host: str, port: int = 443) -> str:
    import base64
    import hashlib
    import socket

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(True)
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_der_x509_certificate(der)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


class CareeredClient:
    def __init__(self, throttle: float = 2.0):
        self.s = get_settings()
        self.throttle = throttle
        self._last = 0.0
        verify = not self.s.careered_insecure_tls
        if self.s.careered_insecure_tls and self.s.careered_spki_pin:
            got = _spki_of("careered.io")
            if got != self.s.careered_spki_pin:
                raise TlsPinError("SPKI не совпал: ждали %s, получили %s"
                                  % (self.s.careered_spki_pin, got))
        headers = {
            "User-Agent": self.s.careered_ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru,en;q=0.9",
            "Referer": self.s.base_url + "/",
        }
        if self.s.auth_header:
            headers["Authorization"] = self.s.auth_header
        # trust_env=False: игнорируем системную socks-прокси из окружения —
        # careered.io доступен напрямую (подтверждено TLS-пробой через сокет).
        self.http = httpx.Client(base_url=self.s.base_url, headers=headers,
                                 verify=verify, timeout=30.0, trust_env=False)
        self.auth_fingerprint = sha256(self.s.auth_header)[:16]

    # ── низкий уровень ──
    def _wait(self):
        dt = time.monotonic() - self._last
        if dt < self.throttle:
            time.sleep(self.throttle - dt)
        self._last = time.monotonic()

    def _get(self, path: str, attempts: int = 3) -> httpx.Response:
        last = None
        for i in range(attempts):
            self._wait()
            try:
                return self.http.get(path)
            except (httpx.RemoteProtocolError, httpx.ConnectError,
                    httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                last = exc
                time.sleep(2.0 * (i + 1))          # 2s, 4s, 6s бэкофф
        raise last

    def verify_auth(self) -> dict:
        """Подтверждает токен через /api/users/me. Иначе AuthLostError."""
        r = self._get("/api/users/me")
        try:
            data = r.json()
        except Exception:
            data = None
        if r.status_code != 200 or not isinstance(data, dict) or "mail" not in data:
            raise AuthLostError("токен не подтверждён (/api/users/me → %s %s). "
                                "Обнови CAREERED_ACCESS_TOKEN в .env."
                                % (r.status_code, str(data)[:120]))
        return data

    # ── список ──
    def list_page(self, offset: int) -> dict:
        r = self._get("/api/jobs?%s&offset=%d" % (self.s.careered_query, offset))
        return r.json()

    def iter_ids(self, max_jobs: int | None = None):
        """Все uuid ленты по страницам, пока не кончатся."""
        offset, seen = 0, 0
        while True:
            page = self.list_page(offset)
            entries = page.get("entries") or []
            if not entries:
                break
            for e in entries:
                uid = e.get("id") or e.get("uuid")
                if uid:
                    yield uid, e
                    seen += 1
                    if max_jobs and seen >= max_jobs:
                        return
            total = page.get("total")
            offset += len(entries)
            if total is not None and offset >= total:
                break

    # ── деталь ──
    def detail(self, uuid: str) -> JobRecord:
        r = self._get("/api/jobs/%s" % uuid)
        d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        links = d.get("links") or []
        title, company = _title_company(d.get("content") or "")
        rec = JobRecord(
            uuid=d.get("id") or uuid,
            title=title,
            company=company,
            tag=(d.get("tag") or {}).get("name", "") if isinstance(d.get("tag"), dict) else "",
            content=d.get("content") or "",
            mode=d.get("mode") or "",
            posted_at=d.get("posted_at") or 0,
            links=[{"key": x.get("key"), "value": x.get("value")} for x in links],
            raw=d,
        )
        # контакт — предпочитаем telegram-юзера
        tg = next((x for x in links if x.get("key") == "telegram"
                   and x.get("value") and x.get("value") != "#"), None)
        chosen = tg["value"] if tg else next(
            (x.get("value") for x in links if x.get("value") and x.get("value") != "#"), "")
        kind, handle, url = classify_contact(chosen)
        rec.contact_kind, rec.contact_handle, rec.contact_url = kind, handle, url
        return rec


# Заголовок и компанию вытаскиваем из markdown-контента careered.
_RE_TITLE = re.compile(r"(?:вакансия|job title|позиция|роль|position)\s*[:：]\s*(.+)", re.I)
_RE_COMPANY = re.compile(r"(?:компания|company|employer)\s*[:：]\s*(.+)", re.I)


_JUNK_HEAD = re.compile(r"^(job description|apply now|start apply|please wait|"
                        r"вакансия|job title|о компании|about)\b", re.I)


def _title_company(content: str) -> tuple:
    title = company = ""
    fallback = ""
    for line in (content or "").splitlines():
        s = line.strip().strip("*").strip()
        if not s:
            continue
        if not title:
            m = _RE_TITLE.search(s)
            if m:
                title = m.group(1).strip().strip("*").strip()
        if not company:
            m = _RE_COMPANY.search(s)
            if m:
                company = m.group(1).strip().strip("*").strip()
        # фолбэк: первая осмысленная строка (не служебный заголовок, не буллет)
        if not fallback and 6 <= len(s) <= 120 and not _JUNK_HEAD.match(s) \
                and not s.startswith(("•", "-", "—", "·", "❤", "✨", "🔥")) \
                and not s.replace("*", "").strip().isdigit():
            fallback = s
        if title and company:
            break
    return (title or fallback)[:200], company[:120]


def title_norm(title: str) -> str:
    return norm_keep_digits(title)
