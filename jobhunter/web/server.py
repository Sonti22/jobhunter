"""Локальный дашборд: очередь откликов, одобрение, статистика, стоп-кран.

    python -m jobhunter.web.server        → http://127.0.0.1:8765

Слушает только 127.0.0.1: наружу ничего не публикуется.
"""
from __future__ import annotations

import html
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..config import get_settings
from ..db import session_scope
from ..models import (
    Application,
    Batch,
    BotOutbox,
    BotTask,
    ContactKind,
    Employer,
    Job,
    Message,
    SendLog,
    Status,
    utcnow,
)
from ..outreach import policy

app = FastAPI(title="jobhunter")


@app.middleware("http")
async def _same_origin_posts(request, call_next):
    """POST принимается только со своих страниц.

    Дашборд слушает 127.0.0.1 без аутентификации, и это нормально ровно до
    тех пор, пока чужая вкладка в браузере владельца не отправит form POST
    на localhost: снять стоп-кран или одобрить рассылку мог любой сайт.
    Браузер всегда подписывает такие запросы заголовком Origin — по нему и
    режем. Запросы без Origin (curl, тесты, свои же формы старых браузеров
    шлют Referer) проверяются по Referer; нет ни того ни другого — пускаем:
    это не браузер, а локальный инструмент владельца.
    """
    if request.method == "POST":
        src = request.headers.get("origin") or request.headers.get("referer") or ""
        if src:
            from urllib.parse import urlparse
            host = (urlparse(src).hostname or "").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse("чужой origin", status_code=403)
    return await call_next(request)

CSS = """
:root{--ink:#14181f;--muted:#5b6472;--accent:#1f4e79;--rule:#dfe5ec;--bg:#f7f9fc;
      --ok:#1a7f37;--warn:#9a6700;--bad:#b42318}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{background:#fff;border-bottom:1px solid var(--rule);padding:14px 22px;
  display:flex;align-items:center;gap:18px;flex-wrap:wrap;position:sticky;top:0;z-index:5}
h1{font-size:17px;margin:0;color:var(--accent)}
.wrap{max-width:1180px;margin:0 auto;padding:20px 22px 60px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.card{background:#fff;border:1px solid var(--rule);border-radius:10px;padding:12px 16px;min-width:126px}
.card b{display:block;font-size:23px;line-height:1.2}
p.muted{color:var(--muted);margin:6px 0 12px;max-width:70ch}
.card span{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--rule);
  border-radius:10px;overflow:hidden}
th{background:#eef2f7;text-align:left;font-size:12px;color:var(--muted);
  padding:9px 11px;font-weight:600}
td{padding:9px 11px;border-top:1px solid var(--rule);vertical-align:top}
tr:hover td{background:#fafcff}
.msg{color:var(--muted);font-size:12.5px;max-width:560px}
.tag{display:inline-block;background:#eef2f7;border-radius:5px;padding:1px 7px;font-size:11.5px}
.btn{display:inline-block;background:var(--accent);color:#fff;border:0;border-radius:7px;
  padding:8px 14px;font-size:13px;cursor:pointer;text-decoration:none}
.btn.ghost{background:#fff;color:var(--accent);border:1px solid var(--accent)}
.btn.bad{background:var(--bad)}
.btn.sm{padding:4px 9px;font-size:12px}
a{color:var(--accent)}
.pill{font-size:11.5px;padding:2px 8px;border-radius:999px}
.pill.ok{background:#e6f4ea;color:var(--ok)} .pill.warn{background:#fff4e5;color:var(--warn)}
.pill.bad{background:#fdecea;color:var(--bad)}
.bar{background:#fff;border:1px solid var(--rule);border-radius:10px;padding:14px 16px;margin-bottom:18px}
pre{white-space:pre-wrap;background:#f4f6f9;padding:11px;border-radius:8px;font-size:12.5px}
"""


def _h(s) -> str:
    return html.escape(str(s or ""))


