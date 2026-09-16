"""Сквозной конвейер подготовки отклика: одна вакансия → резюме + письмо + гейт.

Ставит заявку в статус:
  REJECTED_SCORE     — вакансия не подходит (misfit / низкий скор)
  GATE_FAILED        — резюме или письмо не прошли анти-фабрикация гейт
  PENDING_APPROVAL   — готово к ручному аппруву и отправке

НЕ отправляет ничего. Отправка — отдельный слой (outreach), после аппрува.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .match.explain import MAIN_TRACKS, explain_job
from .match.role import classify
from .match.scorer import score_job
from .models import Application, Job, SendLog, Status
from .tailor.llm_writer import quality_problem, write_message
from .tailor.message import generate as gen_message
from .tailor.message import source_label
from .tailor.render import render_cv, verify_parsable
from .tailor.select import tailor

CV_TEMPLATE_VERSION = "2026-09-04"
MESSAGE_PROMPT_VERSION = "2026-09-04"


@dataclass
class Prepared:
    application_id: int
    status: str
    score: float
    cv_path: str = ""
    message: str = ""
    reason: str = ""


def _uid(external_uuid: str) -> str:
    """Короткий уникальный хвост для имени файла резюме."""
    import hashlib
    return hashlib.sha1((external_uuid or "").encode("utf-8")).hexdigest()[:8]


def _age_days(posted_at) -> int | None:
    """Возраст вакансии в днях. None — дата неизвестна, фильтровать нечем."""
    if not posted_at:
        return None
    from datetime import datetime
    now = int(datetime.now(timezone.utc).timestamp())
    return max(0, (now - int(posted_at)) // 86_400)


def _recent_message_corpus(sess, limit: int = 30) -> list:
    rows = sess.scalars(
        select(Application.message_body)
        .where(Application.message_body != "")
        .order_by(Application.id.desc()).limit(limit)).all()
    return [r for r in rows if r]


def prepare_application(app_id: int,
                        template_preferences: dict | None = None) -> Prepared:
    s = get_settings()
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if app is None:
            raise ValueError("нет заявки %d" % app_id)
        # Preparation must never replace approval/outreach history, including
        # legacy rows reset to DISCOVERED after an attempt recorded only in logs.
        preparable = {Status.DISCOVERED.value, Status.SCORED.value,
                      Status.CONTENT_READY.value, Status.GATE_FAILED.value,
                      Status.HANDLE_MISSING.value, Status.PENDING_APPROVAL.value}
        if (app.status not in preparable or app.approved_at or app.sent_at
                or app.send_attempts or app.send_last_attempt_at or app.applied_at
                or app.sending_lease_until or app.telegram_msg_id
                or sess.scalar(select(SendLog.id).where(
                    SendLog.application_id == app.id).limit(1)) is not None):
            return Prepared(app.id, app.status, app.score, cv_path=app.cv_path,
                            message=app.message_body,
                            reason="история одобрения/отправки сохранена; повторная подготовка пропущена")
        job = sess.get(Job, app.job_id)

        assessment = explain_job(job)
        app.score_breakdown_json = dict(app.score_breakdown_json or {}, assessment=assessment)

        from .outreach.eligibility import vacancy_problem
        problem = vacancy_problem(job)
        if not problem.allowed:
            app.transition(Status.REJECTED_SCORE, reason=problem.reason)
            return Prepared(app.id, app.status, app.score, reason=problem.reason)

        score = score_job(job.title, job.tag, job.description_raw)
        app.score = score.total
        app.score_breakdown_json = dict(
            app.score_breakdown_json or {}, reason=score.reason,
            matched=[t for t, _, _ in score.matched_skills],
            forbidden=score.forbidden_demands, assessment=assessment)

        if not score.recommend:
            app.transition(Status.REJECTED_SCORE,
                           reason=score.reason or "низкий скор/непрофильно")
            return Prepared(app.id, app.status, score.total, reason=app.reject_reason)

        # Протухшие вакансии. posted_at == 0 значит «дату не знаем» — такие
        # пропускаем, иначе отсеются все источники без даты. Ноль как дату
        # трактовать нельзя: это 1970 год и возраст в два с половиной миллиона
        # дней, который наивная проверка «моложе N» молча пропустит.
        age = _age_days(job.posted_at)
        if age is not None and age > s.max_vacancy_age_days:
            app.transition(Status.REJECTED_SCORE,
                           reason="вакансия протухла: опубликована %d дней назад" % age)
            return Prepared(app.id, app.status, score.total, reason=app.reject_reason)

        # ── резюме ──
        res = tailor(job.title, job.tag, job.description_raw)

        # Роль без пресета — базы под неё у кандидата нет. Собирать backend-резюме
        # под фронтенд-вакансию бессмысленно: рекрутёр видит несоответствие сразу
        # («На вакансию дата аналитика ваш профиль не подходит»).
        if res.role is not None and not res.role.supported:
            app.transition(Status.REJECTED_SCORE,
                           reason="нет базы под роль: %s" % res.role.reason())
            return Prepared(app.id, app.status, score.total, reason=app.reject_reason)

        app.score_breakdown_json = dict(app.score_breakdown_json or {},
                                        role=res.role.family if res.role else "",
                                        role_confidence=round(
                                            res.role.confidence, 2) if res.role else 0.0,
                                        role_runner_up=res.role.runner_up if res.role else "")

        if not res.ok:
            app.transition(Status.GATE_FAILED)
            app.gate_passed = False
            app.gate_failures_json = [{"rule": f.rule_id, "term": f.offending,
                                       "detail": f.detail} for f in res.gate.hard]
            return Prepared(app.id, app.status, score.total,
                            reason="CV gate: " + ", ".join(f.rule_id for f in res.gate.hard))

        # Имя файла — по роли резюме, а не по тегу вакансии. Тег приходит от
        # источника (канал python_djangojobs ставит «Python» всему подряд), и
        # раньше PM-резюме уезжало рекрутёру файлом Hakobyan_Python_*.pdf.
        #
        # Хвост — хеш uuid, а не его первые 8 символов: у постов одного канала
        # общий префикс («tg:productjobgo/1», «tg:product_jobs/7» → оба «tg_produ»),
        # из-за чего 16 разных резюме писались в один и тот же файл и затирали
        # друг друга.
        hint = "Hakobyan_%s_%s" % (res.cv_slug, _uid(job.external_uuid))
        cv_path, cv_hash = render_cv(res.render, s.cv_out, filename_hint=hint,
                                     unique_seed=job.external_uuid)

        # PDF должен читаться парсером: имя, контакты, разделы, объём текста.
        # Красивый, но нечитаемый файл = отклик в пустоту.
        parse = verify_parsable(cv_path, res.render)
        if not parse["ok"]:
            app.transition(Status.GATE_FAILED)
            app.cv_path = cv_path
            app.gate_failures_json = [{"rule": "cv.unparsable", "detail": pr}
                                      for pr in parse["problems"]]
            return Prepared(app.id, app.status, score.total,
                            reason="CV не читается: " + "; ".join(parse["problems"]))

        # Владелец может велеть слать одно своё резюме вместо подогнанных.
        # Подгонку при этом не выключаем: на ней держится гейт и проверка
        # парсинга — подменяем только файл, который реально уйдёт рекрутёру.
        #
        # Только для русских вакансий: базовый PDF владельца — русский, а до
        # этой проверки он прикладывался ко ВСЕМ вакансиям без разбора языка,
        # и англоязычный рекрутёр получал русское резюме. EN-вакансии уходят
        # со сгенерированным английским (решение владельца).
        if s.base_cv_path and res.lang == "ru":
            base = Path(s.base_cv_path)
            if base.is_file():
                cv_path = str(base)
                cv_hash = hashlib.sha256(base.read_bytes()).hexdigest()
            else:
                print("  ! BASE_CV_PATH задан, но файла нет: %s" % base)

        review_note = ""
        llm_provider = ""
        # ── письмо ──
        corpus = _recent_message_corpus(sess)
        # Роль — человеческая: заголовок телеграм-поста с хештегами в
        # первой фразе («По вакансии «удаленно #DevOps»») читается как рассылка.
        from .tailor.roletitle import display_role
        role = display_role(job.title or "", job.tag or "", job.description_raw or "", res.lang)
        msg = gen_message(role, job.description_raw, score,
                          seed_str=job.external_uuid, recent_corpus=corpus,
                          source=source_label(job.source, lang=res.lang), lang=res.lang,
                          template_preferences=template_preferences)
        # LLM переписывает шаблон живым текстом; гейт внутри write_message
        # проверяет результат теми же правилами. Не прошло — остаётся шаблон.
        if msg.ok:
            w = write_message(role, job.description_raw, score,
                              source_label(job.source, lang=res.lang), msg.text,
                              lang=res.lang)
            if w.gate_passed and w.text and w.text != msg.text:
                from .textutil import max_similarity, norm_hash
                if max_similarity(w.text, corpus) < 0.75:
                    msg.text = w.text
                    msg.body_hash = norm_hash(w.text)
                    msg.skeleton_id = w.source
            if w.source.startswith("llm:"):
                llm_provider = w.source
            # Самопроверка забраковала текст дважды: отправлять нельзя, но и
            # выбрасывать вакансию не за что — решает владелец.
            if w.review_done and not w.review_ok:
                review_note = w.review_reason or "не прошло самопроверку"

        # Читаемость финального текста — независимо от того, чей он: LLM или
        # шаблона. Гейт проверяет правду, но правдивое косноязычие тратит
        # контакт так же, как выдумка, а второй раз рекрутёру не напишешь.
        bad_quality = quality_problem(msg.text)

        if bad_quality or not msg.ok:
            app.transition(Status.GATE_FAILED)
            app.cv_path = cv_path
            app.cv_sha256 = cv_hash
            app.gate_failures_json = [{"rule": f.rule_id, "term": f.offending}
                                      for f in msg.gate.hard] or \
                                     [{"rule": "message.quality",
                                       "detail": bad_quality or
                                       "sim=%.2f len=%d" % (msg.similarity_max, len(msg.text))}]
            return Prepared(app.id, app.status, score.total,
                            reason=bad_quality or
                            "message not ok (sim=%.2f)" % msg.similarity_max)

        # ── готово к аппруву ──
        app.gate_passed = True
        app.transition(Status.PENDING_APPROVAL, gate_result=res.gate)
        app.gate_failures_json = []
        app.promoted_terms_json = res.gate.promoted_terms
        app.cv_path = cv_path
        app.cv_sha256 = cv_hash
        app.cv_template_version = CV_TEMPLATE_VERSION
        app.message_prompt_version = MESSAGE_PROMPT_VERSION
        app.llm_provider = llm_provider
        app.cv_lang = res.lang
        app.message_body = msg.text
        app.message_body_norm_hash = msg.body_hash
        app.message_similarity_max = msg.similarity_max
        app.message_skeleton_id = msg.skeleton_id
        # Провал самопроверки не отменяет заявку, но снимает её с автопилота:
        # такой текст уходит только с ведома владельца.
        app.review_note = review_note
        return Prepared(app.id, app.status, score.total, cv_path=cv_path,
                        message=msg.text,
                        reason="ready" if not review_note
                        else "ждёт владельца: " + review_note)


def prepare_all_discovered(limit: int | None = None) -> dict:
    """Обрабатывает все заявки в статусе DISCOVERED."""
    # Снимаем статистику один раз на проход. Вызов внутри
    # prepare_application создал бы отдельный запрос на каждую вакансию и
    # замедлил бы утреннюю подготовку ровно там, где важна отзывчивость.
    from .report import template_preferences

    with session_scope() as sess:
        rows = sess.execute(
            select(Application.id, Job.title, Job.tag, Job.description_raw)
            .outerjoin(Job, Application.job_id == Job.id)
            .where(Application.status == Status.DISCOVERED.value)
            .order_by(Application.id)).all()
        # Prioritize the three main tracks before applying a batch limit. Other
        # families stay in the queue and retain their original numerical score.
        rows.sort(key=lambda row: (
            classify(row.title, row.tag, row.description_raw).family not in MAIN_TRACKS,
            row.id))
        ids = [row.id for row in rows[:limit or 10_000]]

    stats = {"processed": 0, "pending": 0, "rejected": 0, "gate_failed": 0}
    preferences = template_preferences()
    for aid in ids:
        p = prepare_application(aid, template_preferences=preferences)
        stats["processed"] += 1
        if p.status == Status.PENDING_APPROVAL.value:
            stats["pending"] += 1
        elif p.status == Status.REJECTED_SCORE.value:
            stats["rejected"] += 1
        elif p.status == Status.GATE_FAILED.value:
            stats["gate_failed"] += 1
    return stats


if __name__ == "__main__":
    import sys
    stats = prepare_all_discovered()
    print("Подготовка заявок:")
    for k, v in stats.items():
        print("  %-12s %d" % (k, v))
    sys.exit(0)
