"""Автопилот: система работает сама, без ручных команд.

Что делает по кругу:
  09:30  сбор вакансий из всех источников (в Telegram — расширенный)
  13:30, 18:30  повторный сбор Telegram-лент
  10:00  подготовка откликов (резюме + письмо + гейт)
  10:15  авто-одобрение того, что прошло гейт и набрало нужный скор
  10:30  отправка email (партиями, с паузами)
  11:00-19:00  отправка Telegram сессиями, в пределах дневной квоты
  каждый час  проверка входящих и напоминания
  20:00  итог дня в лог

Языковая модель для этого не нужна: отправка — это SMTP и MTProto по
расписанию. LLM пригодился бы для черновиков ответов на нестандартные
письма, но не для рассылки.

Предохранители те же: kill-switch перед каждой отправкой, дневная квота,
PeerFlood → стоп на 48ч, анти-фабрикация гейт перед постановкой в очередь.

    python -m jobhunter.autopilot --once     # один прогон цикла, для проверки
    python -m jobhunter.autopilot            # демон по расписанию
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from . import health
from .config import get_settings
from .db import session_scope
from .models import Application, Batch, ContactKind, Job, Status, utcnow
from .outreach import policy

# Порог автоодобрения. Ниже — ждёт ручного решения на дашборде.
AUTO_APPROVE_MIN_SCORE = 55.0
AUTO_APPROVE_MAX_PER_RUN = 25

log = logging.getLogger("autopilot")

# Планировщик демона; в режиме --once остаётся пустым.
_SCHED: dict = {}


def _setup_logging():
    s = get_settings()
    from pathlib import Path
    # Именно log_dir, а не «рядом с базой»: в контейнере база лежит в томе,
    # и лог уехал бы туда же, где владелец его не увидит.
    logdir = Path(s.log_dir)
    logdir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(logdir / "autopilot.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
    # httpx пишет INFO на каждый запрос — сотни строк за один сбор.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    # APScheduler пишет INFO на каждый запуск задания, включая ежеминутный
    # пульс: за сутки это полторы тысячи строк, в которых тонет полезное.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────── шаги ──

def step_ingest() -> dict:
    """Сбор вакансий из всех источников."""
    from .ingest.all_sources import _careered_jobs
    from .ingest.base import save_jobs
    from .ingest.hn import HackerNewsSource

    totals = {}
    try:
        totals["careered"] = save_jobs(_careered_jobs(None), verbose=False)
    except Exception as e:
        log.warning("careered недоступен: %s", str(e)[:100])
        totals["careered"] = {"errors": 1}
    try:
        totals["telegram"] = step_ingest_telegram()
    except Exception as e:
        log.warning("telegram-каналы: %s", str(e)[:100])
        totals["telegram"] = {"error": type(e).__name__}
    try:
        totals["hn"] = save_jobs(HackerNewsSource().iter_jobs(threads=2), verbose=False)
    except Exception as e:
        log.warning("HN: %s", str(e)[:100])
        totals["hn"] = {"error": type(e).__name__}
    try:
        from .ingest.ats import ATSSource
        totals["ats"] = save_jobs(ATSSource().iter_jobs(), verbose=False)
    except Exception as e:
        log.warning("ATS: %s", str(e)[:100])
        totals["ats"] = {"error": type(e).__name__}
    try:
        from .ingest.boards import SOURCES as BOARD_SOURCES
        from .ingest.boards import BoardsSource
        boards = [n for n in BOARD_SOURCES if n not in disabled_sources()]
        totals["boards"] = save_jobs(BoardsSource(only=boards).iter_jobs(), verbose=False)
    except Exception as e:
        log.warning("job-борды: %s", str(e)[:100])
        totals["boards"] = {"error": type(e).__name__}
    # API-источники (Трудвсем с прямыми email, кросс-поиск Workable, Muse):
    # были написаны давно, но к автопилоту не подключены — один ручной
    # прогон 5 августа. Каждый в своём try: смерть одного API не должна
    # отменять остальные.
    try:
        from .ingest.jobapis import SOURCES as API_SOURCES
    except Exception as e:
        # Голый импорт между защищёнными блоками ронял бы весь шаг вместе
        # с cycle_once — источники обязаны умирать поодиночке.
        log.warning("jobapis import: %s", str(e)[:100])
        totals["jobapis"] = {"error": type(e).__name__}
        API_SOURCES = {}
    for api_name, api_cls in API_SOURCES.items():
        if api_name in disabled_sources():
            totals[api_name] = {"skipped": "источник отключён (DISABLED_SOURCES)"}
            continue
        try:
            totals[api_name] = save_jobs(api_cls().iter_jobs(), verbose=False)
        except Exception as e:
            log.warning("%s: %s", api_name, str(e)[:100])
            totals[api_name] = {"error": type(e).__name__}

    # Сбор ссылок на непокрытые ATS-доски — чистый regex по свежесобранному,
    # без сети; сами проверки идут отдельным воскресным шагом.
    try:
        from .ingest.ats_discover import harvest
        h = harvest()
        if h.get("new"):
            log.info("ats-кандидаты: новых досок %d", h["new"])
    except Exception as e:
        log.warning("ats-harvest: %s", str(e)[:100])

    try:
        from .ingest.base import close_stale_jobs
        closed = close_stale_jobs(age_days=45)
        if closed:
            log.info("закрыто устаревших вакансий: %d", closed)
    except Exception as e:
        log.warning("закрытие устаревших вакансий: %s", str(e)[:120])

    new = sum(t.get("new", 0) for t in totals.values())
    contacts = sum(t.get("with_contact", 0) for t in totals.values())
    log.info("ingest: новых %d, с прямым контактом %d", new, contacts)
    result = ingest_result(totals, new, contacts)
    if result["failed_sources"]:
        log.warning("ingest: не отработали источники %s",
                    ", ".join(result["failed_sources"]))
    return result


def ingest_result(totals: dict, new: int, contacts: int) -> dict:
    """Итог сбора для планировщика: провал — только если не сработал ни один источник.

    Детали источников лежали словарём, и has_errors помечал весь шаг
    ошибкой из-за одного канала из двухсот (10.09 — forpython) или одного
    упавшего API. Маркер «сбор выполнен» застрял на 07.09, и догон при
    каждом включении машины заново гонял сбор на полчаса с лишним.

    Детали теперь списком: планировщик заглядывает только в словари, так
    что ошибкой шага считается лишь отказ всех источников сразу. Источник
    считается упавшим, если бросил исключение, если у Telegram не
    открылась половина каналов или если сохранение ничего не увидело.
    """
    def dead(t) -> bool:
        if not isinstance(t, dict) or t.get("error"):
            return True
        channels = t.get("scan_channels") or 0
        if channels:
            return (t.get("scan_errors") or 0) * 2 >= channels
        return bool(t.get("errors")) and not (t.get("seen") or t.get("new"))

    failed = sorted(name for name, t in totals.items() if dead(t))
    details = [dict({"source": name}, **{k: v for k, v in t.items()
                                         if isinstance(v, (int, float, str, bool))})
               for name, t in totals.items() if isinstance(t, dict)]
    out = {"new": new, "with_contact": contacts, "failed_sources": failed,
           "sources": details}
    if totals and len(failed) == len(totals):
        out["error"] = "все источники сбора недоступны"
    return out


def step_ingest_telegram() -> dict:
    """Частый отдельный проход публичных Telegram-лент.

    Полный ingest остаётся утром, а этот шаг повторяет только Telegram днём
    и вечером. Так новые посты не ждут следующего утра, а ошибки по каналам
    видны отдельно, даже если остальные источники отработали нормально.
    """
    from .ingest.tgchannels import ingest_telegram

    try:
        stats = ingest_telegram()
    except Exception as e:
        log.warning("telegram-каналы: %s", str(e)[:120])
        return {"errors": 1}

    log.info(
        "telegram: каналов %d, страниц %d, постов %d, вакансий %d, "
        "контактов %d, ошибок %d, новых %d",
        stats.get("scan_channels", 0), stats.get("scan_pages", 0),
        stats.get("scan_posts", 0), stats.get("scan_vacancies", 0),
        stats.get("scan_contacts", 0), stats.get("scan_errors", 0),
        stats.get("new", 0),
    )
    if stats.get("scan_error_channels"):
        log.warning("telegram: ошибки каналов %s",
                    ", ".join(stats["scan_error_channels"][:12]))
    return stats


def step_prepare() -> dict:
    """Резюме + письмо + гейт для всех новых заявок.

    Под защитой, как остальные шаги: сбой подготовки не должен ронять цикл.
    Так уже случилось при переезде в Docker — генератор резюме искал шрифт по
    windows-пути, падал на первой же заявке, и весь шаг молча не работал.
    """
    from .pipeline import prepare_all_discovered
    try:
        stats = prepare_all_discovered()
    except Exception as e:
        log.error("подготовка: %s: %s", type(e).__name__, str(e)[:160])
        from . import notify
        notify.push("error",
                    "⚠️ Подготовка откликов упала: %s\n%s"
                    % (type(e).__name__, str(e)[:300]),
                    dedup="prepare_fail:%s" % datetime.now(timezone.utc)
                                                      .strftime("%Y-%m-%d"))
        return {"error": type(e).__name__}
    log.info("подготовка: готово %d, отсеяно %d, гейт отклонил %d",
             stats["pending"], stats["rejected"], stats["gate_failed"])
    return stats


def close_old_low_score(days: int = 7, threshold: float = AUTO_APPROVE_MIN_SCORE) -> int:
    """Закрыть старую очередь, которая уже не пройдёт автоаппрув.

    Это не удаление: WITHDRAWN сохраняет заявку и дедуп-историю, но не даёт
    недельному низкобалльному хвосту каждый день занимать место в очереди.
    """
    cutoff = utcnow().replace(tzinfo=None) - timedelta(days=days)
    closed = 0
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application).where(
                Application.status == Status.PENDING_APPROVAL.value,
                Application.score < threshold,
                Application.updated_at < cutoff)).all()
        for app in rows:
            if app.advance(Status.WITHDRAWN,
                           reason="низкий score в очереди более %d дней" % days):
                closed += 1
    return closed


def step_auto_approve() -> int:
    """Новые автоодобрения: прежний порог/гейт плюс свежая проверка требований."""
    from .match.explain import MAIN_TRACKS, approval_problem
    from .match.role import classify
    from .models import SendLog

    approved = 0
    closed = close_old_low_score()
    if closed:
        log.info("очередь: закрыто старых низкобалльных заявок %d", closed)
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.PENDING_APPROVAL.value,
                   Application.score >= AUTO_APPROVE_MIN_SCORE,
                   Application.gate_passed.is_(True),
                   Application.sent_at.is_(None),
                   Application.applied_at.is_(None),
                   Application.send_attempts == 0,
                   Application.send_last_attempt_at.is_(None),
                   ~select(SendLog.id).where(
                       SendLog.application_id == Application.id).exists(),
                   # Письмо, забракованное самопроверкой, уходит только с
                   # ведома владельца: правдивый, но невнятный текст тратит
                   # единственную попытку у рекрутёра так же, как выдумка.
                   Application.review_note == "")
            # A score-only pre-limit can hide every main-track vacancy behind
            # additional roles or rows requiring review. Stream the eligible
            # queue; apply the batch limit after review and track selection.
            .order_by(Application.score.desc(), Application.id.desc())
            .execution_options(yield_per=200))
        # Ранний выход при пустой основной очереди недопустим: ниже ещё
        # одобрение почтовых follow-up, и оно должно идти даже в день, когда
        # новых заявок нет — как раз в такие дни напоминания и накапливаются.
        candidates = []
        for a in rows:
            job = sess.get(Job, a.job_id)
            if not job:
                if a.advance(Status.WITHDRAWN, reason="вакансия удалена"):
                    log.info("заявка #%d закрыта: вакансия не найдена", a.id)
                continue
            if (job.source or "").startswith("direct:"):
                continue          # у прямых писем свои гейты и своё одобрение (outreach/direct.py)
            problem = approval_problem(a, job)
            if problem:
                log.info("заявка #%d ждет проверки требований: %s", a.id, problem)
                continue
            # Всё, что не умеем отправлять и не умеем подать через ATS,
            # закрываем явно: вечный PENDING_APPROVAL был немым тупиком.
            if job.contact_kind not in (ContactKind.USER_HANDLE.value,
                                        ContactKind.EMAIL.value):
                from .apply.forms import form_ref_from_job
                ref = form_ref_from_job(job)
                if not ref or ref[0] != "ashby":
                    if a.advance(Status.WITHDRAWN,
                                 reason="нет поддерживаемого канала отклика"):
                        log.info("заявка #%d закрыта: нет канала отклика", a.id)
                    continue
            candidates.append((a, job))

        # Источник с достаточной историей и нулевым ответом не блокирует
        # очередь, но уступает место равному кандидату с рабочей конверсией.
        try:
            from .report import source_preferences, source_priority_penalty
            source_rates = source_preferences(min_sent=5)
        except Exception:
            source_rates = {}
            source_priority_penalty = lambda source, preferences: 0.0

        def priority(item):
            app, job = item
            main = classify(job.title, job.tag, job.description_raw).family in MAIN_TRACKS
            return main, app.score - source_priority_penalty(job.source, source_rates)

        candidates.sort(key=lambda item: (priority(item), item[1].posted_at or 0, item[0].id),
                        reverse=True)
        candidates = candidates[:AUTO_APPROVE_MAX_PER_RUN]
        if candidates:
            batch = Batch(planned_count=len(candidates), approved_at=utcnow(),
                          approved_count=len(candidates))
            sess.add(batch)
            sess.flush()
            for a, _job in candidates:
                a.transition(Status.APPROVED)
                a.approved_at = utcnow()
                a.batch_id = batch.id
                approved += 1

        # Почтовые follow-up одобряются сами: без этого они навсегда висят в
        # FOLLOWUP_PENDING_APPROVAL — отправщик берёт только APPROVED, а
        # экрана для их ручного аппрува нет. Телеграмные остаются на ручном
        # решении владельца: там каждое сообщение тратит дневную квоту
        # аккаунта, и напоминание не должно съедать её незаметно.
        fu_rows = sess.scalars(
            select(Application)
            .where(Application.status == Status.FOLLOWUP_PENDING_APPROVAL.value,
                   Application.followup_body != "")).all()
        fu_approved = 0
        for a in fu_rows:
            job = sess.get(Job, a.job_id)
            if not job or job.contact_kind != ContactKind.EMAIL.value:
                continue
            a.transition(Status.APPROVED)
            a.approved_at = utcnow()
            fu_approved += 1
        if fu_approved:
            log.info("авто-одобрено follow-up (email): %d", fu_approved)
        approved += fu_approved
    log.info("авто-одобрено: %d (порог скора %.0f)", approved, AUTO_APPROVE_MIN_SCORE)
    return approved


def step_stage_ashby() -> dict:
    """Вечером вернуть ashby-кандидатов в конвейер: утренний prepare сделает
    письма, auto_approve одобрит, step_submit_ashby подаст."""
    from .apply.submit_ashby import stage
    try:
        stats = stage(limit=10)
    except Exception as e:                                  # noqa: BLE001
        log.error("ashby stage: %s: %s", type(e).__name__, str(e)[:160])
        return {"error": type(e).__name__}
    if stats.get("staged"):
        log.info("ashby: в подготовку писем: %d", stats["staged"])
    return stats


def step_submit_ashby() -> dict:
    """Подача откликов через анкеты Ashby (одобренные, свой дневной лимит)."""
    from .apply.submit_ashby import run as ashby_run
    s = get_settings()
    if policy.kill_switch_active():
        log.warning("ashby пропущен: активен стоп-кран")
        return {"blocked": "активен стоп-кран"}
    try:
        stats = ashby_run(limit=s.ats_daily_limit, dry=False)
    except Exception as e:                                  # noqa: BLE001
        log.error("ashby подача: %s: %s", type(e).__name__, str(e)[:160])
        return {"error": type(e).__name__}
    log.info("ashby: подано %d, пропущено %d, ошибок %d",
             stats["ok"], stats["skipped"], stats["errors"])
    return stats


def step_send_email() -> dict:
    from .convo import gmailapi
    from .outreach.mailer import send_batch
    s = get_settings()
    if not gmailapi.sending_configured():
        log.info("email пропущен: почта не настроена (ни Gmail API, ни SMTP)")
        return {"blocked": "почта не настроена"}
    if policy.kill_switch_active():
        log.warning("email пропущен: активен стоп-кран")
        return {"blocked": "активен стоп-кран"}
    try:
        rc = send_batch(s.email_daily_limit, dry=False)
    except Exception as e:
        # Не только в лог: молча съеденная ошибка SMTP означает, что почтовый
        # канал может стоять днями, а владелец узнает об этом по нулям в
        # статистике. Дедуп по дню — одна авария, одно сообщение.
        log.error("email: %s: %s", type(e).__name__, str(e)[:120])
        from . import notify
        notify.push("error",
                    "⚠️ Почтовая отправка упала: %s\n%s"
                    % (type(e).__name__, str(e)[:300]),
                    dedup="email_fail:%s" % datetime.now(timezone.utc)
                                                    .strftime("%Y-%m-%d"))
        raise
    try:
        # Прямые письма уходят без просмотра — владелец видит их постфактум.
        from . import notify
        from .outreach import direct
        text = direct.digest_text()
        if text:
            notify.push("direct_sent", text,
                        dedup="direct_digest:%s" % datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    except Exception as e:                                  # noqa: BLE001
        log.warning("сводка прямых писем: %s", str(e)[:120])
    return {"error": "почтовая партия завершилась с ошибкой"} if rc else {"ok": True}


def email_after_approve(missed: list) -> list:
    """Порядок догона: почта идёт ПОСЛЕ догнавшего одобрения.

    18.09 машина включилась в 10:19: сбор, подготовка и одобрение ушли в
    догон, а почта сработала штатно в 10:30 — по пустой очереди, потому что
    цепочка ещё качала вакансии. Всё одобренное в тот день ждало бы до завтра.
    Повторный запуск безопасен: партию держит файловый лок, письма —
    дневной потолок и проверка «уже отправлено».
    """
    if "approve" in missed and "email" not in missed:
        i = missed.index("approve") + 1
        if i < len(missed) and missed[i] == "direct":
            i += 1                # прямые письма готовятся до отправки, иначе уйдут только завтра
        return missed[:i] + ["email"] + missed[i:]
    return missed


def step_direct() -> dict:
    """Прямые письма руководителям и рефералам: найти адресатов, подготовить, одобрить.

    Отправляет их штатный шаг почты — под общим дневным потолком и своим лимитом.
    """
    from .outreach import direct
    if policy.kill_switch_active():
        return {"blocked": "активен стоп-кран"}
    stats = direct.run()
    log.info("прямые письма: %s", stats)
    return stats


def step_resend_en() -> dict:
    """Повтор на английском тем HN-компаниям, кому ушло русское письмо (до 4 в день)."""
    from .convo import gmailapi
    from .outreach import resend_en
    if not gmailapi.sending_configured():
        return {"blocked": "почта не настроена"}
    if policy.kill_switch_active():
        return {"blocked": "активен стоп-кран"}
    stats = resend_en.run()
    log.info("повтор на английском: %s", stats)
    return stats


def _rearm_telegram(after_seconds: float):
    """Назначить следующий заход отправки через after_seconds. None вне демона.

    Темп холодных держит пауза, а не дневной счётчик: каждый заход отправляет
    одно сообщение и переназначает себя. Спать внутри прогона нельзя —
    однопоточная tg-очередь тогда не отбивает пульс, и сторож убивает
    контейнер посреди отправки.
    """
    sched = _SCHED.get("sched")
    if sched is None:
        return None
    when = datetime.now() + timedelta(seconds=max(60.0, after_seconds))
    sched.add_job(step_send_telegram, "date", id="tg_more",
                  replace_existing=True, run_date=when,
                  misfire_grace_time=3600, executor="tg")
    # APScheduler держит задание в памяти. Без записи в базу перезапуск
    # контейнера посреди паузы обрывал бы цепочку до следующего крона
    # (вплоть до завтра); на старте демон восстанавливает «partial».
    from .observability import record
    record("sender:telegram", "partial", next_run_at=when.astimezone())
    log.info("следующая отправка в %s", when.strftime("%H:%M"))
    return when


def step_send_telegram() -> dict:
    from .outreach.sender import run as sender_run
    s = get_settings()
    if not (s.tg_api_id and s.telegram_api_hash):
        log.info("telegram пропущен: нет API-ключей")
        return {"blocked": "нет API-ключей Telegram"}
    from pathlib import Path
    if not Path(s.telegram_session_path).exists():
        log.info("telegram пропущен: нет сессии (python tg_login.py)")
        return {"blocked": "нет Telegram-сессии"}
    if policy.kill_switch_active():
        log.warning("telegram пропущен: активен стоп-кран")
        return {"blocked": "активен стоп-кран"}
    with session_scope() as sess:
        v = policy.can_send_cold(sess)
    if not v.allowed:
        log.info("telegram пропущен: %s", v.reason)
        if policy.COLD_GAP_REASON in v.reason:
            # Пауза — штатный режим, а не отказ: молча переназначаем прогон на
            # её конец. Уведомлять владельца тут не о чем, он получил бы такое
            # сообщение два десятка раз в день.
            _rearm_telegram(v.wait_seconds + 30)
        return {"blocked": v.reason}
    try:
        from .outreach.sender import MORE_TO_SEND, ProcessLock, lock_path
        # В демоне — по одной сессии за вызов: межсессионные паузы держит
        # планировщик, и однопоточная tg-очередь между сессиями свободна
        # для кнопочных решений и чтения входящих. Прежний сплошной прогон
        # занимал её на 3-7 часов. Вне демона (--once) — как раньше, целиком.
        sessions = 1 if _SCHED.get("sched") is not None else None
        # CLI и daemon должны пользоваться одним и тем же lock-файлом рядом
        # с БД: иначе второй ручной запуск мог открыть ту же Telethon-сессию.
        with ProcessLock(lock_path()):
            rc = asyncio.run(sender_run(s.daily_cold_limit, dry=False,
                                        max_sessions=sessions))
        if rc == MORE_TO_SEND and _SCHED.get("sched") is not None:
            _rearm_telegram(policy.gap_seconds())
        else:
            from .observability import record
            record("sender:telegram", "ok" if rc == 0 else "blocked",
                   details={"return_code": rc})
    except Exception as e:
        log.error("telegram: %s: %s", type(e).__name__, str(e)[:120])
        from .observability import record
        record("sender:telegram", "error", error=type(e).__name__)
        raise
    return {"ok": True} if rc in (0, MORE_TO_SEND) else {"blocked": "код %d" % rc}


def step_discover() -> dict:
    """Автопоиск новых каналов с вакансиями.

    Раз в сутки и с малым лимитом: поиск по Telegram — дорогая операция,
    а каналы не появляются пачками каждый час. Найденное включается в сбор
    сразу: фильтр качества уже отсеял мёртвое и не относящееся к вакансиям.
    """
    from .ingest.discover import run as discover_run
    s = get_settings()
    if not s.discover_enabled:
        return {}
    try:
        stats = asyncio.run(discover_run(apply=True))
    except Exception as e:
        log.error("discover: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}
    if stats.get("error"):
        log.warning("discover: %s", stats["error"])
    elif stats.get("checked"):
        log.info("автопоиск каналов: проверено %d, годных %d, включено %d",
                 stats["checked"], stats["good"], stats.get("added", 0))
    return stats


def step_inbox() -> dict:
    """Входящие: автоответы на рутину, карточки владельцу, его команды."""
    from .convo.engine import run as inbox_run
    s = get_settings()
    if not (s.tg_api_id and s.telegram_api_hash):
        return {}
    from pathlib import Path
    if not Path(s.telegram_session_path).exists():
        return {}
    try:
        stats = asyncio.run(inbox_run(dry=False))
    except Exception as e:
        log.error("inbox: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}
    if stats.get("error"):
        log.warning("inbox: %s", stats["error"])
    elif any(stats.get(k) for k in ("incoming", "cards", "commands", "expired")):
        log.info("inbox: входящих %d, автоответов %d, эскалаций %d, "
                 "карточек %d, команд %d, просрочено %d",
                 stats.get("incoming", 0), stats.get("auto", 0),
                 stats.get("escalated", 0), stats.get("cards", 0),
                 stats.get("commands", 0), stats.get("expired", 0))
    return stats


def step_inbox_email() -> dict:
    """Входящие письма: IMAP → привязка к заявкам → автоответы и карточки."""
    from .convo import gmailapi
    from .convo.inbox_email import run as mail_run
    s = get_settings()
    if not (s.imap_enabled and gmailapi.reading_configured()):
        return {}
    try:
        stats = asyncio.run(mail_run(dry=False))
    except Exception as e:
        log.error("почта: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}
    if stats.get("error"):
        log.warning("почта: %s", stats["error"])
    elif stats.get("seen"):
        log.info("почта: просмотрено %d, тел скачано %d, привязано %d (%s), "
                 "пропущено %d, автоответов %d, эскалаций %d",
                 stats["seen"], stats["bodies"], stats["matched"],
                 ", ".join("%s=%d" % kv for kv in stats["by_rule"].items())
                 or "—",
                 stats["skipped"], stats["auto"], stats["escalated"])
    return stats


def step_retry_drafts() -> dict:
    """Повторяет черновики карточек, пока у владельца ещё есть время."""
    from .owner import retry_missing_drafts
    try:
        stats = retry_missing_drafts(limit=5)
    except Exception as e:
        log.error("повтор черновиков: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}
    if stats.get("ready"):
        log.info("повтор черновиков: проверено %d, готово %d",
                 stats.get("checked", 0), stats["ready"])
    return stats


def step_recheck() -> dict:
    """Повторная классификация входящих, по которым LLM промолчала."""
    from .convo.recheck import run as recheck_run
    try:
        st = recheck_run(limit=20)
    except Exception as e:
        log.error("повтор классификации: %s: %s", type(e).__name__, str(e)[:140])
        return {"error": type(e).__name__}
    if st.get("due"):
        log.info("повтор классификации: ждали %d, прояснилось %d",
                 st["due"], st.get("resolved", 0))
    return st


def step_revive() -> dict:
    """Возврат в оборот диалогов, застрявших в NEEDS_HUMAN."""
    from .convo.revive import run as revive_run
    try:
        st = revive_run(limit=10)
    except Exception as e:
        log.error("оживление диалогов: %s: %s", type(e).__name__, str(e)[:140])
        return {"error": type(e).__name__}
    if st.get("revived"):
        log.info("оживление: разобрано %d из %d застрявших",
                 st["revived"], st.get("stale", 0))
        from . import notify
        notify.push("info",
                    "♻️ Вернул в переписку %d заглохших диалогов — "
                    "рекрутёры получат ответ." % st["revived"],
                    dedup="revive:%s" % datetime.now(timezone.utc)
                                                .strftime("%Y-%m-%d-%H"))
    return st


def step_manual_prepare() -> dict:
    """Оценка и резюме для ручной очереди — перед утренней пачкой.

    rescore_all и build_cvs существовали с самого начала, но вызывались
    только руками из CLI: 733 из 848 ATS-вакансий стояли со score=0 и не
    попадали в пачку вовсе, а резюме было готово у 23 из 389. Пачка без
    этого шага показывает случайные вакансии вместо лучших.
    """
    from .manual_apply import build_cvs, rescore_all, sweep_unreachable
    from .repair_queue import reset_email_language
    try:
        # Сначала повторно ищем контакты в уже сохранённом тексте. Если
        # закрыть HANDLE_MISSING раньше, найденная строка уже не попадёт в
        # recontact и второй проход станет бесполезным.
        from .ingest.recontact import run as recontact_run
        contacts = recontact_run(dry=False)
        if contacts.get("restored"):
            log.info("ручная очередь: возвращено контактов из текста %d",
                     contacts["restored"])

        # Старые APPROVED без отправки пересобираются на языке вакансии.
        lang_fix = reset_email_language(dry=False)
        if lang_fix.get("сброшено"):
            log.info("ручная очередь: сброшено писем из-за языка %d",
                     lang_fix["сброшено"])

        # Одобренное, которое уже никогда не уйдёт, закрываем с причиной: иначе счётчик
        # «одобрено» в боте обещает очередь, которой нет.
        try:
            from .repair_queue import sweep_dead_approved
            dead = sweep_dead_approved()
            if dead:
                log.info("одобренные без шансов на отправку закрыты: %s", dead)
        except Exception as e:                                  # noqa: BLE001
            log.warning("уборка одобренных: %s: %s", type(e).__name__, str(e)[:120])

        # Уборка до скоринга: незачем оценивать то, по чему нельзя
        # откликнуться.
        swept = sweep_unreachable(apply=True)
        if swept.get("closed"):
            log.info("ручная очередь: закрыто без канала связи %d",
                     swept["closed"])
        st = rescore_all(verbose=False)
        made = build_cvs(top=40, verbose=False)
    except Exception as e:
        log.error("подготовка ручной очереди: %s: %s",
                  type(e).__name__, str(e)[:140])
        return {"error": type(e).__name__}
    log.info("ручная очередь: оценено %d, годных %d, резюме готово %d",
             st.get("scored", 0), st.get("relevant", 0), len(made))

    # Анкеты для лучших вакансий: схема формы публична только у Greenhouse
    # и частично у Ashby, поэтому сборка идёт по тем, что доступны.
    try:
        from .apply.packet import prepare_batch
        pk = prepare_batch()
        if pk.get("built"):
            log.info("анкеты подготовлены: %d", pk["built"])
    except Exception as e:
        log.warning("подготовка анкет: %s: %s", type(e).__name__, str(e)[:120])
        pk = {}
    return {"scored": st.get("scored", 0), "cvs": len(made),
            "packets": pk.get("built", 0),
            "recontacted": contacts.get("restored", 0),
            "email_language_reset": lang_fix.get("сброшено", 0)}


def step_manual_batch() -> dict:
    """Утренняя пачка ручных откликов в бот."""
    from .manual_batch import run as batch_run
    try:
        return batch_run()
    except Exception as e:
        log.error("ручные отклики: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}


def step_interview_reminders() -> dict:
    """Напоминания о встречах в бот: за сутки, за час и «как прошло».

    Дублирует напоминания Google Calendar намеренно: они приходят туда, где
    их легко пропустить, а бот — то место, куда владелец и так смотрит по
    этой системе, и там же лежит контекст встречи.
    """
    from .schedule.remind import run as remind_run
    # Карточки без решения — здесь же: этот шаг не зависит от Telegram, а истечение
    # карточек жило внутри шага входящих и вставало вместе с ним.
    cards = 0
    try:
        from . import owner
        cards = owner.remind_pending()
        owner.expire_stale()
    except Exception as e:
        log.error("напоминания о карточках: %s: %s", type(e).__name__, str(e)[:120])
    try:
        return dict(remind_run() or {}, card_reminders=cards)
    except Exception as e:
        log.error("напоминания: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}


def step_decisions() -> dict:
    """Решения владельца из бота → реальные действия по MTProto.

    Бот только помечает карточку решённой: писать рекрутёру он не может,
    сессия Telethon принадлежит этому процессу. Раз в 5 минут, потому что
    подтверждение слота — самое чувствительное ко времени действие: рекрутёр
    может отдать слот другому кандидату.
    """
    from . import decisions
    s = get_settings()
    if not (s.tg_api_id and s.telegram_api_hash):
        return {}
    from pathlib import Path
    if not Path(s.telegram_session_path).exists():
        return {}
    try:
        stats = asyncio.run(decisions.run(limit=10))
    except Exception as e:
        log.error("решения: %s: %s", type(e).__name__, str(e)[:120])
        return {"error": type(e).__name__}
    if stats.get("taken"):
        log.info("решения владельца: взято %d, исполнено %d, ошибок %d",
                 stats["taken"], stats["done"], stats["failed"])
    return stats


def step_gcal_sync() -> None:
    """Догоняет Google Calendar по интервью, подтверждённым офлайн."""
    try:
        from .schedule.book import sync_calendar
        r = sync_calendar()
        if r.get("synced"):
            log.info("gcal: досоздано событий %d", r["synced"])
    except Exception as e:
        log.error("gcal: %s: %s", type(e).__name__, str(e)[:120])


def step_followups() -> dict:
    from .outreach.followup import prepare
    stats = prepare(dry=False)
    if stats["prepared"] or stats["closed"]:
        log.info("напоминания: подготовлено %d, закрыто без ответа %d",
                 stats["prepared"], stats["closed"])
    return stats


def step_calendar() -> None:
    from .schedule.ics import build
    n, _, _ = build()
    if n:
        log.info("календарь: %d интервью", n)


def step_google_token() -> dict:
    """Жив ли вход в Google. Мёртвый токен — это молча не работающие календарь и почта.

    20.09 выяснилось, что токен от 26.08 умер с invalid_grant (проект Google Cloud в статусе
    «Тестирование» — токены живут 7 дней), и три недели об этом никто не знал.
    """
    from . import googleauth, notify
    state = googleauth.check()
    if state["token"] and not state["alive"]:
        notify.push_once(
            "google_token_dead",
            "⚠️ Вход в Google перестал работать: календарь и почта через Gmail API стоят.\n"
            "Нужен повторный вход: python -m jobhunter.googleauth --login\n"
            "Если это повторяется раз в неделю — проект Google Cloud в статусе «Тестирование», "
            "переведи его в «В работе».",
            dedup="google_token_dead:%s" % datetime.now().strftime("%Y-%m-%d"))
    if state["token"] and state["alive"] and not state["writable"]:
        # 21.09: файл токена остался у root после docker cp, бот (пользователь app) не мог записать
        # обновлённый токен — и почта тихо ушла на SMTP/IMAP. Теперь код работает и так, но каждое
        # подключение ходит за новым токеном; правильно — вернуть права.
        notify.push_once(
            "google_token_readonly",
            "⚠️ Токен Google не перезаписывается (нет прав на файл). Почта работает, но каждый раз "
            "запрашивает новый токен.\nИсправление: docker compose exec -u root autopilot "
            "chown app:app /data/google_token.json",
            dedup="google_token_readonly:%s" % datetime.now().strftime("%Y-%m-%d"))
    return {k: state[k] for k in ("token", "alive", "writable", "calendar", "gmail_send", "gmail_read")}


BACKUP_KEEP_DAILY = 7
BACKUP_KEEP_WEEKLY = 4
_BACKUP_NAME = "jobhunter-%s.tar.gz"


def backup_dir() -> Path:
    """Папка копий — на диске хоста (./out смонтирован в /out), а не в томе с самой базой."""
    return Path(get_settings().out_dir) / "backups"


def prune_backups(folder: Path, today: datetime | None = None) -> list:
    """Оставить 7 последних ежедневных копий и 4 воскресные. Возвращает удалённые имена.

    Трогаем только файлы своего образца: в папке могут лежать копии, сделанные руками.
    """
    import re
    pattern = re.compile(r"^jobhunter-(\d{4}-\d{2}-\d{2})\.tar\.gz$")
    dated = []
    for path in folder.glob("jobhunter-*.tar.gz"):
        m = pattern.match(path.name)
        if m:
            dated.append((datetime.strptime(m.group(1), "%Y-%m-%d"), path))
    dated.sort(reverse=True)
    keep = {p for _, p in dated[:BACKUP_KEEP_DAILY]}
    keep |= {p for d, p in [x for x in dated if x[0].weekday() == 6][:BACKUP_KEEP_WEEKLY]}
    removed = []
    for _, path in dated:
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def step_backup() -> dict:
    """Ежедневная копия базы и сессий с проверкой восстановления.

    Проверка 20.09: модуль backup.py существовал, но автопилот его ни разу не вызывал —
    21 тысяча вакансий, вся переписка и сессия Telegram жили в одном томе Docker без единой
    копии. create_archive снимает SQLite штатным backup API и сам же распаковывает архив во
    временную папку с integrity_check: копия, которую нельзя восстановить, не публикуется.
    """
    from . import notify
    from .backup import create_archive
    folder = backup_dir()
    target = folder / (_BACKUP_NAME % datetime.now().strftime("%Y-%m-%d"))
    if target.exists():
        return {"skipped": "сегодняшняя копия уже есть", "archive": target.name}
    try:
        result = create_archive(Path(get_settings().db_path).parent, target)
    except Exception as e:
        log.error("резервная копия: %s: %s", type(e).__name__, str(e)[:160])
        notify.push_once("backup_failed",
                         "⚠️ Резервная копия базы не создана: %s\n%s" % (type(e).__name__, str(e)[:300]),
                         dedup="backup_failed:%s" % datetime.now().strftime("%Y-%m-%d"))
        return {"error": type(e).__name__}
    removed = prune_backups(folder)
    log.info("резервная копия: %s, %.1f МБ, заявок %s, удалено старых %d", result["archive"],
             result["bytes"] / 1e6, result.get("applications"), len(removed))
    return {"archive": result["archive"], "mb": round(result["bytes"] / 1e6, 1),
            "applications": result.get("applications"), "removed": len(removed)}


def step_db_maintenance() -> None:
    """Ночное обслуживание SQLite.

    С тремя процессами (автопилот, бот, дашборд) WAL растёт быстрее, чем
    успевают автоматические контрольные точки: любой читатель мешает
    checkpoint'у. Раз в сутки сводим журнал принудительно и обновляем
    статистику планировщика запросов.
    """
    # Заявки, зависшие в SENDING после сбоя, возвращаются в очередь и ночью
    # тоже: отправщик делает это в начале прогона, но прогона могло не быть.
    try:
        from .outreach.sender import reclaim_stale_sending
        reclaim_stale_sending()
    except Exception as e:
        log.error("возврат SENDING: %s", str(e)[:120])

    # Доставленные уведомления старше месяца — чистим: bot_outbox иначе не
    # чистился нигде и рос вечно (аудит недавних доставок при этом остаётся).
    try:
        from .models import BotOutbox
        edge = utcnow() - timedelta(days=30)
        with session_scope() as sess:
            n = sess.query(BotOutbox).filter(
                BotOutbox.sent_at.is_not(None),
                BotOutbox.sent_at < edge).delete(synchronize_session=False)
        if n:
            log.info("bot_outbox: удалено доставленных старше 30 дней: %d", n)
    except Exception as e:
        log.error("чистка bot_outbox: %s", str(e)[:120])

    import sqlite3
    s = get_settings()
    try:
        conn = sqlite3.connect(s.db_path, timeout=60)
        busy, log_pages, moved = conn.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.execute("PRAGMA optimize")
        conn.close()
        log.info("обслуживание БД: checkpoint busy=%s страниц=%s сведено=%s",
                 busy, log_pages, moved)
    except Exception as e:
        log.warning("обслуживание БД: %s: %s", type(e).__name__, str(e)[:100])


def step_daily_summary() -> None:
    from collections import Counter
    with session_scope() as sess:
        apps = sess.scalars(select(Application)).all()
        c = Counter(a.status for a in apps)
        q = policy.get_quota(sess)
        st = policy.get_state(sess)
        policy.close_day(sess)
    log.info("ИТОГ ДНЯ: отправлено %d (пауза %d мин) | ждут ответа %d | ответили %d | "
             "в очереди %d | чистых дней подряд %d",
             q.sent_count, policy.COLD_GAP_MINUTES,
             c.get(Status.AWAITING_REPLY.value, 0),
             c.get(Status.REPLIED.value, 0) + c.get(Status.IN_DIALOGUE.value, 0),
             c.get(Status.PENDING_APPROVAL.value, 0),
             st.consecutive_clean_days)
    from .report import intents_daily
    intents = intents_daily(days=1)
    day_counts = next(iter(intents["days"].values()), {})
    if day_counts:
        log.info("интенты за день: %s | поправок LLM: %d",
                 ", ".join("%s=%d" % kv for kv in sorted(day_counts.items())),
                 intents["llm_corrections"])

    from . import notify
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notify.push("daily_summary",
                "📊 Итог дня\n"
                "отправлено: %d (пауза между холодными %d мин)\n"
                "ждут ответа: %d · ответили: %d\n"
                "в очереди: %d · интервью: %d\n"
                "чистых дней подряд: %d"
                % (q.sent_count, policy.COLD_GAP_MINUTES,
                   c.get(Status.AWAITING_REPLY.value, 0),
                   c.get(Status.REPLIED.value, 0)
                   + c.get(Status.IN_DIALOGUE.value, 0),
                   c.get(Status.PENDING_APPROVAL.value, 0),
                   c.get(Status.INTERVIEW_CONFIRMED.value, 0),
                   st.consecutive_clean_days),
                dedup="summary:%s" % day)


# ────────────────────────────────────────────────────────── циклы ──

def cycle_once() -> None:
    """Полный проход: собрать → подготовить → одобрить → отправить."""
    log.info("=" * 60)
    log.info("цикл автопилота")
    step_discover()
    step_ingest()
    step_prepare()
    step_auto_approve()
    step_send_email()
    step_send_telegram()
    step_inbox()
    step_inbox_email()
    step_recheck()
    step_revive()
    step_decisions()
    step_interview_reminders()
    step_manual_prepare()
    step_manual_batch()
    step_followups()
    step_calendar()
    step_gcal_sync()
    step_daily_summary()
    log.info("цикл завершён")


def step_ats_verify() -> None:
    """Еженедельная проверка найденных ATS-досок + уведомление владельцу."""
    from .ingest.ats_discover import verify
    try:
        stats = verify(limit=10)
    except Exception as e:
        log.error("ats-verify: %s", str(e)[:120])
        return
    log.info("ats-verify: %s", stats)
    if stats.get("passed"):
        from . import notify
        notify.push(
            "info",
            "🧭 Найдены живые ATS-доски с профильными вакансиями: %d.\n"
            "Включить в сбор: docker compose exec autopilot "
            "python -m jobhunter.ingest.ats_discover --apply" % stats["passed"],
            dedup="ats_passed:%s" % datetime.now(timezone.utc)
                                          .strftime("%Y-%m-%d"))


def step_mail_digest() -> None:
    """Утренняя сводка почты в бот: «что в ящике, какие дела».

    Только заголовки, ящик readonly — см. convo/mail_digest.
    """
    from .convo import gmailapi
    if not gmailapi.reading_configured():
        return
    try:
        from .convo.mail_digest import run as digest_run
        text = digest_run(hours=24)
    except Exception as e:
        log.error("почтовый дайджест: %s: %s", type(e).__name__, str(e)[:120])
        return
    from . import notify
    notify.push("mail_digest", text,
                dedup="maildig:%s" % datetime.now(timezone.utc)
                                            .strftime("%Y-%m-%d"))
    log.info("почтовый дайджест отправлен в бот")


def send_after_ingest(steps: tuple = ()) -> dict:
    """Довести только что собранное до отправки: подготовка → одобрение → почта.

    Зовёт сами функции шагов, а не задания планировщика: дневные задания обёрнуты
    защитой «сегодня уже выполнялось» и молча возвращают None. Первая версия (19.09)
    звала именно их — вечерний сбор принёс 373 вакансии, цепочка «отработала» за
    миллисекунды и не сделала ничего, без единой строки в логе.
    """
    from .observability import record
    steps = steps or (("prepare", step_prepare), ("approve", step_auto_approve),
                      ("email", step_send_email))
    out: dict = {}
    for name, fn in steps:
        try:
            res = fn()
            out[name] = res if isinstance(res, dict) else {"result": res}
        except Exception as e:                                  # noqa: BLE001
            log.error("после сбора: %s: %s: %s", name, type(e).__name__, str(e)[:120])
            out[name] = {"error": type(e).__name__}
    log.info("после сбора: %s", str(out)[:300])
    record("task:after_ingest", "error" if any("error" in v for v in out.values()) else "ok",
           details=out)
    return out


def disabled_sources() -> set:
    """Источники, выключенные владельцем или проверкой (config.disabled_sources)."""
    return {x.strip().lower() for x in (get_settings().disabled_sources or "").split(",") if x.strip()}


# Одна сессия отправки легально держит телеграм-пул до ~25 минут, healthcheck ждёт 40.
# Час без пульса — это уже не работа, а зависший вызов.
TG_WEDGE_SECONDS = 3600


def wedged(tg_pulse_age: float) -> bool:
    """Телеграм-очередь зависла. inf (пульса не было вовсе) зависанием не считаем —
    иначе неверно настроенная папка пульса уводила бы процесс в вечный перезапуск."""
    return TG_WEDGE_SECONDS < tg_pulse_age < float("inf")


def _watchdog() -> None:
    """Завершить зависший процесс, чтобы Docker поднял его заново.

    19.09 на 2,5 часа пропала сеть; вызов Telethon завис без таймаута, шаги
    «входящие» и «решения» встали навсегда, цепочка догона — за ними. Контейнер
    стал unhealthy, но Docker такие не перезапускает: автопилот простоял три часа
    после возвращения сети, пока его не перезапустили руками. При restart:
    unless-stopped выход из процесса = перезапуск, а догон на старте возвращает день.
    """
    age = health.age("autopilot_tg")
    if wedged(age):
        log.critical("телеграм-очередь молчит %.0f мин — выхожу, Docker перезапустит", age / 60)
        health.ping_external("/fail")
        logging.shutdown()
        os._exit(1)
    health.ping_external()          # внешний сторож: «жив»; без отметок он поднимет тревогу сам


def run_daemon() -> int:
    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.blocking import BlockingScheduler

    # Все шаги, открывающие Telethon, идут через пул из ОДНОГО воркера.
    # max_instances=1 защищает задание только от самого себя, а пул по
    # умолчанию (10 потоков) позволяет discover, tg1/tg2 и inbox пересечься
    # во времени. Два MTProto-клиента на одном jobhunter.session — это
    # AuthKeyDuplicatedError и разлогин аккаунта, то есть потеря доступа ко
    # всей переписке с рекрутёрами до повторного tg_login.py.
    sched = BlockingScheduler(
        timezone=get_settings().owner_tz,
        executors={"default": ThreadPoolExecutor(5),
                   "tg": ThreadPoolExecutor(1),
                   # Пульс живёт отдельно: полный догон занимает все пять
                   # default-потоков, и healthcheck ловил ложный unhealthy.
                   "beat": ThreadPoolExecutor(1)})
    _SCHED["sched"] = sched
    opts = dict(max_instances=1, coalesce=True, misfire_grace_time=1800)
    tg = dict(opts, executor="tg")

    # Автопоиск каналов — до сбора: найденное сегодня же попадёт в ленту.
    sched.add_job(step_discover, "cron", hour=9, minute=0, id="discover", **tg)
    sched.add_job(step_ingest, "cron", hour=9, minute=30, id="ingest", **opts)
    sched.add_job(step_prepare, "cron", hour=10, minute=0, id="prepare", **opts)
    sched.add_job(step_auto_approve, "cron", hour=10, minute=15, id="approve", **opts)
    sched.add_job(step_send_email, "cron", hour=10, minute=30, id="email", **opts)
    # Второй проход: 18.09 сбор затянулся до 11:06, почта в 10:30 сработала раньше
    # одобрения, и одобренное в 11:09 ждало бы сутки. Отправщик берёт только ещё не
    # отправленное и в пределах дневного потолка — повторов не бывает.
    sched.add_job(step_send_email, "cron", hour=13, minute=0, id="email_late", **opts)
    sched.add_job(step_resend_en, "cron", hour=11, minute=0, id="resend_en", **opts)
    sched.add_job(step_direct, "cron", hour=9, minute=50, id="direct", **opts)
    # Анкеты Ashby: stage вечером готовит кандидатов к утреннему prepare,
    # подача — после авто-одобрения. Свой дневной лимит (ATS_DAILY_LIMIT),
    # телеграмную квоту не трогает.
    sched.add_job(step_submit_ashby, "cron", hour=10, minute=45,
                  id="ashby", **opts)
    sched.add_job(step_stage_ashby, "cron", hour=20, minute=30,
                  id="ashby_stage", **opts)
    # Telegram — двумя окнами, чтобы дневная квота расходилась по времени
    sched.add_job(step_send_telegram, "cron", hour=11, minute=15, id="tg1", **tg)
    sched.add_job(step_send_telegram, "cron", hour=16, minute=40, id="tg2", **tg)
    # Сбор вакансий не должен ждать следующего утра. Тот же однопоточный
    # tg-пул не допускает пересечения с Telethon-сессией отправки.
    def _ingest_tg_then_send():
        """Дневной и вечерний сбор сразу доводится до отправки.

        Раньше подготовка, одобрение и почта шли раз в день утром: собранное в 13:30 и
        18:30 ждало 10:00 следующего дня и старело на сутки при допустимом возрасте
        вакансии в семь дней (проверка 19.09). Цепочка уходит в общий пул — телеграм-пул
        из одного потока нельзя занимать рендером резюме. Отправщик берёт только ещё не
        отправленное в пределах дневного потолка, повторов не бывает.
        """
        stats = step_ingest_telegram()
        if isinstance(stats, dict) and stats.get("new"):
            sched.add_job(send_after_ingest, "date", id="after_tg_ingest",
                          replace_existing=True,
                          run_date=datetime.now() + timedelta(seconds=20), **opts)
        return stats

    sched.add_job(_ingest_tg_then_send, "cron", hour=13, minute=30,
                  id="ingest_tg_midday", **tg)
    sched.add_job(_ingest_tg_then_send, "cron", hour=18, minute=30,
                  id="ingest_tg_evening", **tg)
    # Входящие — каждые 20 минут днём: рекрутёру, назвавшему время, нельзя
    # отвечать на следующий день, а чаще — лишний трафик по MTProto.
    sched.add_job(step_inbox, "cron", hour="9-21", minute="*/20",
                  id="inbox", **tg)
    # Почта — на ОБЫЧНОМ пуле, а не на однопоточном tg: IMAP не трогает
    # сессию Telethon, и выстраивать его в очередь за рассылкой незачем.
    # Минуты смещены относительно телеграмного inbox (:00/:20/:40), чтобы
    # два писателя реже встречались на одной SQLite.
    sched.add_job(step_inbox_email, "cron", hour="8-22", minute="5,25,45",
                  id="inbox_mail", **opts)
    # LLM может временно вернуть 429/ошибку. Не заставляем владельца писать
    # технический ответ руками, если карточка ещё не истекла.
    sched.add_job(step_retry_drafts, "cron", hour="8-23", minute="*/20",
                  id="retry_drafts", **opts)
    # Решения из бота — часто и дёшево: обычно это пустой SELECT, но когда
    # владелец нажал кнопку, ждать 20 минут до следующего inbox нельзя.
    sched.add_job(step_decisions, "cron", hour="8-23", minute="*/5",
                  id="decisions", **tg)
    # Напоминания о встречах — каждые полчаса: попасть точно в минуту нельзя,
    # а окна касаний взяты с запасом.
    # Пачка ручных откликов — утром, до рабочего дня: их разбирают руками,
    # и лучше это делать на свежую голову, чем вечером.
    sched.add_job(step_manual_prepare, "cron", hour=9, minute=40,
                  id="manual_prep", **opts)
    # Оба шага — на обычном пуле: сети Telethon они не трогают, а очередь
    # tg занята многочасовой отправкой. Минуты выбраны между окнами inbox
    # (*/20) и inbox_mail (5,25,45), чтобы писатели реже встречались на
    # одной SQLite.
    sched.add_job(step_recheck, "cron", hour="9-21", minute="12,42",
                  id="recheck", **opts)
    sched.add_job(step_revive, "cron", hour="10,15,19", minute=7,
                  id="revive", **opts)
    sched.add_job(step_manual_batch, "cron", hour=9, minute=45,
                  id="manual_batch", **opts)
    sched.add_job(step_interview_reminders, "cron", minute="*/30",
                  id="reminders", **opts)
    sched.add_job(step_followups, "cron", hour=12, minute=0, id="followups", **opts)
    sched.add_job(step_calendar, "cron", hour="*/4", id="calendar", **opts)
    sched.add_job(step_google_token, "cron", hour="*/4", minute=20, id="google_token", **opts)
    sched.add_job(step_gcal_sync, "cron", hour="*/4", minute=10,
                  id="gcal", **opts)
    sched.add_job(step_daily_summary, "cron", hour=20, minute=0, id="summary", **opts)
    # Пульс для healthcheck контейнера: живой, но зависший планировщик
    # перестанет отмечаться, и Docker это увидит.
    sched.add_job(lambda: health.beat("autopilot"), "interval", minutes=1,
                  id="beat", max_instances=1, coalesce=True, executor="beat")
    # Сторож живёт в пуле пульса: тот свободен, даже когда остальные пулы стоят.
    sched.add_job(_watchdog, "interval", minutes=5, id="watchdog",
                  max_instances=1, coalesce=True, executor="beat")
    # Пульс из tg-пула: основной beat не видит зависший Telethon-вызов —
    # default-поток отбивается, а вся tg-очередь мертва. Порог свободный
    # (40 мин): одна сессия отправки легально держит пул до ~25 минут.
    sched.add_job(lambda: health.beat("autopilot_tg"), "interval", minutes=5,
                  id="beat_tg", max_instances=1, coalesce=True,
                  misfire_grace_time=300, executor="tg")
    sched.add_job(step_db_maintenance, "cron", hour=3, minute=30,
                  id="dbmaint", **opts)
    # Копия базы — в 08:45, перед дневным конвейером, и в догон: ночью домашний ПК выключен.
    sched.add_job(step_backup, "cron", hour=8, minute=45, id="backup", **opts)
    # Проверка ATS-кандидатов — раз в неделю: каждая проверка это живой
    # запрос к доске. Включение прошедших остаётся за владельцем (--apply).
    sched.add_job(step_ats_verify, "cron", day_of_week="sun", hour=12,
                  minute=30, id="ats_verify", **opts)
    # Сводка почты — утром, до начала рабочего дня владельца.
    sched.add_job(step_mail_digest, "cron", hour=9, minute=10,
                  id="mail_digest", **opts)

    # ── догон пропущенного дня ─────────────────────────────────────────
    # Джобы живут в памяти с misfire_grace_time=1800: если ПК спал в
    # 09:00-11:15 (обычное утро домашней машины), весь дневной конвейер —
    # сбор, подготовка, одобрение, обе отправки — молча пропадал до завтра.
    # Каждый дневной шаг оставляет маркер прогона; страж раз в 5 минут
    # замечает разрыв монотонных часов (= машина спала) и на старте демона
    # проверяет маркеры: плановое время прошло, прогона не было — шаг
    # ставится на ближайшую минуту, в исходном порядке конвейера.
    daily = [("backup", 8, 45), ("discover", 9, 0), ("ingest", 9, 30), ("prepare", 10, 0),
             ("approve", 10, 15), ("direct", 9, 50), ("email", 10, 30), ("resend_en", 11, 0),
             ("tg1", 11, 15),
             ("manual_prep", 9, 40), ("manual_batch", 9, 45),
             ("followups", 12, 0), ("tg2", 16, 40)]

    def _mark_path(job_id):
        return Path(get_settings().heartbeat_dir) / ("last_%s.txt" % job_id)

    def _ran_today(job_id):
        try:
            return _mark_path(job_id).read_text().strip() == \
                datetime.now().strftime("%Y-%m-%d")
        except OSError:
            return False

    def _wrap_marked(fn, job_id):
        from .scheduled import wrap_marked
        return wrap_marked(fn, job_id, Path(get_settings().heartbeat_dir))

    for job_id, *_ in daily:
        j = sched.get_job(job_id)
        j.modify(func=_wrap_marked(j.func, job_id))

    def _chain(job_ids, then=None):
        """Последовательное выполнение шагов — конвейер, а не салют.

        Прежний догон ставил каждый шаг отдельным заданием со сдвигом в две
        минуты: email стартовал, когда ingest ещё качал вакансии, и утренние
        письма уходили по вчерашней очереди. Цепочка держит порядок; хвост
        может запланировать следующую цепочку (tg — после default).
        """
        def run():
            for job_id in job_ids:
                j = sched.get_job(job_id)
                if j is None:
                    continue
                try:
                    j.func()
                except Exception as e:
                    log.error("догон %s: %s: %s", job_id,
                              type(e).__name__, str(e)[:120])
            if then:
                then()
        return run

    def catch_up():
        now = datetime.now()
        missed_default, missed_tg = [], []
        for job_id, hh, mm in daily:
            planned = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if planned >= now - timedelta(seconds=60) or _ran_today(job_id):
                continue
            j = sched.get_job(job_id)
            # Штатный крон сработает в ближайшие 20 минут — не дублируем.
            nrt = getattr(j, "next_run_time", None)
            if nrt is not None and nrt - now.astimezone(nrt.tzinfo) \
                    < timedelta(minutes=20):
                continue
            (missed_tg if job_id in ("discover", "tg1", "tg2")
             else missed_default).append(job_id)
            log.info("догоняю пропущенный шаг %s (план был %02d:%02d)",
                     job_id, hh, mm)
        missed_default = email_after_approve(missed_default)

        def start_tg():
            if missed_tg:
                sched.add_job(_chain(missed_tg), "date",
                              id="catchup_chain_tg", replace_existing=True,
                              run_date=datetime.now() + timedelta(seconds=30),
                              misfire_grace_time=3600, executor="tg")

        if missed_default:
            sched.add_job(_chain(missed_default, then=start_tg), "date",
                          id="catchup_chain", replace_existing=True,
                          run_date=now + timedelta(minutes=1),
                          misfire_grace_time=3600)
        else:
            start_tg()

    _last_tick = {"t": time.monotonic()}

    def sleep_guard():
        gap = time.monotonic() - _last_tick["t"]
        _last_tick["t"] = time.monotonic()
        if gap > 15 * 60:
            log.info("обнаружен сон машины (%.0f мин) — проверяю пропуски",
                     gap / 60)
            catch_up()

    sched.add_job(sleep_guard, "interval", minutes=5, id="sleep_guard",
                  max_instances=1, coalesce=True)
    sched.add_job(catch_up, "date",
                  run_date=datetime.now() + timedelta(seconds=30),
                  id="catchup_boot", misfire_grace_time=3600)

    def publish_schedule():
        from .observability import record
        for job_id in ("tg1", "tg2", "tg_more", "email"):
            job = sched.get_job(job_id)
            at = getattr(job, "next_run_time", None) if job else None
            record("schedule:" + job_id, "scheduled", next_run_at=at)

    # Продолжение партии сохраняется отдельно от APScheduler MemoryJobStore.
    from .models import RuntimeState
    with session_scope() as sess:
        pending = sess.get(RuntimeState, "sender:telegram")
        resume_at = pending.next_run_at if pending and pending.status == "partial" else None
    if resume_at:
        when = max(resume_at.replace(tzinfo=timezone.utc),
                   datetime.now(timezone.utc) + timedelta(minutes=1))
        sched.add_job(step_send_telegram, "date", id="tg_more", replace_existing=True,
                      run_date=when, misfire_grace_time=3600, executor="tg")
    sched.add_job(publish_schedule, "interval", seconds=30, id="schedule_state",
                  max_instances=1, coalesce=True, executor="beat")

    health.beat("autopilot")
    health.beat("autopilot_tg")     # первый пульс до старта interval-джоба
    log.info("Автопилот запущен. Расписание:")
    for j in sched.get_jobs():
        log.info("  %-10s %s", j.id, j.trigger)
    log.info("Стоп-кран: создать файл %s", get_settings().kill_switch)
    log.info("Остановить автопилот: Ctrl+C")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("автопилот остановлен")
    return 0


def main() -> int:
    _setup_logging()
    ap = argparse.ArgumentParser(description="Автопилот jobhunter")
    ap.add_argument("--once", action="store_true", help="один прогон и выход")
    ap.add_argument("--no-send", action="store_true",
                    help="собрать и подготовить, но не отправлять")
    args = ap.parse_args()

    if args.once:
        if args.no_send:
            step_ingest()
            step_prepare()
            step_auto_approve()
            step_daily_summary()
        else:
            cycle_once()
        return 0
    return run_daemon()


if __name__ == "__main__":
    sys.exit(main())
