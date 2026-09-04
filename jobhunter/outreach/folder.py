"""Папка Telegram со всеми диалогами, куда писал бот.

Зачем. «Избранное» — канал управления: туда приходят карточки на решение.
Смешивать с ним переписку по 30 вакансиям в день нельзя, иначе команды
владельца утонут в потоке. Поэтому каждый диалог, куда бот написал,
кладётся в отдельную папку-вкладку рядом с «Все чаты».

Как это устроено в Telegram. Папка (chat folder) в API называется
DialogFilter: список include_peers, свой id и заголовок. Папок у обычного
аккаунта до 10, у Premium — до 20; чатов в папке до 100 (Premium — 200).
Мы держим ОДНУ папку и дописываем в неё пиров, не трогая остальные:
UpdateDialogFilterRequest перезаписывает фильтр целиком, поэтому сначала
читаем текущий состав, добавляем новое и отправляем полный список обратно.

Telethon 1.36+ ждёт title типом TextWithEntities, более старые — строкой;
_title_value() отдаёт то, что понимает установленная версия.

Переполнение папки — не ошибка: при достижении лимита старые пиры
вытесняются новыми (FIFO), потому что свежая переписка нужнее архива.
Полная история всё равно лежит в БД и в logs/sent/.
"""
from __future__ import annotations

from ..config import get_settings

# Пределы Telegram на 2026-08. Держим с запасом: сервер отвергает фильтр
# целиком, если чатов больше лимита, и тогда папка не обновится вовсе.
MAX_PEERS_FREE = 96
MAX_PEERS_PREMIUM = 196
FOLDER_EMOTICON = "\U0001F4BC"        # 💼 — иконка вкладки


class FolderUnavailable(RuntimeError):
    """Папку создать не вышло: лимит папок, старый слой API или запрет."""


def _title_value(text: str):
    """title для DialogFilter: TextWithEntities в новых Telethon, str в старых.

    Определяем по аннотации конструктора, а не по наличию TextWithEntities в
    модуле: этот тип появился раньше (для factCheck) и существует даже там,
    где DialogFilter.title всё ещё обычная строка. Ошибка вылезла бы только
    при сериализации запроса — «'str' object has no attribute '_bytes'».
    """
    import inspect

    from telethon.tl.types import DialogFilter, TextWithEntities

    ann = inspect.signature(DialogFilter.__init__).parameters["title"].annotation
    if "TextWithEntities" in str(ann):
        return TextWithEntities(text=text, entities=[])
    return text


def _title_text(flt) -> str:
    """Обратная операция: достать строку из title любого вида."""
    t = getattr(flt, "title", "")
    return getattr(t, "text", t) or ""


def _peer_key(peer) -> tuple:
    """Ключ для сравнения пиров: (тип, id). InputPeer* не сравниваются напрямую."""
    for attr in ("user_id", "channel_id", "chat_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            return (attr, int(val))
    return ("raw", repr(peer))


async def _find_filter(client, title: str):
    """Наш фильтр по заголовку и свободный id. (filter|None, free_id, count)."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    from telethon.tl.types import DialogFilter

    res = await client(GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)          # в новых слоях это объект
    ours, used = None, set()
    for flt in filters:
        fid = getattr(flt, "id", None)
        if fid is not None:
            used.add(int(fid))
        if isinstance(flt, DialogFilter) and _title_text(flt) == title:
            ours = flt
    # id папок начинаются с 2: 0 и 1 заняты служебными «Все чаты»/«Архив»
    free_id = next(i for i in range(2, 256) if i not in used)
    return ours, free_id, len(used)


async def add_to_folder(client, peer, title: str = "") -> str:
    """Кладёт диалог в папку бота. Возвращает короткий статус для лога.

    Никогда не бросает наружу: провал раскладки по папкам не должен
    ронять отправку отклика — это удобство, а не часть доставки.
    """
    s = get_settings()
    if not s.tg_folder_enabled:
        return "skip:выключено"
    title = title or s.tg_folder_name

    try:
        from telethon.tl.functions.messages import UpdateDialogFilterRequest
        from telethon.tl.types import DialogFilter

        input_peer = await client.get_input_entity(peer)
        ours, free_id, total = await _find_filter(client, title)

        if ours is None:
            if total >= 10:
                return "skip:лимит папок Telegram (10)"
            flt = DialogFilter(id=free_id, title=_title_value(title),
                               pinned_peers=[], include_peers=[input_peer],
                               exclude_peers=[], emoticon=FOLDER_EMOTICON)
            await client(UpdateDialogFilterRequest(id=free_id, filter=flt))
            return "создана папка «%s»" % title

        existing = list(ours.include_peers or [])
        keys = {_peer_key(p) for p in existing}
        keys |= {_peer_key(p) for p in (ours.pinned_peers or [])}
        if _peer_key(input_peer) in keys:
            return "уже в папке"

        me = await client.get_me()
        cap = MAX_PEERS_PREMIUM if getattr(me, "premium", False) else MAX_PEERS_FREE
        existing.append(input_peer)
        dropped = 0
        # Оставляем минимум один пир: фильтр без include_peers и без флагов
        # сервер отвергает целиком (FILTER_INCLUDE_EMPTY), и папка не обновится.
        while (len(existing) + len(ours.pinned_peers or []) > cap
               and len(existing) > 1):
            existing.pop(0)                        # вытесняем самые старые
            dropped += 1

        ours.include_peers = existing
        await client(UpdateDialogFilterRequest(id=ours.id, filter=ours))
        return "в папку%s" % (" (вытеснено %d)" % dropped if dropped else "")
    except Exception as e:
        return "skip:%s" % type(e).__name__


async def folder_stats(client, title: str = "") -> dict:
    """Сколько диалогов в папке — для /status и диагностики."""
    title = title or get_settings().tg_folder_name
    try:
        ours, _, total = await _find_filter(client, title)
        if ours is None:
            return {"exists": False, "peers": 0, "folders": total}
        return {"exists": True,
                "peers": len(ours.include_peers or []) + len(ours.pinned_peers or []),
                "folders": total}
    except Exception as e:
        return {"exists": False, "peers": 0, "error": type(e).__name__}
