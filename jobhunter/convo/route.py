"""Каким каналом разговаривать с работодателем по конкретной заявке.

Отдельный модуль, а не пара строк на месте: тот же вопрос решается ещё в
трёх местах — при отправке ответа, при чтении входящих и при подписи
карточки владельцу. Разъехавшиеся копии условия «если это почта» дают самый
неприятный класс ошибок: ответ уходит не туда, куда пришёл вопрос.
"""
from __future__ import annotations

from ..models import Application, ContactKind, Job

TELEGRAM = "telegram"
EMAIL = "email"


def peer_email(app: Application, job: Job) -> str:
    """Куда писать по почте.

    Приоритет у адреса, с которого нам ответили: рекрутёры сплошь и рядом
    отвечают с личного ящика вместо hr@, и продолжать разговор надо там же,
    иначе получается две параллельные ветки, в одной из которых человек
    молчит, потому что не читает общий ящик.
    """
    if getattr(app, "email_peer", ""):
        return app.email_peer.strip().lower()
    raw = (job.contact_url or "") if job else ""
    return raw.replace("mailto:", "").strip().lower()


def channel_for(app: Application, job: Job) -> tuple:
    """('telegram', handle) | ('email', адрес) | ('', '') — канала нет."""
    if job is None:
        return "", ""
    handle = (job.contact_handle or "").lstrip("@").strip()
    # Почта проверяется первой: если у вакансии есть и хендл, и адрес, отклик
    # ушёл на адрес, и переписка живёт там.
    if job.contact_kind == ContactKind.EMAIL.value:
        addr = peer_email(app, job)
        return (EMAIL, addr) if "@" in addr else ("", "")
    if handle:
        return TELEGRAM, handle
    # Страховка: вид контакта потерян или записан неточно, но адрес есть.
    addr = peer_email(app, job)
    return (EMAIL, addr) if "@" in addr else ("", "")


def peer_label(app: Application, job: Job) -> str:
    """Как назвать собеседника в карточке владельцу и в логе."""
    channel, peer = channel_for(app, job)
    if channel == TELEGRAM:
        return "@" + peer
    return peer or "?"
