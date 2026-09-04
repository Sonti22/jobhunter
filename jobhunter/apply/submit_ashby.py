# -*- coding: utf-8 -*-
"""Автоподача отклика в Ashby через документированный Posting API.

Ashby — единственная из четырёх ATS, где подача не требует ключа
работодателя: публичный Posting API принимает отклик тем же эндпоинтом,
что и собственная форма доски (multipart/form-data, поле applicationForm
с JSON {"fieldSubmissions": [...]} плюс файл резюме). У остальных трёх
подаёт владелец — см. докстринг forms.py.

Правила те же, что во всём конвейере, — fail-closed и ни одной догадки:
  - значения полей только из profile.yaml (identity) и готовых артефактов
    заявки (письмо, прошедшее анти-фабрикация гейт, и резюме);
  - любое обязательное поле, которое нечем заполнить фактами (кастомные
    вопросы, селекты, EEOC/veteran/citizenship-опросники, файлы кроме
    резюме), — отказ от подачи целиком, заявка остаётся владельцу;
  - булево согласие на обработку данных заполняется True: подачу владелец
    санкционировал, а без согласия форма не принимается в принципе.

Email в форме — собственный ящик с plus-меткой заявки (suren6pro+jh42x…):
ответ рекрутёра приходит обычным письмом на адрес из формы, и только по
этой метке почтовый цикл (правило «plus» в mailmatch) привяжет его к
заявке — нашего Message-ID в такой переписке нет, email_peer пуст, а
contact_url ashby-вакансии — ссылка на доску, не адрес. Ящик, который
читает IMAP, обязан совпадать с email профиля, иначе ответ потеряется
молча — такая заявка не подаётся вовсе.

Путь статуса — тот же, что у остальных каналов: stage() возвращает
ashby-кандидатов из HANDLE_MISSING в DISCOVERED, штатный prepare готовит
письмо и резюме через гейт (PENDING_APPROVAL), step_auto_approve одобряет
по скору (APPROVED) — и только APPROVED заявки подаются здесь. Дублировать
подготовку внутри этого модуля нельзя: получилось бы второе место, где
рождаются письма, и однажды они разошлись бы с гейтом. Перед сетевым POST
заявка захватывается переходом APPROVED → SENDING вместе с ключом
идемпотентности — ДО сети: упади процесс между POST и записью результата,
reclaim_stale_sending уведёт заявку в SEND_FAILED_AMBIGUOUS, а не на
повторную подачу вслепую. Успех — SENT; точный отказ формы — SEND_FAILED
(повтор без правки бессмысленен); неоднозначный сбой сети —
SEND_FAILED_AMBIGUOUS до решения владельца.

    python -m jobhunter.apply.submit_ashby --dry            # показать пейлоады
    python -m jobhunter.apply.submit_ashby --live --limit 3 # боевая подача
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, Job, Message, SendLog, Status, utcnow
from ..profile import get_profile
from ..tailor.render import resolve_cv
from .forms import fetch_form, form_ref_from_job

SUBMIT_URL = "https://api.ashbyhq.com/posting-api/application-form/submit"
# Порог выше, чем у ручной очереди (45): автоматика подаёт только то, в чём
# уверена, пограничные вакансии остаются на решение владельцу.
ATS_MIN_SCORE = 55.0
# Имя multipart-части с файлом резюме; на него ссылается value
# соответствующего fieldSubmission — так задаёт формат Ashby.
RESUME_PART = "resume_file"


class AshbyRejected(RuntimeError):
    """HTTP 200, но success=false — форма отклонила подачу."""


def _field_value(f, ident: dict, letter: str):
    """Значение одного поля из фактов, или None (нечем заполнить).

    Сознательно НЕ отвечает на скрининговые вопросы (виза, право на работу,
    зарплата, релокация и т.п.) — даже когда ответ кажется выводимым из
    профиля: уверенно-неверный ответ закрывает компанию навсегда. По той же
    причине не заполняются селекты и небулевы «согласия»: выбор варианта —
    это ответ на вопрос, а не факт из identity.
    """
    probe = ("%s %s" % (f.name or "", f.label or "")).lower()
    if re.search(r"privacy|gdpr|personal\s+data|"
                 r"обработк\w*\s+(?:персональных\s+)?данн", probe):
        # Согласие на обработку данных — только явным булевым чекбоксом;
        # селект или текст с теми же словами — уже вопрос владельцу.
        return True if f.type == "boolean" else None
    if re.search(r"consent|соглас", probe):
        # Согласие на что-то другое (background check, условия) — не наше
        # решение: галочку ставит владелец.
        return None
    if f.is_choice or f.type == "boolean":
        return None
    if f.name == "_systemfield_name" or \
            re.search(r"full\s*name|полное\s+имя", probe):
        return ident.get("full_name_en") or None
    if re.search(r"e-?mail|почта", probe):
        return ident.get("email") or None
    if re.search(r"phone|телефон", probe):
        return ident.get("phone") or None
    if re.search(r"\blocation|\bcity\b|\bгород|прожив", probe):
        # \b отсекает relocation/relocate: вопрос о переезде — скрининг,
        # а не место жительства.
        return ident.get("location") or None
    if re.search(r"linkedin", probe):
        return ident.get("linkedin") or None
    if re.search(r"github", probe):
        return ident.get("github") or None
    if re.search(r"website|portfolio|сайт", probe):
        return ident.get("website") or ident.get("portfolio") or None
    if re.search(r"cover\s*letter|why\b|motivation|сопроводительн", probe):
        return letter or None
    return None


def _candidate_email(app_id: int, ident: dict):
    """(адрес для формы, "") или (None, причина не подавать).

    Ответы Ashby приходят обычным письмом на адрес из формы. Plus-метка
    заявки (outreach.mailer.reply_to_addr) доставляется в тот же ящик и
    опознаётся правилом «plus» почтового цикла — единственная привязка,
    доступная при подаче через форму. Метка ставится только на ящик из
    профиля: если IMAP читает другой ящик, ответ рекрутёра потеряется
    молча, и подавать в таком состоянии нельзя.
    """
    base = (ident.get("email") or "").strip()
    if not base:
        return None, "в профиле нет email"
    smtp_user = (get_settings().smtp_user or "").strip()
    if smtp_user.lower() != base.lower():
        return None, ("почтовый ящик системы (%s) не совпадает с email "
                      "профиля — ответ рекрутёра некому прочитать"
                      % (smtp_user or "не задан"))
    from ..outreach.mailer import reply_to_addr
    return reply_to_addr(app_id), ""


def build_submission(app_id: int, http=None) -> tuple:
    """(payload, "") — пейлоад для POST, или (None, причина) — не подавать.

    Причина уходит в карточку владельцу как есть, поэтому пишется
    по-человечески: какое поле и почему осталось без ответа.
    """
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if app is None:
            return None, "заявка не найдена"
        job = sess.get(Job, app.job_id)
        if job is None:
            return None, "вакансия не найдена"
        ref = form_ref_from_job(job)
        letter = app.message_body or ""
        gate_ok = bool(app.gate_passed)
        cv_hint = app.cv_path or ""

    if ref is None or ref[0] != "ashby":
        return None, "не ashby-вакансия"
    _provider, board, job_id = ref
    if not letter.strip() or not gate_ok:
        # Письмо — обязательный артефакт подачи, даже если в форме нет поля
        # под него: без прошедшего гейт текста система не подаёт ничего.
        return None, "письмо не готово или не прошло гейт правды"

    try:
        spec = fetch_form("ashby", board, job_id, http=http)
    except Exception as e:                                 # noqa: BLE001
        # Мёртвая доска или обрыв сети: пропуск одной заявки, а не падение
        # всего прогона очереди.
        return None, "схема формы недоступна: %s" % type(e).__name__
    if not spec.supported:
        return None, spec.note or "форма недоступна"

    ident = dict(get_profile().raw.get("identity", {}) or {})
    email, why = _candidate_email(app_id, ident)
    if email is None:
        return None, why
    ident["email"] = email
    resume = resolve_cv(cv_hint)
    subs, missing, resume_used = [], [], False
    for f in spec.fields:
        if f.is_file:
            if re.search(r"resume|cv|резюме",
                         ("%s %s" % (f.name, f.label)).lower()):
                if resume:
                    subs.append({"path": f.name, "value": RESUME_PART})
                    resume_used = True
                else:
                    # Резюме обязательно независимо от required у поля:
                    # отклик без него хуже неподачи.
                    missing.append("%s (резюме не найдено)" % (f.label or f.name))
            elif f.required:
                missing.append("%s (файл кроме резюме)" % (f.label or f.name))
            continue
        val = _field_value(f, ident, letter)
        if val is None:
            if f.required:
                missing.append(f.label or f.name)
            continue
        subs.append({"path": f.name, "value": val})

    if missing:
        return None, "нет фактов для: " + "; ".join(missing)

    return {
        "url": SUBMIT_URL,
        "peer": "ashby:%s/%s" % (board, job_id),
        "data": {
            "organizationHostedJobsPageName": board,
            "jobPostingId": job_id,
            "applicationForm": json.dumps({"fieldSubmissions": subs},
                                          ensure_ascii=False),
        },
        "resume_path": resume if resume_used else "",
        "resume_part": RESUME_PART,
        "letter": letter,
    }, ""


def _sent_today(sess) -> int:
    """Сколько ATS-подач уже ушло сегодня (по SendLog, peer ashby:*)."""
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return sess.scalar(
        select(func.count()).select_from(SendLog).where(
            SendLog.result == "ok",
            SendLog.peer_id.like("ashby:%"),
            SendLog.attempted_at >= day_start)) or 0


def _delivery_ambiguous(e: Exception) -> bool:
    """Мог ли Ashby принять отклик до того, как мы увидели ошибку.

    Ответ формы success=false и клиентские 4xx означают «не принято» —
    заявку можно вернуть владельцу на разбор с возможностью повтора. Обрыв
    сети, таймаут и 5xx доказательства не дают: подача могла дойти, и
    повтор разрешается только явным решением владельца.
    """
    if isinstance(e, AshbyRejected):
        return False
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500
    return True


def _locks(sess, app_id: int, s) -> str:
    """Причина не подавать, или пустая строка. Замки идемпотентности.

    Статус (берём только APPROVED — письмо готово и одобрено), sent_at и
    SendLog(ok) — страховка от повторной подачи, если статус кто-то откатил
    руками, — плюс дневная квота. Вызывается дважды: до сборки пейлоада и
    повторно в транзакции захвата, где и закрывается окно гонки.
    """
    app = sess.get(Application, app_id)
    if app is None:
        return "заявка не найдена"
    if app.status != Status.APPROVED.value:
        return "статус %s" % app.status
    if app.sent_at is not None:
        return "уже подана"
    dup = sess.scalar(
        select(func.count()).select_from(SendLog).where(
            SendLog.application_id == app_id,
            SendLog.result == "ok",
            SendLog.peer_id.like("ashby:%"))) or 0
    if dup:
        return "уже подана"
    if _sent_today(sess) >= s.ats_daily_limit:
        return "daily_limit"
    return ""


def submit(app_id: int, dry: bool = True, http=None) -> str:
    """Подать одну заявку. 'ok' | 'skipped:<почему>' | 'error:<класс>'."""
    s = get_settings()
    with session_scope() as sess:
        why = _locks(sess, app_id, s)
    if why:
        return "skipped:%s" % why

    payload, reason = build_submission(app_id, http=http)
    if payload is None:
        return "skipped:%s" % reason

    if dry:
        print("DRY %s" % payload["peer"])
        print("  POST %s" % payload["url"])
        print("  applicationForm=%s" % payload["data"]["applicationForm"])
        if payload["resume_path"]:
            print("  %s=%s" % (payload["resume_part"], payload["resume_path"]))
        return "ok"

    peer = payload["peer"]
    files = None
    if payload["resume_path"]:
        p = Path(payload["resume_path"])
        files = {payload["resume_part"]:
                 (p.name, p.read_bytes(), "application/pdf")}

    # Захват до сети: перепроверка замков и перевод в SENDING одной
    # транзакцией закрывают окно между проверкой и POST — параллельный
    # процесс увидит уже не APPROVED. Ключ идемпотентности пишется
    # тоже до сети: заявка, зависшая в SENDING после сбоя, уйдёт через
    # reclaim_stale_sending в SEND_FAILED_AMBIGUOUS, а не на повтор вслепую.
    with session_scope() as sess:
        why = _locks(sess, app_id, s)
        if why:
            return "skipped:%s" % why
        app = sess.get(Application, app_id)
        now = utcnow()
        app.transition(Status.SENDING)
        app.sending_lease_until = now + timedelta(seconds=180)
        app.send_channel = "ats"
        app.send_idempotency_key = peer
        app.send_last_attempt_at = now
        app.send_next_try_at = None
        app.send_error_detail = ""
        app.send_attempts += 1

    own = http is None
    http = http or httpx.Client(trust_env=False, follow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = http.post(payload["url"], data=payload["data"], files=files,
                      timeout=30.0)
        r.raise_for_status()
        try:
            body = r.json()
        except Exception:                                  # noqa: BLE001
            body = {}
        if isinstance(body, dict) and body.get("success") is False:
            raise AshbyRejected(str(body.get("errors") or body)[:200])
    except Exception as e:                                 # noqa: BLE001
        ambiguous = _delivery_ambiguous(e)
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            app.sending_lease_until = None
            app.send_error_class = type(e).__name__
            app.send_error_detail = str(e)[:500]
            if ambiguous:
                app.transition(Status.SEND_FAILED_AMBIGUOUS,
                               reason=type(e).__name__)
            else:
                # Доставки точно не было. SEND_FAILED, а не откат в очередь:
                # форма отклонена по содержанию, и повторять её без правки
                # бессмысленно — решение за владельцем.
                app.transition(Status.SEND_FAILED,
                               reason="ashby отклонил форму")
            sess.add(SendLog(application_id=app_id,
                             result="ambiguous" if ambiguous else "error",
                             error_class=type(e).__name__, peer_id=peer))
        return "error:%s" % type(e).__name__
    finally:
        if own:
            http.close()

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        app.transition(Status.SENT)
        app.sending_lease_until = None
        app.sent_at = utcnow()
        app.last_outbound_at = utcnow()
        sess.add(SendLog(application_id=app_id, result="ok", peer_id=peer))
        sess.add(Message(application_id=app_id, direction="out",
                         body=payload["letter"], sent_at=utcnow(),
                         is_auto=True))
    return "ok"


def stage(limit: int = 10) -> dict:
    """Вернуть ashby-кандидатов в конвейер подготовки.

    HANDLE_MISSING → DISCOVERED; письмо и резюме сделает штатный prepare
    (гейт правды обязателен), одобрение — step_auto_approve по скору. Лимит
    небольшой намеренно: каждая подготовка жжёт LLM-квоту, а подача всё
    равно ограничена ATS_DAILY_LIMIT в день.
    """
    stats = {"staged": 0}
    with session_scope() as sess:
        ids = []
        for app, job in sess.execute(
                select(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .where(Application.status == Status.HANDLE_MISSING.value,
                       Application.sent_at.is_(None),
                       Application.score >= ATS_MIN_SCORE,
                       or_(Job.contact_url.contains("ashbyhq.com"),
                           Job.external_uuid.like("ats:ashby:%")))
                .order_by(Application.score.desc())):
            ref = form_ref_from_job(job)
            if ref is None or ref[0] != "ashby":
                continue
            ids.append(app.id)
            if len(ids) >= limit:
                break
    for aid in ids:
        with session_scope() as sess:
            app = sess.get(Application, aid)
            if app is None or app.status != Status.HANDLE_MISSING.value:
                continue
            if app.advance(Status.DISCOVERED,
                           reason="подготовка к подаче через анкету Ashby"):
                stats["staged"] += 1
    return stats


def run(limit: int = 5, dry: bool = True) -> dict:
    """Пройти очередь ashby-кандидатов. Счётчики для сводки автопилота.

    Берём только APPROVED: письмо существует, прошло гейт и одобрено — тем
    же путём, что для Telegram и почты. Подготовка письма — не забота этого
    модуля (см. stage()).
    """
    stats = {"checked": 0, "ok": 0, "skipped": 0, "errors": 0}
    with session_scope() as sess:
        picked = []
        for app, job in sess.execute(
                select(Application, Job)
                .join(Job, Application.job_id == Job.id)
                .where(Application.status == Status.APPROVED.value,
                       Application.score >= ATS_MIN_SCORE,
                       or_(Job.contact_url.contains("ashbyhq.com"),
                           Job.external_uuid.like("ats:ashby:%")))
                .order_by(Application.score.desc())):
            ref = form_ref_from_job(job)
            if ref is None or ref[0] != "ashby":
                continue
            picked.append({"id": app.id,
                           "ready": bool((app.message_body or "").strip()
                                         and app.gate_passed)})

    done = 0
    for c in picked:
        if done >= limit:
            break
        stats["checked"] += 1
        if not c["ready"]:
            stats["skipped"] += 1
            continue
        try:
            res = submit(c["id"], dry=dry)
        except Exception as e:                             # noqa: BLE001
            # Одна сломавшаяся заявка не должна останавливать очередь.
            res = "error:%s" % type(e).__name__
        print("  #%d %s" % (c["id"], res))
        if res == "ok":
            stats["ok"] += 1
            done += 1
        elif res.startswith("error"):
            stats["errors"] += 1
        else:
            stats["skipped"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Автоподача откликов в Ashby")
    ap.add_argument("--dry", action="store_true",
                    help="показать пейлоады без POST — схема формы всё же "
                         "читается сетью (режим по умолчанию)")
    ap.add_argument("--live", action="store_true",
                    help="боевая подача (без этого флага — всегда dry)")
    ap.add_argument("--stage", action="store_true",
                    help="вернуть кандидатов в конвейер подготовки писем")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    if args.stage:
        print(stage(limit=args.limit))
        return 0
    stats = run(limit=args.limit, dry=not args.live)
    print(stats)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
