"""Отправка откликов в Telegram с личного аккаунта (MTProto/Telethon).

Дисциплина:
  - ОДИН процесс на сессию (PID-лок): два клиента на одном .session ломают его
  - kill-switch проверяется перед КАЖДОЙ отправкой
  - random_id фиксируется в БД: при ретрае Telegram сам дедуплицирует
  - имитация набора перед отправкой
  - при PeerFlood — немедленный стоп партии (см. policy)

Запуск:
    python -m jobhunter.outreach.sender --dry-run          # без сети
    python -m jobhunter.outreach.sender --login            # только авторизация
    python -m jobhunter.outreach.sender --limit 5          # боевая партия
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..config import ROOT, get_settings
from ..db import session_scope
from ..models import Application, ContactKind, Employer, Job, Message, SendLog, Status, utcnow
from ..tailor.render import resolve_cv
from . import eligibility, policy
from .resolver import HandleDead, NotAUser, resolve

# Привязан к корню проекта, а не к текущему каталогу: лок, зависящий от того,
# откуда запустили процесс, — это лок, которого нет.
LOCK_FILE = str(ROOT / "sender.pid")


def lock_path() -> Path:
    """Лок рядом с БД, чтобы host/container делили один guard."""
    return Path(get_settings().db_path).with_name("sender.pid")


# ────────────────────────────────────────────────────── PID-лок ──

class ProcessLock:
    """Один отправляющий процесс на машину. Иначе .session повреждается."""

    def __init__(self, path: Path):
        self.path = path
        self._pid = os.getpid()
        self._identity = _process_identity(self._pid)
        self._start_marker = _process_start_marker(self._pid)

    def _write(self) -> None:
        with self.path.open("x", encoding="utf-8") as f:
            f.write("%d\n%s\n%s" % (self._pid, self._identity,
                                     self._start_marker or ""))

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._write()
        except FileExistsError:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
                pid = int(lines[0].strip())
                owner = lines[1].strip() if len(lines) > 1 else ""
                marker = lines[2].strip() if len(lines) > 2 else ""
            except (OSError, ValueError):
                pid, owner, marker = 0, "", ""
            if pid and _lock_owner_alive(self.path, pid, owner, marker):
                raise RuntimeError("уже запущен процесс отправки (pid %d)" % pid) from None
            try:
                self.path.unlink()
            except OSError:
                raise RuntimeError("не удалось заменить устаревший лок отправки") from None
            self._write()
        return self

    def __exit__(self, *exc):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            same_owner = (lines and lines[0].strip() == str(self._pid)
                          and (len(lines) < 2 or lines[1].strip() == self._identity)
                          and (len(lines) < 3 or lines[2].strip() == (self._start_marker or "")))
            if same_owner:
                self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return True
    return True


def _process_identity(pid: int) -> str:
    """Командная строка процесса, если ОС даёт её прочитать."""
    try:
        raw = Path("/proc/%d/cmdline" % pid).read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""


def _process_start_marker(pid: int) -> str:
    """Маркер запуска из procfs; отличает новый контейнер с тем же PID 1."""
    try:
        stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
        tail = stat.rsplit(") ", 1)[1].split()
        # После закрывающей скобки начинается поле 3; starttime — поле 22,
        # то есть индекс 19 в этом хвосте.
        return tail[19]
    except (OSError, IndexError, ValueError):
        return ""


def _lock_owner_alive(path: Path, pid: int, owner: str, marker: str) -> bool:
    """Проверить lock с защитой от повторного использования PID контейнера."""
    if not _pid_alive(pid):
        return False
    actual_owner = _process_identity(pid)
    actual_marker = _process_start_marker(pid)
    if marker and actual_marker:
        return marker == actual_marker
    if owner and actual_owner:
        return owner == actual_owner
    # Старый однострочный lock не знает момент запуска. В Linux procfs можно
    # сравнить его mtime с моментом старта процесса; на Windows остаётся
    # консервативная проверка PID.
    if not owner and not marker:
        try:
            stat = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8")
            tail = stat.rsplit(") ", 1)[1].split()
            ticks = int(tail[19])
            hz = os.sysconf("SC_CLK_TCK")
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            started = time.time() - uptime + ticks / hz
            if path.stat().st_mtime < started - 2:
                return False
        except (OSError, IndexError, ValueError, AttributeError):
            pass
    return True


def _telethon_input_peer(peer):
    """Преобразовать внутренний ResolvedPeer в объект Telethon."""
    from telethon import types

    if not peer.access_hash:
        raise ValueError("у @%s нет access_hash для отправки" % peer.handle)
    return types.InputPeerUser(user_id=peer.user_id,
                               access_hash=int(peer.access_hash))


# ────────────────────────────────────────────── выбор кандидатов ──

def pick_batch(limit: int) -> list:
    """Заявки, готовые к отправке в Telegram, по убыванию скора.

    Правила отбора:
      - только APPROVED (аппрув пользователя уже был)
      - только живой user_handle
      - не более одного холодного контакта на работодателя за 30 дней
      - если работодатель уже написал сам — не шлём холодное
    """
    out = []
    seen_employers: set[str] = set()
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.APPROVED.value)
            .order_by(Application.score.desc())).all()
        try:
            from ..report import source_preferences, source_priority_penalty
            source_rates = source_preferences(min_sent=5)
        except Exception:
            source_rates = {}

            def source_priority_penalty(source: str,
                                        preferences: dict | None = None
                                        ) -> float:
                return 0.0
        pairs = [(app, sess.get(Job, app.job_id)) for app in rows]
        pairs.sort(key=lambda pair: (
            (pair[0].score - source_priority_penalty(
                pair[1].source if pair[1] else "", source_rates)),
            pair[0].id), reverse=True)
        for app, job in pairs:
            if not job or job.is_closed or job.contact_kind != ContactKind.USER_HANDLE.value:
                continue
            if not job.contact_handle:
                continue
            emp = sess.get(Employer, app.employer_id) if app.employer_id else None
            if not eligibility.check(app, job, emp).allowed:
                continue
            # Дедуп внутри партии: last_contacted_at обновится только после
            # отправки, а партия собирается заранее — без этого набора два
            # письма одному работодателю уходили одной пачкой.
            if emp:
                if emp.handle_norm in seen_employers:
                    continue
                seen_employers.add(emp.handle_norm)
            out.append({
                "app_id": app.id, "job_id": job.id,
                "employer_id": app.employer_id,
                "handle": job.contact_handle,
                "title": job.title or job.tag,
                "company": job.company_name,
                "tag": job.tag,
                "text": app.message_body,
                "cv_path": app.cv_path,
                "score": app.score,
            })
            if len(out) >= limit:
                break
    return out


# ─────────────────────────────────────────────────────── отправка ──

async def send_one(client, item: dict, rng: random.Random, dry: bool) -> str:
    """Возвращает 'ok' | 'skipped:<why>' | 'stop:<why>'."""
    s = get_settings()
    if not dry:
        from telethon import errors

    if policy.kill_switch_active():
        return "stop:kill-switch"

    with session_scope() as sess:
        v = policy.can_send_cold(sess)
        if not v.allowed:
            return "stop:%s" % v.reason

    # резолв — лениво, прямо перед отправкой
    with session_scope() as sess:
        app = sess.get(Application, item["app_id"])
        job = sess.get(Job, app.job_id) if app else None
        emp = sess.get(Employer, app.employer_id) if app and app.employer_id else None
        verdict = eligibility.check(app, job, emp)
        if not verdict.allowed:
            return "skipped:%s" % verdict.reason
        assert app is not None and job is not None
        if job.contact_handle != item["handle"]:
            return "skipped:контакт изменился после выбора партии"
        if dry:
            print("      [dry-run] → @%s" % item["handle"])
            return "ok"
        try:
            peer = await resolve(client, sess, item["handle"])
        except HandleDead as e:
            app.transition(Status.SENDING) if app.status == Status.APPROVED.value else None
            app.transition(Status.HANDLE_DEAD, reason=str(e))
            return "skipped:handle_dead"
        except NotAUser as e:
            app.transition(Status.SENDING) if app.status == Status.APPROVED.value else None
            app.transition(Status.HANDLE_DEAD, reason=str(e))
            return "skipped:not_a_user"
        except errors.FloodWaitError as e:
            # Учтён политикой внутри resolve; заявка остаётся APPROVED и
            # уйдёт следующим прогоном. Долгий флуд — стоп всей партии.
            if e.seconds > policy.FLOOD_SLEEP_MAX:
                return "stop:floodwait_%ds_на_резолве" % e.seconds
            await asyncio.sleep(e.seconds + 1)
            return "skipped:floodwait_resolve"

        # лизинг: помечаем «в отправке», чтобы второй процесс не взял
        app.transition(Status.SENDING)
        app.worker_pid = os.getpid()
        app.sending_lease_until = (datetime.now(timezone.utc).replace(tzinfo=None)
                                   + __import__("datetime").timedelta(seconds=180))
        is_followup = eligibility.is_followup(app)
        key_field = "telegram_followup_random_id" if is_followup else "telegram_random_id"
        if not getattr(app, key_field):
            setattr(app, key_field, rng.getrandbits(62))
        random_id = getattr(app, key_field)
        if not app.telegram_file_random_id:
            app.telegram_file_random_id = random_id + 1
        file_random_id = app.telegram_file_random_id
        app.send_channel = "telegram"
        app.send_idempotency_key = "telegram:%d:%d" % (app.id, random_id)
        app.send_last_attempt_at = utcnow()
        app.send_next_try_at = None
        app.send_error_detail = ""
        app.send_attempts += 1
        # Напоминание, если оно подготовлено и ещё не ушло, иначе
        # исходное письмо. Исходник при этом сохраняется целиком —
        # он нужен владельцу при разборе и аналитике шаблонов.
        text = app.followup_body if is_followup else app.message_body
        cv_path = app.cv_path

    # Для dry-run peer намеренно None; до сетевой ветки мы уже вышли.
    assert peer is not None

    # имитация набора
    try:
        async with client.action(peer.user_id, "typing"):
            await asyncio.sleep(policy.typing_seconds(text, rng))
    except Exception:
        pass

    # Файл — только с первым сообщением: follow-up повторял резюме тому, у
    # кого оно уже есть, а второй документ подряд — лишний антиспам-риск.
    cv_real = (resolve_cv(cv_path)
               if s.send_cv_with_first_message and not is_followup else "")
    if s.send_cv_with_first_message and not is_followup and not cv_real:
        # Резюме должно уходить с каждым откликом. Если файла нет — это
        # поломка настройки или тома, а не повод отправить письмо без
        # главного вложения: рекрутёр получит текст «прикладываю резюме»
        # без резюме, и второго шанса не будет.
        from .. import notify
        notify.push("error",
                    "⚠️ Резюме не найдено — отправка отклика остановлена.\n"
                    "Путь в заявке: %s\nBASE_CV_PATH: %s"
                    % ((cv_path or "пусто")[:120], s.base_cv_path or "не задан"),
                    dedup="cv_missing")
        with session_scope() as sess:
            sess.get(Application, item["app_id"]).transition(Status.APPROVED)
        return "skipped:нет резюме"

    try:
        # Сначала текст, затем файл отдельным сообщением (решение владельца,
        # 04.09): письмо не режется лимитом подписи в 1024 символа, а пара
        # «сообщение → документ» читается по-человечески. Файл уходит ниже,
        # отдельным try: его сбой не отменяет уже состоявшийся отклик.
        if getattr(client, "_jobhunter_use_raw_requests", False):
            from telethon import functions
            # resolver возвращает внутренний ResolvedPeer, а не TL-объект.
            # Передавать его в get_input_entity нельзя: Telethon пытается
            # cast-нуть dataclass и получает «Cannot cast ResolvedPeer».
            input_peer = _telethon_input_peer(peer)
            sent = await client(functions.messages.SendMessageRequest(
                peer=input_peer, message=text, random_id=random_id))
        else:
            # Совместимость с тестовыми/сторонними клиентами без raw API.
            # Рабочий run() включает _jobhunter_use_raw_requests.
            sent = await client.send_message(peer.user_id, text)
    except errors.PeerFloodError as e:
        with session_scope() as sess:
            policy.on_peer_flood(sess, str(e)[:120])
            sess.add(SendLog(application_id=item["app_id"], result="peerflood",
                             error_class="PeerFloodError", peer_id=item["handle"]))
            # Peer-триггер уходит в карантин, а не обратно в очередь.
            # Возврат в APPROVED стоил кампании второго страйка: после
            # первого PeerFlood на @angel_hrdigital заявка осталась в
            # очереди, через два дня сендер написал ТОМУ ЖЕ адресату,
            # снова словил PeerFlood — потолок 30→15→7 и ручной режим.
            app = sess.get(Application, item["app_id"])
            app.transition(Status.HANDLE_DEAD,
                           reason="peerflood-триггер, повтор запрещён")
            if app.employer_id:
                emp = sess.get(Employer, app.employer_id)
                if emp:
                    emp.do_not_contact = True
        return "stop:PeerFloodError"
    except errors.FloodWaitError as e:
        with session_scope() as sess:
            v = policy.on_flood_wait(sess, int(e.seconds))
            sess.add(SendLog(application_id=item["app_id"], result="flood",
                             error_class="FloodWaitError", error_seconds=int(e.seconds),
                             peer_id=item["handle"]))
            sess.get(Application, item["app_id"]).transition(Status.APPROVED)
        if v.allowed:
            await asyncio.sleep(v.wait_seconds)
            return "skipped:floodwait_%ds" % e.seconds
        return "stop:%s" % v.reason
    except errors.UserPrivacyRestrictedError:
        with session_scope() as sess:
            sess.get(Application, item["app_id"]).transition(
                Status.HANDLE_DEAD, reason="privacy restricted")
            sess.add(SendLog(application_id=item["app_id"], result="error",
                             error_class="UserPrivacyRestricted", peer_id=item["handle"]))
        return "skipped:privacy_restricted"
    except Exception as e:                      # неизвестный сбой — не ретраим вслепую
        with session_scope() as sess:
            app = sess.get(Application, item["app_id"])
            app.transition(Status.SEND_FAILED_AMBIGUOUS,
                           reason=type(e).__name__)
            app.send_error_class = type(e).__name__
            app.send_error_detail = str(e)[:500]
            app.send_next_try_at = None
            sess.add(SendLog(application_id=item["app_id"], result="error",
                             error_class=type(e).__name__, peer_id=item["handle"]))
        return "skipped:%s" % type(e).__name__

    # Текст доставлен — отклик состоялся. Теперь файл, отдельным сообщением
    # и отдельным try: заявка обязана дойти до SENT/AWAITING_REPLY даже если
    # документ не прошёл, иначе ответ рекрутёра на текст читаться не будет.
    if cv_real:
        cv_note = ""
        try:
            await asyncio.sleep(rng.uniform(2.0, 5.0))   # человеческая пауза
            if getattr(client, "_jobhunter_use_raw_requests", False):
                try:
                    await client.send_file(peer.user_id, cv_real,
                                           force_document=True,
                                           random_id=file_random_id)
                except TypeError as exc:
                    # Старые/тестовые клиенты без random_id остаются
                    # совместимыми; реальный Telethon принимает kwargs.
                    if "random_id" not in str(exc):
                        raise
                    await client.send_file(peer.user_id, cv_real,
                                           force_document=True)
            else:
                await client.send_file(peer.user_id, cv_real, force_document=True)
        except errors.PeerFloodError as e:
            # Страйк настоящий — засчитываем политике и замораживаем
            # адресата, но HANDLE_DEAD не ставим: текст у него уже есть,
            # и его ответ должен читаться.
            with session_scope() as sess:
                policy.on_peer_flood(sess, str(e)[:120])
                sess.add(SendLog(application_id=item["app_id"],
                                 result="cv_peerflood",
                                 error_class="PeerFloodError",
                                 peer_id=item["handle"]))
                if item.get("employer_id"):
                    emp = sess.get(Employer, item["employer_id"])
                    if emp:
                        emp.do_not_contact = True
            cv_note = "PeerFlood на файле"
        except Exception as e:                             # noqa: BLE001
            with session_scope() as sess:
                sess.add(SendLog(application_id=item["app_id"],
                                 result="cv_failed",
                                 error_class=type(e).__name__,
                                 peer_id=item["handle"]))
            cv_note = type(e).__name__
        if cv_note:
            from .. import notify
            notify.push("error",
                        "⚠️ Текст @%s доставлен, а резюме — нет (%s).\n"
                        "Отправь файл вручную из cv_base."
                        % (item["handle"], cv_note),
                        dedup="cv_fail:%s" % item["handle"])

    # успех
    with session_scope() as sess:
        app = sess.get(Application, item["app_id"])
        app.transition(Status.SENT)
        if not is_followup:
            app.sent_at = utcnow()
        app.telegram_msg_id = getattr(sent, "id", None)
        app.last_outbound_at = utcnow()
        app.transition(Status.FOLLOWED_UP if is_followup else Status.AWAITING_REPLY)
        if is_followup:
            app.followup_sent_at = utcnow()
            app.followup_due_at = None
        else:
            app.followup_due_at = (datetime.now(timezone.utc).replace(tzinfo=None)
                                   + __import__("datetime").timedelta(days=3))
        policy.register_sent(sess, cold=True)
        sess.add(SendLog(application_id=item["app_id"], result="ok",
                         peer_id=item["handle"]))
        sess.add(Message(application_id=item["app_id"], direction="out",
                         telegram_msg_id=getattr(sent, "id", None),
                         body=text, sent_at=utcnow(), is_auto=True))
        if item.get("employer_id"):
            emp = sess.get(Employer, item["employer_id"])
            if emp:
                emp.last_contacted_at = utcnow()
                emp.total_messages_sent += 1

    # Следы работы бота: копия на диск и раскладка диалога по папке.
    # Ни то, ни другое не влияет на доставку — отклик уже ушёл.
    from . import archive, folder
    archive.record(item["app_id"], "telegram", "@" + item["handle"], text,
                   job_title=item.get("title", ""),
                   company=item.get("company", ""), score=item.get("score", 0),
                   cv_path=item.get("cv_path", ""), kind="cold")
    res = await folder.add_to_folder(client, peer.user_id)
    if res.startswith("создана"):
        print("      %s" % res)
    return "ok"


def reclaim_stale_sending() -> int:
    """Возвращает в очередь заявки, зависшие в SENDING после сбоя.

    Просроченный lease без зафиксированного random_id возвращается в очередь.
    Если попытка уже получила idempotency key, доставка неоднозначна: заявка
    остаётся SEND_FAILED_AMBIGUOUS и требует явного решения владельца.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = 0
    with session_scope() as sess:
        rows = sess.scalars(select(Application).where(
            Application.status == Status.SENDING.value)).all()
        for app in rows:
            if app.sending_lease_until and app.sending_lease_until > now:
                continue                       # лизинг ещё жив — не трогаем
            if app.telegram_random_id or app.send_idempotency_key:
                app.transition(Status.SEND_FAILED_AMBIGUOUS,
                               reason="lease истёк: доставка неоднозначна")
                app.send_error_class = "LeaseExpiredAmbiguous"
                app.send_error_detail = (
                    "Проверь доставку вручную перед повторной отправкой")
            else:
                app.transition(Status.APPROVED, reason="лизинг SENDING истёк")
            app.worker_pid = 0
            app.sending_lease_until = None
            n += 1
    if n:
        print("Возвращено из зависшего SENDING: %d" % n)
    return n


