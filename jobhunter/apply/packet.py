"""Пакет для отклика: всё, что нужно владельцу, кроме нажатия Submit.

Резюме и письмо берутся из существующего конвейера — те же генераторы, что
для автоматических откликов, с тем же анти-фабрикация гейтом. Новое здесь
только сопоставление с полями конкретной формы.

Идемпотентность по отпечатку формы: пока схема не изменилась, пакет не
пересобирается. Форма поменялась — пересобираем и говорим об этом.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from ..config import get_settings
from ..db import session_scope
from ..models import Application, Job, utcnow
from .answers import answer_all
from .forms import fetch_form, form_ref_from_job


@dataclass
class Packet:
    app_id: int
    provider: str
    apply_url: str
    title: str = ""
    company: str = ""
    cv_path: str = ""
    cv_lang: str = "ru"
    letter: str = ""
    answers: list = field(default_factory=list)     # [{field,label,value,source}]
    unresolved: list = field(default_factory=list)  # [{name,label,values}]
    form_hash: str = ""
    supported: bool = True
    note: str = ""


def _letter_for(job, lang: str) -> str:
    """Сопроводительное тем же генератором, что и для автооткликов."""
    from ..match.scorer import score_job
    from ..tailor.message import generate, source_label
    score = score_job(job.title or "", job.tag or "", job.description_raw or "")
    from ..tailor.roletitle import display_role
    role = display_role(job.title or "", job.tag or "", job.description_raw or "", lang)
    msg = generate(role, job.description_raw or "", score,
                   seed_str=job.external_uuid,
                   source=source_label(job.source, lang=lang), lang=lang)
    return msg.text


def build_packet(app_id: int, force: bool = False, http=None) -> Packet | None:
    """Собирает пакет. None — форму прочитать нечем."""
    from ..tailor.select import _pick_lang

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        if app is None:
            return None
        job = sess.get(Job, app.job_id)
        if job is None:
            return None
        cached = dict(app.apply_packet_json or {})
        old_hash = app.apply_form_hash or ""
        cv_path, cv_lang = app.cv_path or "", app.cv_lang or ""
        job_title = job.title or ""
        job_company = job.company_name or ""
        job_desc = job.description_raw or ""
        job_tag = job.tag or ""
        job_uuid = job.external_uuid or ""
        job_source = job.source or ""
        job_url = job.contact_url or ""
        ref = form_ref_from_job(job)

    if ref is None:
        return None
    provider, board, job_id = ref

    spec = fetch_form(provider, board, job_id, http=http)
    if not spec.supported:
        # Ссылка и письмо всё равно полезны: заполнять руками, но с
        # готовым текстом и резюме.
        pkt = Packet(app_id=app_id, provider=provider,
                     apply_url=spec.apply_url or job_url,
                     title=job_title, company=job_company,
                     supported=False, note=spec.note)
    else:
        fresh_hash = spec.fingerprint()
        if not force and cached and old_hash == fresh_hash:
            return Packet(**cached)

        class _J:                       # лёгкий носитель для генераторов
            title, tag, description_raw = job_title, job_tag, job_desc
            external_uuid, source = job_uuid, job_source
        lang = cv_lang or _pick_lang(job_desc, job_title)
        answers, unresolved = answer_all(spec, job=_J)
        pkt = Packet(
            app_id=app_id, provider=provider,
            apply_url=spec.apply_url or job_url,
            title=job_title, company=job_company,
            cv_path=cv_path, cv_lang=lang,
            letter=_letter_for(_J, lang),
            answers=[{"field": a.field, "label": a.label,
                      "value": a.value, "source": a.source} for a in answers],
            unresolved=[{"name": f.name, "label": f.label,
                         "values": f.values[:8]} for f in unresolved],
            form_hash=fresh_hash)

    with session_scope() as sess:
        app = sess.get(Application, app_id)
        app.apply_packet_json = asdict(pkt)
        app.apply_prepared_at = utcnow()
        app.apply_form_hash = pkt.form_hash
    return pkt


def packet_of(app_id: int) -> Packet | None:
    with session_scope() as sess:
        app = sess.get(Application, app_id)
        data = dict(app.apply_packet_json or {}) if app else {}
    return Packet(**data) if data else None


def render(pkt: Packet) -> str:
    """Текст для бота: скопировать и вставить в форму."""
    if not pkt.supported:
        head = ["📝 %s" % (pkt.title or "вакансия")[:60]]
        if pkt.note:
            head.append(pkt.note)
        head += ["", "Форма заполняется вручную: %s" % pkt.apply_url]
        return "\n".join(head)

    lines = ["📝 Анкета: %s" % (pkt.title or "вакансия")[:60]]
    if pkt.company:
        lines.append(pkt.company[:60])
    lines += ["", "ГОТОВЫЕ ОТВЕТЫ:"]
    for a in pkt.answers:
        lines.append("  %s: %s" % (a["label"][:38], a["value"][:60]))
    if pkt.unresolved:
        lines += ["", "❓ НУЖЕН ТВОЙ ОТВЕТ:"]
        for f in pkt.unresolved:
            opts = ", ".join(lbl for _, lbl in (f.get("values") or [])[:4])
            lines.append("  %s%s" % (f["label"][:60],
                                     (" → " + opts[:70]) if opts else ""))
    lines += ["", "Форма: %s" % pkt.apply_url]
    return "\n".join(lines)


def prepare_batch(limit: int | None = None) -> dict:
    """Шаг автопилота: собрать пакеты для лучших вакансий очереди."""
    import httpx
    from sqlalchemy import or_

    from ..manual_apply import MIN_SCORE, OUTCOME_NEW
    from ..models import Status
    s = get_settings()
    limit = limit or s.apply_prep_daily_limit
    stats = {"checked": 0, "built": 0, "skipped": 0}

    # Отбираем по признаку «форму можно прочитать», а не по верху очереди:
    # лучшие вакансии очереди сидят на trudvsem, workable и careered, где
    # схема формы недоступна, и сборка вхолостую перебирала бы их каждый
    # день, ни разу не дойдя до greenhouse.
    with session_scope() as sess:
        rows = [{"id": a.id} for a in sess.scalars(
            select(Application).join(Job, Application.job_id == Job.id)
            .where(Application.status == Status.HANDLE_MISSING.value,
                   Application.outcome == OUTCOME_NEW,
                   Application.score >= MIN_SCORE,
                   or_(Job.contact_url.contains("greenhouse.io"),
                       Job.contact_url.contains("ashbyhq.com")))
            .order_by(Application.score.desc()).limit(limit * 2)).all()]
    with httpx.Client(trust_env=False, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as http:
        for r in rows:
            if stats["built"] >= limit:
                break
            stats["checked"] += 1
            try:
                pkt = build_packet(r["id"], http=http)
            except Exception:                              # noqa: BLE001
                stats["skipped"] += 1
                continue
            if pkt is None or not pkt.supported:
                stats["skipped"] += 1
                continue
            stats["built"] += 1
            import time as _t
            _t.sleep(s.apply_form_fetch_throttle)
    return stats