def _safe_url(u: str) -> str:
    """Ссылка из собранных данных годится в href только с безопасной схемой.

    _h экранирует разметку, но не схему: javascript:-URL из текста вакансии
    прошёл бы в кнопку «открыть» как есть и исполнился по клику.
    """
    u = str(u or "").strip()
    low = u.lower()
    if low.startswith(("http://", "https://", "mailto:")):
        return u
    return "#"


def _layout(body: str, banner: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>jobhunter</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>%s</style>"
        "<header><h1>jobhunter</h1>"
        "<a href='/'>Очередь</a><a href='/manual'>Отклик вручную</a>"
        "<a href='/sent'>Отправленные</a>"
        "<a href='/stats'>Статистика</a><a href='/blacklist'>Blacklist</a>"
        "<a href='/health'>Состояние</a></header>"
        "<div class='wrap'>%s%s</div>" % (CSS, banner, body))


def _status_counts(sess) -> Counter:
    # Агрегат в SQL, а не загрузка всей таблицы в ORM: на 9.5k заявок прежняя
    # версия делала каждый показ главной в ~10 раз дороже /stats.
    from sqlalchemy import func
    rows = sess.execute(select(Application.status, func.count())
                        .group_by(Application.status)).all()
    return Counter(dict(rows))


# ─────────────────────────────────────────────────────── очередь ──

