"""Политика безопасности отправки: квоты, стоп-краны, реакция на флуд.

Темп задан пользователем (по умолчанию 30 холодных/день, без прогрева).
Здесь не ограничивается сам темп — здесь стоят предохранители, которые
не дают потерять аккаунт при срабатывании анти-спама Telegram.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..config import get_settings
from ..models import CampaignState, DailyQuota, SendLock, utcnow

# Ответы в существующие диалоги — отдельный, более щедрый бакет:
# они низкорисковые и не должны съедать холодную квоту.
WARM_REPLY_DAILY = 80

# Темп холодных задан ПАУЗОЙ, а не дневным счётчиком (решение владельца 23.09):
# новому адресату — не чаще раза в полчаса, дневного потолка нет. В окне
# вежливости 09-21 это само по себе даёт не больше ~24 сообщений в день.
# Тёплые ответы тем, кто уже написал сам, эта пауза не касается — у них свой
# бакет (WARM_REPLY_DAILY) и своя проверка в convo/send.py.
COLD_GAP_MINUTES = 30
COLD_GAP_REASON = "пауза между холодными"

PEERFLOOD_LOCK_HOURS = 48
FLOOD_SLEEP_MAX = 300          # выше — это уже анти-спам сигнал, не rate limit


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""
    wait_seconds: int = 0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_state(sess) -> CampaignState:
    st = sess.get(CampaignState, 1)
    if st is None:
        s = get_settings()
        st = CampaignState(id=1, quota_ceiling=s.daily_cold_limit)
        sess.add(st)
        sess.flush()
    return st


def get_quota(sess, day: str | None = None) -> DailyQuota:
    day = day or _today()
    q = sess.get(DailyQuota, day)
    if q is None:
        st = get_state(sess)
        cap = st.quota_ceiling
        # Первый день после истёкшего пирфлуд-лока — разведка тремя
        # сообщениями, а не сразу полный потолок: третий страйк подряд
        # может стоить аккаунта надолго.
        lk = sess.get(SendLock, 1)
        if lk and lk.locked_until:
            since = (datetime.now(timezone.utc).replace(tzinfo=None)
                     - lk.locked_until)
            if timedelta(0) <= since < timedelta(hours=24):
                cap = min(3, cap)
        q = DailyQuota(date=day, planned_cap=cap)
        sess.add(q)
        sess.flush()
    return q


def get_lock(sess) -> SendLock:
    lk = sess.get(SendLock, 1)
    if lk is None:
        lk = SendLock(id=1)
        sess.add(lk)
        sess.flush()
    return lk


def kill_switch_active() -> bool:
    """Файл-стоп-кран: проверяется ПЕРЕД каждой отправкой, не раз на партию."""
    return get_settings().kill_switch.exists()


def cold_gap_left(sess) -> int:
    """Секунд до следующего холодного сообщения. 0 — можно сейчас."""
    st = get_state(sess)
    last = st.last_cold_sent_at
    if not last:
        return 0
    # utcnow() отдаёт время с зоной, из SQLite оно читается без неё: в одной
    # транзакции метка ещё «с зоной», после перечитывания — уже без. Вычитание
    # разнородных дат падает TypeError, поэтому приводим к наивному UTC.
    if last.tzinfo is not None:
        last = last.astimezone(timezone.utc).replace(tzinfo=None)
    passed = (datetime.now(timezone.utc).replace(tzinfo=None) - last).total_seconds()
    return max(0, int(COLD_GAP_MINUTES * 60 - passed))


def can_send_cold(sess) -> Verdict:
    """Можно ли отправить ещё одно холодное сообщение прямо сейчас.

    Дневного потолка нет: темп держит пауза между сообщениями. Счётчик
    отправленных за день остаётся — он нужен пульту и отчётам, но ничего
    не запрещает.
    """
    if kill_switch_active():
        return Verdict(False, "kill-switch: %s" % get_settings().kill_switch.name)

    st = get_state(sess)
    if st.manual_only:
        return Verdict(False, "кампания переведена в ручной режим (2× PeerFlood)")

    lk = get_lock(sess)
    if lk.locked_until and lk.locked_until > datetime.now(timezone.utc).replace(tzinfo=None):
        left = lk.locked_until - datetime.now(timezone.utc).replace(tzinfo=None)
        return Verdict(False, "лок до %s (%s)" % (lk.locked_until, lk.reason),
                       int(left.total_seconds()))

    left_s = cold_gap_left(sess)
    if left_s > 0:
        return Verdict(False, "%s: ещё %d мин" % (COLD_GAP_REASON,
                                                  -(-left_s // 60)), left_s)
    return Verdict(True, "пауза выдержана, сегодня отправлено %d"
                   % get_quota(sess).sent_count)


def mark_cold_attempt(sess) -> None:
    """Пауза отсчитывается с момента, когда запрос к незнакомцу ушёл в Telegram.

    Не только с успешной доставки: отказ по приватности или обрыв посреди
    запроса — тоже сообщение чужому человеку. Считай паузу лишь от успеха —
    следующий адресат получал бы сообщение сразу за неудачным, и пачка
    возвращалась бы в обход паузы. Метка хранится в базе: прогон запускается
    планировщиком заново, память процесса перезапуска не переживает.
    """
    get_state(sess).last_cold_sent_at = utcnow()


def register_sent(sess, cold: bool = True) -> None:
    q = get_quota(sess)
    if cold:
        q.sent_count += 1
        mark_cold_attempt(sess)


def email_sent_today(sess) -> int:
    """Одинаковый счётчик для панели и отправщика; день кампании — UTC."""
    from sqlalchemy import func, select

    from ..models import SendLog
    day_start = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0,
                                                  second=0, microsecond=0)
    return int(sess.scalar(select(func.count(SendLog.id)).where(
        SendLog.result == "ok", SendLog.attempted_at >= day_start,
        SendLog.peer_id.contains("@"), ~SendLog.peer_id.startswith("@"))) or 0)


def _email_log_days(sess, since: datetime) -> dict:
    """{дата: [отправлено, отбивок]} по почтовым записям журнала."""
    from sqlalchemy import select

    from ..models import SendLog
    out: dict = {}
    for result, at in sess.execute(select(SendLog.result, SendLog.attempted_at).where(
            SendLog.attempted_at >= since, SendLog.result.in_(("ok", "bounce")),
            SendLog.peer_id.contains("@"), ~SendLog.peer_id.startswith("@"))).all():
        day = out.setdefault(at.date(), [0, 0])
        day[0 if result == "ok" else 1] += 1
    return out


def email_bounces_today(sess) -> int:
    today = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0,
                                              second=0, microsecond=0)
    return sum(b for _, b in _email_log_days(sess, today).values())


def email_clean_days(sess) -> int:
    """Подряд идущие прошлые дни с отправками и без отбивок.

    Дни без писем серию не рвут (выходные, пустая очередь), отбивка — рвёт.
    Сегодняшний день не считается: он ещё не закончился.
    """
    s = get_settings()
    today = datetime.now(timezone.utc).replace(tzinfo=None, hour=0, minute=0,
                                              second=0, microsecond=0)
    try:
        since = datetime.fromisoformat(s.email_warmup_since)
    except ValueError:
        since = today
    since = max(since, today - timedelta(days=60))
    streak = 0
    for day, (ok, bounced) in sorted(_email_log_days(sess, since).items(), reverse=True):
        if day >= today.date():
            continue
        if bounced:
            break
        if ok:
            streak += 1
    return streak


def email_daily_cap(sess) -> int:
    """Потолок писем на сегодня с учётом прогрева."""
    s = get_settings()
    if s.email_daily_limit <= s.email_warmup_start:
        return s.email_daily_limit
    return min(s.email_daily_limit,
               s.email_warmup_start + s.email_warmup_step * email_clean_days(sess))


def can_send_email(sess) -> Verdict:
    if kill_switch_active():
        return Verdict(False, "активен стоп-кран")
    bounced = email_bounces_today(sess)
    if bounced > get_settings().email_bounce_stop:
        return Verdict(False, "отбивок сегодня %d — почта стоит до завтра" % bounced)
    sent = email_sent_today(sess)
    cap = email_daily_cap(sess)
    if sent >= cap:
        return Verdict(False, "дневная квота email исчерпана (%d/%d)" % (sent, cap))
    return Verdict(True, "%d/%d email за сегодня" % (sent, cap))


def register_resolve(sess) -> None:
    """Резолв юзернейма имеет свои лимиты — считаем отдельно."""
    get_quota(sess).resolve_count += 1


def on_flood_wait(sess, seconds: int) -> Verdict:
    """FloodWaitError. Короткий — ждём. Длинный — это уже сигнал, не лимит."""
    q = get_quota(sess)
    q.floodwait_total_seconds += int(seconds)
    if seconds <= FLOOD_SLEEP_MAX:
        return Verdict(True, "flood wait %ds — ждём" % seconds, int(seconds))
    return on_peer_flood(sess, "FloodWait %ds" % seconds)


def on_peer_flood(sess, detail: str = "") -> Verdict:
    """PeerFloodError — аккаунт уже в анти-спам списке. Немедленный стоп.

    Это не ограничение выбранного пользователем темпа: без этой реакции
    следующие сообщения идут в пустоту, а аккаунт уходит в постоянный бан.
    """
    st = get_state(sess)
    q = get_quota(sess)
    lk = get_lock(sess)

    q.peerflood_count += 1
    q.clean_day = False
    st.peerflood_total += 1
    st.consecutive_clean_days = 0
    st.quota_ceiling = max(2, st.quota_ceiling // 2)     # навсегда для кампании

    lk.locked_until = (datetime.now(timezone.utc).replace(tzinfo=None)
                       + timedelta(hours=PEERFLOOD_LOCK_HOURS))
    lk.scope = "cold_only"        # переписка с ответившими продолжается
    lk.reason = "peerflood: %s" % (detail or "")
    lk.set_at = utcnow()
    lk.set_by = "policy"

    # Уведомление ставится ТОЙ ЖЕ транзакцией, что и понижение квоты: иначе
    # возможен коммит наказания без предупреждения владельцу или наоборот.
    from .. import notify
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if st.peerflood_total >= 2:
        st.manual_only = True
        notify.push("error",
                    "🛑 Второй PeerFlood — кампания в ручном режиме.\n"
                    "Автоматическая отправка остановлена до твоего решения.\n"
                    "Причина: %s" % (detail or "—"),
                    dedup="manual_only", sess=sess)
        return Verdict(False, "второй PeerFlood — постоянный ручной режим")

    notify.push("peerflood",
                "⚠️ PeerFlood: Telegram придержал отправку.\n"
                "Стоп на %d ч; дальше — по одному холодному раз в %d мин.\n"
                "Переписка с теми, кто уже ответил, продолжается."
                % (PEERFLOOD_LOCK_HOURS, COLD_GAP_MINUTES),
                dedup="peerflood:%s" % today, sess=sess)
    return Verdict(False, "PeerFlood: стоп на %dч" % PEERFLOOD_LOCK_HOURS)


def resume_manual_only(sess) -> bool:
    """Снять ручной режим Telegram по явному решению владельца. True — было что снимать.

    До этой функции снять ручной режим было НЕЧЕМ: on_peer_flood переводит кампанию
    в manual_only и пишет «до твоего решения», а решения в коде не было — ни кнопки,
    ни команды. Временный лок (locked_until, 48ч) при этом отключён отдельно и мог
    истечь днями раньше; ручной режим оставался висеть вечно (docs/audit 2026-09,
    находка «Telegram — причина сбоя», 22.09).

    Понижение quota_ceiling и историю peerflood_total не трогаем: темп навсегда
    остаётся ниже прежнего, а если новый PeerFlood случится сразу после возобновления,
    on_peer_flood снова поставит ручной режим при первом же срабатывании.

    Это НЕ обход антиспама Telegram: временный лок (реакция самого Telegram) к этому
    моменту уже истёк сам; здесь снимается только наш добавочный самозапрет. Решение —
    осознанное действие владельца, а не автоматика; перед ним стоит проверить статус
    аккаунта у @SpamBot.
    """
    st = get_state(sess)
    if not st.manual_only:
        return False
    st.manual_only = False
    from .. import notify
    notify.push("info", "▶️ Telegram возобновлён вручную. Холодные — по одному раз в %d мин."
                % COLD_GAP_MINUTES, sess=sess)
    return True


def close_day(sess) -> None:
    """Итог дня: чистый день увеличивает счётчик доверия."""
    q = get_quota(sess)
    st = get_state(sess)
    if q.clean_day and q.sent_count > 0:
        st.consecutive_clean_days += 1
        # Восстановление доверия: каждые 3 чистых дня подряд возвращают
        # единицу потолка. Максимум 15, не исходные 30: аккаунт с двумя
        # страйками к прежнему темпу не возвращается.
        if st.consecutive_clean_days % 3 == 0 and st.quota_ceiling < 15:
            st.quota_ceiling += 1


# ── тайминг: сессиями, а не равномерным рандомом ──

def session_plan(daily_cap: int, rng: random.Random | None = None) -> list:
    """План дня: 4-6 сессий по 2-6 сообщений.

    Равномерные интервалы — сами по себе машинный паттерн; люди пишут
    пачками и потом молчат час.
    """
    rng = rng or random.Random()
    sessions = rng.randint(4, 6)
    left, plan = daily_cap, []
    for i in range(sessions):
        if left <= 0:
            break
        remaining_sessions = sessions - i
        chunk = max(1, min(left, round(left / remaining_sessions + rng.uniform(-1, 1))))
        plan.append(chunk)
        left -= chunk
    if left > 0 and plan:
        plan[-1] += left
    return plan


def gap_seconds(rng: random.Random | None = None) -> float:
    """Когда планировщику звать отправку снова: логнормальная, медиана ~33 мин.

    Раньше медиана держалась в секундах (90 → 120 с): несколько сообщений
    подряд уходили за пару минут, и сама плотность пачки — независимо от
    текста — уже похожа на спам-паттерн для Telegram. К 22.09 счёт дошёл до
    6 страйков. Решение владельца 22-23.09: новому адресату — не чаще раза
    в полчаса, дневного потолка нет.

    Жёсткий минимум держит can_send_cold() по метке в базе; здесь — разброс,
    чтобы вызовы не попадали в одну и ту же секунду получаса: ровный интервал
    сам по себе машинный признак.
    """
    rng = rng or random.Random()
    v = rng.lognormvariate(7.6, 0.12)    # медиана e^7.6 ≈ 1998 с ≈ 33 мин
    return max(float(COLD_GAP_MINUTES * 60), min(2700.0, v)) + rng.uniform(0, 30)


def typing_seconds(text: str, rng: random.Random | None = None) -> float:
    """Сколько «печатать» перед отправкой — реальный MTProto-сигнал."""
    rng = rng or random.Random()
    return min(12.0, len(text or "") / rng.uniform(6.0, 9.0))


# ── окно по времени ПОЛУЧАТЕЛЯ ──

def within_send_window(hour_local: int) -> bool:
    """09:00-21:00 по времени получателя. Ночной холодный DM = жалоба."""
    return 9 <= hour_local < 21
