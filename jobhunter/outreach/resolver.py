"""Резолв @handle → сущность Telegram, с кешем навсегда.

Массовый резолв незнакомых юзернеймов — классическая подпись скрапера и
рискованнее самих отправок. Поэтому: лениво, по одному, прямо перед
отправкой, и каждый результат кешируется в handle_cache.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import HandleCache, utcnow
from . import policy


class HandleDead(RuntimeError):
    """Юзернейм не существует / удалён — заявку в HANDLE_DEAD, не ретраить."""


class NotAUser(RuntimeError):
    """Это канал, группа или бот — автоотправку не делаем."""


@dataclass
class ResolvedPeer:
    handle: str
    user_id: int
    access_hash: str
    kind: str          # user | bot | channel | chat


async def resolve(client, sess, handle: str) -> ResolvedPeer:
    """Резолв с кешем. Бросает HandleDead / NotAUser."""
    from telethon import errors, types

    key = (handle or "").lower().lstrip("@")
    if not key:
        raise HandleDead("пустой хендл")

    cached = sess.get(HandleCache, key)
    if cached and cached.user_id and cached.last_error == "":
        return ResolvedPeer(key, int(cached.user_id), cached.access_hash, "user")
    if cached and cached.last_error in ("dead", "not_a_user"):
        raise (HandleDead if cached.last_error == "dead" else NotAUser)(key)

    row = cached or HandleCache(handle_norm=key)
    try:
        entity = await client.get_entity(key)
        policy.register_resolve(sess)
    except errors.FloodWaitError as e:
        # Резолв — тоже точка, где Telegram выдаёт FloodWait, и до сих пор
        # он отсюда улетал наружу, обрывая партию БЕЗ регистрации в
        # политике: счётчик floodwait и реакция «длинный флуд = сигнал»
        # просто не видели этих событий. Хендл не хороним — он живой,
        # это лимит на нас, а не проблема адресата.
        policy.on_flood_wait(sess, int(e.seconds))
        raise
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        row.last_error = "dead"
        row.resolve_failures = (row.resolve_failures or 0) + 1
        row.resolved_at = utcnow()
        sess.merge(row)
        raise HandleDead(key) from None

    if isinstance(entity, types.User):
        if entity.bot:
            row.last_error = "not_a_user"
            sess.merge(row)
            raise NotAUser("%s — бот" % key)
        kind = "user"
    else:
        row.last_error = "not_a_user"
        row.resolved_at = utcnow()
        sess.merge(row)
        raise NotAUser("%s — канал/группа" % key)

    row.user_id = str(entity.id)
    row.access_hash = str(getattr(entity, "access_hash", "") or "")
    row.resolved_at = utcnow()
    row.last_error = ""
    sess.merge(row)
    return ResolvedPeer(key, entity.id, row.access_hash, kind)