@app.get("/", response_class=HTMLResponse)
@app.get("/queue", response_class=HTMLResponse)
def queue(q: str = "", source: str = "", page: int = 1,
          page_size: int = 40):
    s = get_settings()
    page = max(1, page)
    page_size = min(100, max(10, page_size))
    with session_scope() as sess:
        # gate_passed обязателен: заявка без гейта не может быть одобрена
        # (инвариант transition), и показывать её с галочкой — обещать то,
        # что /approve не выполнит.
        query = (select(Application).join(Job, Application.job_id == Job.id)
                 .where(Application.status.in_((Status.PENDING_APPROVAL.value,
                                               Status.FOLLOWUP_PENDING_APPROVAL.value)),
                        Application.gate_passed.is_(True),
                        (Job.is_closed.is_(False) | Job.is_closed.is_(None))))
        if q.strip():
            needle = "%" + q.strip().replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where((Job.title.ilike(needle, escape="\\")) |
                                (Job.company_name.ilike(needle, escape="\\")) |
                                (Job.tag.ilike(needle, escape="\\")))
        if source.strip():
            query = query.where(Job.source.like(source.strip() + "%"))
        apps = sess.scalars(query.order_by(Application.score.desc())
                            .offset((page - 1) * page_size).limit(page_size)).all()
        counts = _status_counts(sess)
        quota_state = policy.get_quota(sess)
        st = policy.get_state(sess)
        verdict = policy.can_send_cold(sess)
        rows = []
        for a in apps:
            j = sess.get(Job, a.job_id)
            if j.contact_kind == ContactKind.USER_HANDLE.value:
                contact, chan = "@" + j.contact_handle, "telegram"
            elif j.contact_kind == ContactKind.EMAIL.value:
                contact, chan = (j.contact_url or "").replace("mailto:", ""), "email"
            else:
                contact, chan = "—", "нет"
            promoted = ", ".join(a.promoted_terms_json or [])
            rows.append(
                "<tr><td><input type='checkbox' name='ids' value='%d' checked></td>"
                "<td><b>%.0f</b></td><td><span class='tag'>%s</span></td>"
                "<td><a href='/applications/%d'><b>%s</b></a>"
                "<div class='msg'>%s</div>"
                "%s</td>"
                "<td>%s<br><span class='tag'>%s</span></td>"
                "<td><a class='btn ghost sm' href='/cv/%d'>резюме</a></td></tr>"
                % (a.id, a.score, _h(j.tag), a.id, _h(j.title or "(без названия)"),
                   _h(a.message_body),
                   ("<div class='msg'>поднято из вакансии: <b>%s</b></div>" % _h(promoted))
                   if promoted else "",
                   _h(contact), chan, a.id))

        cap = min(quota_state.planned_cap or st.quota_ceiling, st.quota_ceiling)
        pill = ("<span class='pill ok'>отправка разрешена</span>" if verdict.allowed
                else "<span class='pill bad'>%s</span>" % _h(verdict.reason))
        cards = (
            "<div class='cards'>"
            "<div class='card'><b>%d</b><span>в очереди</span></div>"
            "<div class='card'><b>%d</b><span>одобрено</span></div>"
            "<div class='card'><b>%d</b><span>отправлено</span></div>"
            "<div class='card'><b>%d</b><span>ждут ответа</span></div>"
            "<div class='card'><b>%d/%d</b><span>квота сегодня</span></div>"
            "</div>"
            % (counts.get(Status.PENDING_APPROVAL.value, 0),
               counts.get(Status.APPROVED.value, 0),
               counts.get(Status.SENT.value, 0) + counts.get(Status.AWAITING_REPLY.value, 0),
               counts.get(Status.AWAITING_REPLY.value, 0), quota_state.sent_count, cap))

        body = cards + (
            "<div class='bar'>%s &nbsp; лимит %d/день &nbsp;·&nbsp; "
            "резюме в первом сообщении: %s"
            "<form method='post' action='/killswitch' style='display:inline;float:right'>"
            "<button class='btn %s' name='on' value='%s'>%s</button></form></div>"
            % (pill, cap, "да" if s.send_cv_with_first_message else "нет",
               "ghost" if policy.kill_switch_active() else "bad",
               "0" if policy.kill_switch_active() else "1",
               "Снять стоп" if policy.kill_switch_active() else "СТОП отправки"))

        if not rows:
            body += "<p>Очередь пуста. Собери вакансии и подготовь отклики:</p><pre>" \
                    "python -m jobhunter.ingest.all_sources\npython -m jobhunter.pipeline</pre>"
        else:
            body += (
                "<form method='get' action='/' class='bar'>"
                "<input name='q' value='%s' placeholder='поиск по вакансии/компании'> "
                "<input name='source' value='%s' placeholder='источник'> "
                "<button class='btn ghost sm' type='submit'>Фильтр</button> "
                "<a class='btn ghost sm' href='/'>Сбросить</a></form>"
                "<form method='post' action='/approve'>"
                "<table><tr><th></th><th>скор</th><th>тег</th><th>вакансия и письмо</th>"
                "<th>контакт</th><th></th></tr>%s</table>"
                "<p style='margin-top:14px'>"
                "<button class='btn' type='submit'>Одобрить отмеченные</button> "
                "<span class='msg'>после одобрения: "
                "<code>python -m jobhunter.outreach.sender</code></span></p>"
                "</form>" % (_h(q), _h(source), "".join(rows)))
    return _layout(body)


@app.post("/approve")
def approve(ids: list[int] | None = Form(default=None)):
    ids = ids or []
    with session_scope() as sess:
        batch = Batch(planned_count=len(ids), approved_at=utcnow(),
                      approved_count=0)
        sess.add(batch)
        sess.flush()
        done = 0
        for aid in ids:
            a = sess.get(Application, aid)
            # Follow-up здесь равноправен: очередь показывает оба статуса,
            # и молча игнорировать отмеченный follow-up — терять напоминание.
            if a is None or a.status not in (
                    Status.PENDING_APPROVAL.value,
                    Status.FOLLOWUP_PENDING_APPROVAL.value):
                continue
            # Одна недопустимая заявка (гейт сбит руками/инцидентом) не
            # должна валить одобрение всей пачки пятисоткой.
            try:
                a.transition(Status.APPROVED)
            except Exception:                              # noqa: BLE001
                continue
            a.approved_at = utcnow()
            a.batch_id = batch.id
            done += 1
        # Счётчики — по факту, а не по длине формы: иначе аналитика батчей
        # считает пропущенные и несуществующие id за одобренные.
        batch.approved_count = done
    return RedirectResponse("/", status_code=303)