def requeue_ambiguous(app_id: int) -> bool:
    """Явно вернуть неоднозначную отправку в очередь после проверки владельцем."""
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if not app or app.status != Status.SEND_FAILED_AMBIGUOUS.value:
            return False
        app.transition(Status.APPROVED, reason="владелец подтвердил повтор")
        app.send_next_try_at = None
        app.send_error_detail = ""
        return True


# Код возврата run(): «сессия отправлена, партия не закончена — зови ещё».
MORE_TO_SEND = 10


async def run(limit: int, dry: bool, max_sessions: int | None = None) -> int:
    s = get_settings()
    rng = random.Random()

    if not dry:
        reclaim_stale_sending()
    batch = pick_batch(limit)
    if not batch:
        print("Нечего отправлять: нет заявок в статусе APPROVED с живым @handle.")
        print("Сначала одобри партию: python -m jobhunter.outreach.approve --list")
        return 0

    # Ранний выход вне окна вежливости — до открытия сессии Telethon:
    # подключать аккаунт, чтобы тут же отключить, незачем. Второй такой же
    # чек стоит в цикле — окно может закрыться посреди многочасового прогона.
    if not dry:
        from zoneinfo import ZoneInfo
        hour_msk = datetime.now(ZoneInfo("Europe/Moscow")).hour
        if not policy.within_send_window(hour_msk):
            print("Вне окна 09-21 МСК (%02d:xx) — отправка ждёт утра." % hour_msk)
            return 0

    print("Партия: %d сообщений%s" % (len(batch), "  [DRY-RUN]" if dry else ""))
    for it in batch:
        print("  %5.0f  @%-24s %s" % (it["score"], it["handle"], (it["title"] or "")[:42]))

    client = None
    if not dry:
        from telethon import TelegramClient
        if not s.tg_api_id or not s.telegram_api_hash:
            print("\nНет TELEGRAM_API_ID / TELEGRAM_API_HASH в .env — см. my.telegram.org")
            return 2
        client = TelegramClient(s.telegram_session_path, s.tg_api_id,
                                s.telegram_api_hash)
        # Только основной отправщик включает raw requests с фиксированным
        # random_id. Это сохраняет совместимость с моками и не позволяет
        # случайному вызывающему коду тихо заявить об exactly-once.
        client._jobhunter_use_raw_requests = True
        client.flood_sleep_threshold = 60        # ставим явно, не полагаясь на дефолт
        await client.start(phone=s.telegram_phone or None)

        me = await client.get_me()
        if getattr(me, "restricted", False):
            print("\nАККАУНТ ОГРАНИЧЕН Telegram: %s" % getattr(me, "restriction_reason", ""))
            print("Автоотправка отменена.")
            await client.disconnect()
            return 3
        print("\nАккаунт: @%s (id %s), premium=%s, ограничений нет"
              % (me.username, me.id, getattr(me, "premium", False)))

    plan = policy.session_plan(len(batch), rng)
    print("План сессий: %s\n" % plan)

    idx, stats = 0, {"ok": 0, "skipped": 0, "stopped": 0}
    for si, chunk in enumerate(plan):
        if idx >= len(batch):
            break
        for k in range(chunk):
            if idx >= len(batch):
                break
            item = batch[idx]
            idx += 1
            # Окно вежливости 09:00-21:00: паузы между сессиями достигают
            # двух часов, и прогон, начатый днём, может доползти до ночи.
            # Ночной холодный DM — это жалоба и бан, а не отклик; остаток
            # партии никуда не денется — уйдёт завтрашним кроном. Таймзона
            # получателя неизвестна, берём московскую: основная аудитория —
            # русскоязычные каналы.
            from zoneinfo import ZoneInfo
            hour_msk = datetime.now(ZoneInfo("Europe/Moscow")).hour
            if not dry and not policy.within_send_window(hour_msk):
                print("\nВне окна 09-21 МСК (%02d:xx) — остальное завтра."
                      % hour_msk)
                if client:
                    await client.disconnect()
                _summary(stats)
                return 0
            res = await send_one(client, item, rng, dry)
            print("  [%d.%d] @%-22s %s" % (si + 1, k + 1, item["handle"], res))
            if res.startswith("stop:"):
                stats["stopped"] += 1
                print("\nСТОП: %s" % res[5:])
                if client:
                    await client.disconnect()
                _summary(stats)
                return 1
            stats["ok" if res == "ok" else "skipped"] += 1
            if idx < len(batch) and not dry:
                await asyncio.sleep(policy.gap_seconds(rng))
        # Сессионный режим: одна сессия за вызов, межсессионную паузу держит
        # планировщик, а не этот поток. Прежний сплошной прогон занимал
        # единственный tg-воркер на 3-7 часов, и всё это время кнопочные
        # решения владельца и чтение входящих стояли в очереди ЗА сном.
        if max_sessions is not None and si + 1 >= max_sessions                 and idx < len(batch):
            if client:
                await client.disconnect()
            _summary(stats)
            print("  сессия %d/%d завершена, остаток партии: %d — "
                  "продолжение по расписанию" % (si + 1, len(plan),
                                                 len(batch) - idx))
            return MORE_TO_SEND
        if idx < len(batch) and not dry:
            gap = policy.session_gap_seconds(rng)
            print("  ── пауза между сессиями %d мин ──" % round(gap / 60))
            await asyncio.sleep(gap)

    if client:
        await client.disconnect()
    with session_scope() as sess:
        policy.close_day(sess)
    _summary(stats)
    return 0