@app.post("/killswitch")
def killswitch(on: str = Form(...)):
    from ..owner import set_kill_switch
    set_kill_switch(on == "1", "дашборд")
    return RedirectResponse("/", status_code=303)


@app.get("/cv/{app_id}")
def cv(app_id: int):
    with session_scope() as sess:
        a = sess.get(Application, app_id)
        if not a or not a.cv_path or not Path(a.cv_path).exists():
            return HTMLResponse("<p>Резюме не найдено</p>", status_code=404)
        return FileResponse(a.cv_path, media_type="application/pdf",
                            filename=Path(a.cv_path).name)


@app.get("/applications/{app_id}", response_class=HTMLResponse)
def application_detail(app_id: int):
    """История заявки: вакансия, гейт, отправки и переписка в одном месте."""
    with session_scope() as sess:
        a = sess.get(Application, app_id)
        if not a:
            return HTMLResponse("<p>Заявка не найдена</p>", status_code=404)
        j = sess.get(Job, a.job_id)
        emp = sess.get(Employer, a.employer_id) if a.employer_id else None
        messages = sess.scalars(select(Message).where(
            Message.application_id == app_id).order_by(Message.id)).all()
        logs = sess.scalars(select(SendLog).where(
            SendLog.application_id == app_id).order_by(SendLog.id.desc()).limit(10)).all()

        contact = (j.contact_handle and "@" + j.contact_handle) or j.contact_url or "—"
        history = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td><pre>%s</pre></td></tr>"
            % (_h(m.sent_at or m.received_at or "—"),
               "входящее" if m.direction == "in" else "исходящее",
               _h(m.email_subject or ""), _h(m.body))
            for m in messages)
        log_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (_h(x.attempted_at), _h(x.result), _h(x.error_class), _h(x.peer_id))
            for x in logs)
        actions = ""
        if a.status == Status.SEND_FAILED_AMBIGUOUS.value:
            actions += ("<form method='post' action='/applications/%d/requeue'>"
                        "<button class='btn bad' type='submit'>Я проверил доставку — вернуть в очередь</button></form>"
                        % app_id)
        if emp:
            actions += ("<form method='post' action='/blacklist/%d' style='margin-top:8px'>"
                        "<input type='hidden' name='on' value='%s'>"
                        "<button class='btn ghost sm' type='submit'>%s</button></form>"
                        % (emp.id, "0" if emp.do_not_contact else "1",
                           "Снять blacklist" if emp.do_not_contact else "Добавить в blacklist"))
        body = (
            "<p><a href='/'>← очередь</a></p><h2>%s</h2>"
            "<div class='bar'><b>%s</b> · %s · score %.0f<br>контакт: %s<br>"
            "источник: %s · вакансия: %s<br>последний входящий: %s</div>"
            "<div class='bar'>статус: <span class='pill warn'>%s</span> "
            "попыток отправки: %d · канал: %s<br>ошибка: %s%s</div>"
            "<h3>Описание вакансии</h3><pre>%s</pre>"
            "<h3>История сообщений</h3><table><tr><th>время</th><th>тип</th>"
            "<th>тема</th><th>текст</th></tr>%s</table>"
            "<h3>Журнал отправок</h3><table><tr><th>время</th><th>результат</th>"
            "<th>ошибка</th><th>адресат</th></tr>%s</table>"
            % (_h(j.title or "Заявка #%d" % app_id), _h(j.company_name),
               _h(j.title), a.score, _h(contact), _h(j.source),
               _h(j.last_seen_at or "—"), _h(a.last_inbound_at or "—"),
               _h(a.status), a.send_attempts, _h(a.send_channel or "—"),
               _h(a.send_error_detail or a.send_error_class or "—"), actions,
               _h(j.description_raw), history or "<tr><td colspan=4>нет сообщений</td></tr>",
               log_rows or "<tr><td colspan=4>нет отправок</td></tr>"))
    return _layout(body)


@app.post("/applications/{app_id}/requeue")
def application_requeue(app_id: int):
    from ..outreach.sender import requeue_ambiguous
    requeue_ambiguous(app_id)
    return RedirectResponse("/applications/%d" % app_id, status_code=303)


@app.get("/api/summary")
def api_summary():
    """Маленький read-only API для будущего внешнего виджета/мониторинга."""
    from .. import report
    payload = {"totals": report.totals(),
               "funnel": report.funnel(),
               "conversion": report.conversion_metrics(),
               "quota": report.quota(),
               "telegram": report.telegram_health()}
    return JSONResponse(jsonable_encoder(payload))


@app.get("/api/applications")
def api_applications(status: str = "", source: str = "", q: str = "",
                     page: int = 1, page_size: int = 50):
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    with session_scope() as sess:
        query = select(Application).join(Job, Application.job_id == Job.id)
        if status:
            query = query.where(Application.status == status)
        if source:
            query = query.where(Job.source.like(source + "%"))
        if q:
            needle = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where((Job.title.ilike(needle, escape="\\")) |
                                (Job.company_name.ilike(needle, escape="\\")))
        rows = sess.scalars(query.order_by(Application.updated_at.desc())
                            .offset((page - 1) * page_size).limit(page_size)).all()
        data = []
        for a in rows:
            job = sess.get(Job, a.job_id)
            data.append({"id": a.id, "status": a.status, "score": a.score,
                         "title": job.title if job else "",
                         "company": job.company_name if job else "",
                         "source": job.source if job else "",
                         "closed": bool(job.is_closed) if job else False,
                         "sent_at": a.sent_at.isoformat() if a.sent_at else None,
                         "first_reply_at": (a.first_reply_at.isoformat()
                                             if a.first_reply_at else None)})
    return {"page": page, "page_size": page_size, "items": data}


@app.get("/blacklist", response_class=HTMLResponse)
def blacklist():
    with session_scope() as sess:
        employers = sess.scalars(select(Employer).where(
            Employer.do_not_contact.is_(True)).order_by(Employer.display_name,
                                                          Employer.handle_norm)).all()
        rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%d</td><td>%s</td><td>"
            "<form method='post' action='/blacklist/%d'><input type='hidden' name='on' value='0'>"
            "<button class='btn ghost sm'>разрешить</button></form></td></tr>"
            % (_h(e.display_name or "—"), _h(e.handle_norm), e.total_jobs_seen,
               _h(e.notes or ""), e.id) for e in employers)
    body = ("<h2>Blacklist</h2><p class='msg'>Компании и контакты, которым "
            "автоматическая система больше не пишет.</p>"
            "<table><tr><th>компания</th><th>контакт</th><th>вакансий</th>"
            "<th>заметки</th><th></th></tr>%s</table>"
            % (rows or "<tr><td colspan=5>blacklist пуст</td></tr>"))
    return _layout(body)


@app.post("/blacklist/{employer_id}")
def blacklist_toggle(employer_id: int, on: str = Form("1")):
    with session_scope() as sess:
        employer = sess.get(Employer, employer_id)
        if employer:
            employer.do_not_contact = on == "1"
    return RedirectResponse("/blacklist", status_code=303)


# ─────────────────────────────────────────── отклик вручную (ATS) ──