def _summary(stats: dict) -> None:
    print("\nИтог: отправлено %d, пропущено %d, остановок %d"
          % (stats["ok"], stats["skipped"], stats["stopped"]))


def main() -> int:
    ap = argparse.ArgumentParser(description="Отправка откликов в Telegram")
    ap.add_argument("--limit", type=int, default=None,
                    help="максимум сообщений за прогон (по умолчанию дневная квота)")
    ap.add_argument("--dry-run", action="store_true", help="без сети, только план")
    ap.add_argument("--login", action="store_true", help="только авторизация Telethon")
    args = ap.parse_args()

    s = get_settings()
    limit = args.limit or s.daily_cold_limit

    if args.login:
        from telethon import TelegramClient
        if not s.tg_api_id:
            print("Нет TELEGRAM_API_ID в .env — получи на my.telegram.org")
            return 2

        async def _login():
            c = TelegramClient(s.telegram_session_path, s.tg_api_id,
                               s.telegram_api_hash)
            await c.start(phone=s.telegram_phone or None)
            me = await c.get_me()
            print("Авторизовано: @%s (id %s)" % (me.username, me.id))
            await c.disconnect()
        asyncio.run(_login())
        return 0

    with ProcessLock(lock_path()):
        return asyncio.run(run(limit, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