@app.get("/manual", response_class=HTMLResponse)
def manual():
    """Вакансии напрямую от компаний: резюме готово, отклик подаёшь сам.

    Формы ATS автозаполнять нельзя — отсеивающие вопросы там дают
    уверенно-неверные ответы и закрывают компанию навсегда.
    """
    from ..manual_apply import listing
    rows = listing(40)
    from ..manual_apply import stats as manual_stats
    st = manual_stats()
    body = (
        "<div class='cards'>"
        "<div class='card'><b>%d</b><span>без контакта</span></div>"
        "<div class='card'><b>%d</b><span>подходящих</span></div>"
        "<div class='card'><b>%d</b><span>откликнулся</span></div>"
        "<div class='card'><b>%d</b><span>отложено</span></div></div>"
        % (st["total"], st["ready"], st["applied"], st["snoozed"]) +
        "<div class='bar'><b>Отклик через форму компании.</b> Резюме под каждую "
        "вакансию уже собрано — открываешь ссылку, прикладываешь файл, отправляешь. "
        "Автозаполнение форм намеренно не делаем: отсеивающие вопросы требуют твоих "
        "ответов, а неверный ответ закрывает компанию навсегда.</div>")
    if not rows:
        body += ("<p>Пока пусто. Оцени и подготовь:</p>"
                 "<pre>python -m jobhunter.manual_apply --top 30</pre>")
        return _layout(body)
    def _mark_form(app_id: int) -> str:
        """Три кнопки-отметки. Состояние общее с ботом — одна таблица."""
        buttons = [("applied", "откликнулся"), ("not_fit", "не подходит"),
                   ("snoozed", "потом")]
        return "".join(
            "<form method='post' action='/manual/mark' style='display:inline'>"
            "<input type='hidden' name='app_id' value='%d'>"
            "<input type='hidden' name='outcome' value='%s'>"
            "<button class='btn ghost sm' type='submit'>%s</button></form> "
            % (app_id, key, label) for key, label in buttons)

    tr = "".join(
        "<tr><td><b>%.0f</b></td><td>%s</td><td>%s<br><span class='tag'>%s</span>"
        "%s</td>"
        "<td><a class='btn sm' href='%s' target='_blank' rel='noopener'>открыть</a></td>"
        "<td>%s</td><td>%s</td></tr>"
        % (r["score"], _h(r["company"]), _h(r["title"]), _h(r["tag"]),
           ("  <span class='tag'>%s</span>" % _h(r["salary"])) if r["salary"] else "",
           _h(_safe_url(r["url"])),
           ("<a class='btn ghost sm' href='/cv/%d'>резюме</a>" % r["id"])
           if r["cv_path"] else "<span class='msg'>нет</span>",
           _mark_form(r["id"]))
        for r in rows)
    body += ("<table><tr><th>скор</th><th>компания</th><th>вакансия</th>"
             "<th></th><th>резюме</th><th>отметка</th></tr>%s</table>" % tr)
    return _layout(body)


@app.post("/manual/mark")
def manual_mark(app_id: int = Form(...), outcome: str = Form(...)):
    """Отметка ручного отклика. Та же функция, что зовут кнопки бота."""
    from ..manual_apply import mark
    mark(app_id, outcome)
    return RedirectResponse("/manual", status_code=303)


# ────────────────────────────────────────────────── отправленные ──

@app.get("/sent", response_class=HTMLResponse)
def sent():
    with session_scope() as sess:
        apps = sess.scalars(
            select(Application)
            .where(Application.status.in_([
                Status.SENT.value, Status.AWAITING_REPLY.value,
                Status.FOLLOWED_UP.value, Status.REPLIED.value,
                Status.IN_DIALOGUE.value, Status.NEEDS_HUMAN.value,
                Status.INTERVIEW_CONFIRMED.value, Status.NO_REPLY_CLOSED.value]))
            .order_by(Application.sent_at.desc())).all()
        rows = []
        for a in apps:
            j = sess.get(Job, a.job_id)
            cls = {"REPLIED": "ok", "IN_DIALOGUE": "ok", "INTERVIEW_CONFIRMED": "ok",
                   "NEEDS_HUMAN": "warn", "NO_REPLY_CLOSED": "bad"}.get(a.status, "")
            rows.append("<tr><td>%s</td><td><span class='tag'>%s</span></td>"
                        "<td>%s</td><td>%s</td>"
                        "<td><span class='pill %s'>%s</span></td></tr>"
                        % (a.sent_at.strftime("%d.%m %H:%M") if a.sent_at else "—",
                           _h(j.tag), _h(j.title or ""),
                           _h("@" + j.contact_handle if j.contact_handle else j.contact_url),
                           cls, a.status))
        body = ("<h3>Отправленные: %d</h3><table><tr><th>когда</th><th>тег</th>"
                "<th>вакансия</th><th>контакт</th><th>статус</th></tr>%s</table>"
                % (len(apps), "".join(rows) or "<tr><td colspan=5>пусто</td></tr>"))
    return _layout(body)


# ──────────────────────────────────────────────────── статистика ──

@app.get("/stats", response_class=HTMLResponse)
def stats():
    """Статистика. Считает report.py — тот же код, что у бота.

    Раньше здесь была своя копия агрегатов: она грузила все заявки и вакансии
    в память ради нескольких счётчиков и неминуемо разошлась бы с цифрами
    бота при первой же правке одной из двух реализаций.
    """
    from .. import report

    t = report.totals()
    f = report.funnel()
    metrics = report.conversion_metrics()
    rate = 100.0 * t["replied"] / t["sent"] if t["sent"] else 0.0
    avg_response = ("%.1f ч" % metrics["avg_response_hours"]
                    if metrics["avg_response_hours"] is not None else "—")

    def _share_rows(rows):
        if not rows:
            return "<tr><td colspan=4>ещё нет данных</td></tr>"
        return "".join(
            "<tr><td>%s</td><td>%d</td><td>%d</td><td>%.0f%%</td></tr>"
            % (_h(r["key"]), r["sent"], r["replied"], r["rate"]) for r in rows)

    st_rows = "".join("<tr><td>%s</td><td>%d</td></tr>" % (_h(k), v)
                      for k, v in sorted(f["counts"].items(),
                                         key=lambda kv: -kv[1]) if v)
    warn = ("<p class='muted'>Отправлено %d — этого мало, чтобы сравнивать "
            "шаблоны: разница между ними пока неотличима от случайности. "
            "Цифры начнут что-то значить после ~200 отправок.</p>"
            % t["sent"]) if t["sent"] < 200 else ""

    body = (
        "<div class='cards'><div class='card'><b>%d</b><span>всего заявок</span></div>"
        "<div class='card'><b>%d</b><span>отправлено</span></div>"
        "<div class='card'><b>%.0f%%</b><span>ответов</span></div></div>"
        "<div class='bar'><b>Последние %d дней:</b> ответили %d · интервью %d · "
        "офферы %d · среднее время ответа %s</div>"
        "<div style='display:flex;gap:18px;flex-wrap:wrap'>"
        "<div style='flex:1;min-width:260px'><h3>Статусы</h3><table>%s</table></div>"
        "<div style='flex:1;min-width:300px'><h3>Ответы по источникам</h3>"
        "<table><tr><th>источник</th><th>ушло</th><th>ответов</th><th>доля</th></tr>"
        "%s</table></div></div>"
        "<h3 style='margin-top:22px'>Ответы по шаблонам письма</h3>%s"
        "<table><tr><th>шаблон</th><th>ушло</th><th>ответов</th><th>доля</th></tr>%s</table>"
        % (t["applications"], t["sent"], rate, metrics["days"],
           metrics["replied"], metrics["interviews"], metrics["offers"], avg_response,
           st_rows,
           _share_rows(report.by_source()), warn,
           _share_rows(report.by_template())))
    source_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%d</td><td>%s</td></tr>"
        % (_h(row["source"]), row["jobs"], row["closed"],
           _h(row["last_seen_at"] or "—"))
        for row in report.source_health())
    body += ("<h3 style='margin-top:22px'>Здоровье источников</h3>"
             "<table><tr><th>источник</th><th>вакансий</th><th>закрыто</th>"
             "<th>последний seen</th></tr>%s</table>"
             % (source_rows or "<tr><td colspan=4>нет данных</td></tr>"))
    return _layout(body)


# ───────────────────────────────────────────────────── состояние ──

@app.get("/health", response_class=HTMLResponse)
def health():
    s = get_settings()
    with session_scope() as sess:
        st = policy.get_state(sess)
        q = policy.get_quota(sess)
        lk = policy.get_lock(sess)
        ambiguous = sess.scalar(select(func.count(Application.id)).where(
            Application.status == Status.SEND_FAILED_AMBIGUOUS.value)) or 0
        failed = sess.scalar(select(func.count(Application.id)).where(
            Application.status == Status.SEND_FAILED.value)) or 0
        pending_outbox = sess.scalar(select(func.count(BotOutbox.id)).where(
            BotOutbox.sent_at.is_(None))) or 0
        failed_tasks = sess.scalar(select(func.count(BotTask.id)).where(
            BotTask.status == "failed")) or 0
        logs = sess.scalars(select(SendLog).order_by(SendLog.id.desc()).limit(25)).all()
        log_rows = "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (l.attempted_at.strftime("%d.%m %H:%M") if l.attempted_at else "",
               _h(l.result), _h(l.error_class), _h(l.peer_id))
            for l in logs)

        ready = [("Telegram API", bool(s.tg_api_id and s.telegram_api_hash)),
                 ("Telegram сессия", Path(s.telegram_session_path).exists()),
                 ("SMTP", bool(s.smtp_user and s.smtp_app_password)),
                 ("careered токен", bool(s.auth_header))]
        rd = "".join("<tr><td>%s</td><td><span class='pill %s'>%s</span></td></tr>"
                     % (n, "ok" if ok else "bad", "готово" if ok else "не настроено")
                     for n, ok in ready)

        body = (
            "<h3>Готовность каналов</h3><table>%s</table>"
            "<h3 style='margin-top:22px'>Кампания</h3><table>"
            "<tr><td>дневной потолок</td><td>%d</td></tr>"
            "<tr><td>отправлено сегодня</td><td>%d</td></tr>"
            "<tr><td>чистых дней подряд</td><td>%d</td></tr>"
            "<tr><td>PeerFlood всего</td><td>%d</td></tr>"
            "<tr><td>ручной режим</td><td>%s</td></tr>"
            "<tr><td>лок до</td><td>%s</td></tr>"
            "<tr><td>стоп-файл</td><td>%s</td></tr>"
            "<tr><td>неоднозначные отправки</td><td>%d</td></tr>"
            "<tr><td>ошибки отправки</td><td>%d</td></tr>"
            "<tr><td>outbox не доставлен</td><td>%d</td></tr>"
            "<tr><td>задачи бота failed</td><td>%d</td></tr>"
            "</table>"
            "<h3 style='margin-top:22px'>Последние отправки</h3>"
            "<table><tr><th>когда</th><th>результат</th><th>ошибка</th><th>адресат</th></tr>%s</table>"
            % (rd, st.quota_ceiling, q.sent_count, st.consecutive_clean_days,
               st.peerflood_total, "ДА" if st.manual_only else "нет",
               lk.locked_until or "—",
               "активен" if policy.kill_switch_active() else "снят",
               ambiguous, failed, pending_outbox, failed_tasks,
               log_rows or "<tr><td colspan=4>пусто</td></tr>"))
    return _layout(body)


@app.get("/ping")
def ping():
    """Лёгкая проба живости для healthcheck контейнера.

    Отдельно от /health: тот делает два десятка запросов к БД и держать его
    под опросом раз в минуту — лишняя нагрузка на общую SQLite.
    """
    from fastapi.responses import PlainTextResponse

    from .. import health as hb
    hb.beat("web")
    return PlainTextResponse("ok")


def main():
    import uvicorn
    s = get_settings()
    print("Дашборд: http://%s:%d" % (s.web_host, s.web_port))
    uvicorn.run(app, host=s.web_host, port=s.web_port, log_level="warning")


if __name__ == "__main__":
    main()
