"""Перегонка очереди через боевой конвейер после смены правил.

Когда меняются правила отбора или текст письма, накопленная очередь остаётся
собранной по старым правилам. Дописывать её на месте нельзя: пришлось бы
завести вторую копию логики скоринга, гейта и проверки качества, а две копии
однажды разойдутся — и разойдутся молча, потому что письмо всё равно уйдёт.

Поэтому заявки возвращаются в DISCOVERED и проходят обычный prepare заново.
Тот сам отсеет офисные вакансии по новому правилу, перегенерирует письма и
заново проверит их гейтом. Переход PENDING_APPROVAL → DISCOVERED в графе
состояний разрешён именно для этого.

Уже отправленные заявки не трогаются: письмо ушло, переписывать нечего.

    python -m jobhunter.repair_queue --dry
    python -m jobhunter.repair_queue
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy import select

from .db import session_scope
from .models import Application, ContactKind, Job, Status, utcnow

# Ровно те статусы, где письмо ещё не ушло. SENDING сюда не входит: заявка
# может быть в руках отправителя прямо сейчас.
RESETTABLE = (Status.PENDING_APPROVAL.value, Status.APPROVED.value)


def reset_email_language(dry: bool = True) -> dict:
    """Вернуть на пересборку несравнимые по языку email-заявки.

    Отправленные письма не трогаются: сменить язык задним числом невозможно.
    В работу возвращаются только несостоявшиеся PENDING/APPROVED-заявки, у
    которых сохранённый cv_lang расходится с текущим _pick_lang().
    """
    from .tailor.select import _pick_lang

    stats = Counter()
    with session_scope() as sess:
        rows = sess.scalars(
            select(Application).where(
                Application.status.in_(RESETTABLE),
                Application.sent_at.is_(None))).all()
        ids = [a.id for a in rows]

    for app_id in ids:
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            if (app is None or app.status not in RESETTABLE
                    or app.sent_at is not None):
                stats["пропущено (статус изменился)"] += 1
                continue
            job = sess.get(Job, app.job_id)
            if not job or job.contact_kind != ContactKind.EMAIL.value:
                continue
            expected = _pick_lang(job.description_raw or "", job.title or "")
            if (app.cv_lang or "ru") == expected:
                continue
            stats["найдено"] += 1
            if dry:
                continue
            if app.status == Status.APPROVED.value:
                if not app.advance(Status.PENDING_APPROVAL,
                                   reason="исправление языка письма"):
                    stats["пропущено (переход запрещён)"] += 1
                    continue
            if not app.advance(Status.DISCOVERED,
                               reason="пересборка письма на языке вакансии"):
                stats["пропущено (переход запрещён)"] += 1
                continue
            # Не оставляем старый PDF/текст рядом с новой заявкой: pipeline
            # построит их заново и запишет актуальный язык и тему.
            app.gate_passed = False
            app.gate_failures_json = []
            app.cv_path = ""
            app.cv_sha256 = ""
            app.cv_lang = ""
            app.message_body = ""
            app.message_body_norm_hash = ""
            app.message_similarity_max = 0.0
            app.message_skeleton_id = ""
            app.email_subject = ""
            app.updated_at = utcnow()
            stats["сброшено"] += 1
    return dict(stats)


def reset(dry: bool = True) -> dict:
    stats = Counter()
    with session_scope() as sess:
        # sent_at IS NULL — не украшение, а суть: без него пересборка
        # откатывала УЖЕ ОТПРАВЛЕННЫЕ заявки в дозаявочный статус. Так
        # ушли 44 штуки: они выпали из списка живых (convo/engine.py), и
        # ответы рекрутёров по ним перестали читаться вовсе, а часть
        # встала в очередь на повторную отправку тому же человеку.
        ids = [a.id for a in sess.scalars(
            select(Application).where(Application.status.in_(RESETTABLE),
                                      Application.sent_at.is_(None)))]

    for app_id in ids:
        with session_scope() as sess:
            app = sess.get(Application, app_id)
            # Перепроверка внутри транзакции обязательна: между выборкой ids
            # и этой сессией отправитель мог взять заявку в SENDING или уже
            # отправить. Без перепроверки reset() утаскивал такую заявку в
            # DISCOVERED посреди отправки — письмо уходило, а заявка
            # готовилась к повторной отправке тому же человеку.
            if (app is None or app.status not in RESETTABLE
                    or app.sent_at is not None):
                stats["пропущено (успела уйти)"] += 1
                continue
            stats["всего"] += 1
            if dry:
                continue
            # APPROVED → DISCOVERED напрямую граф не разрешает: одобрение
            # снимается через возврат в очередь, и это правильно —
            # «одобрено» не должно исчезать одним шагом.
            if app.status == Status.APPROVED.value:
                if not app.advance(Status.PENDING_APPROVAL,
                                   reason="пересборка очереди"):
                    stats["пропущено (переход запрещён)"] += 1
                    continue
            if not app.advance(Status.DISCOVERED,
                               reason="изменились правила отбора"):
                # Статус не сменился — трогать gate-поля нельзя: у живой
                # SENT-заявки сломался бы follow-up на проверке gate_passed.
                stats["пропущено (переход запрещён)"] += 1
                continue
            app.gate_passed = False
            app.gate_failures_json = []
            app.updated_at = utcnow()
            stats["сброшено"] += 1
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description="Пересобрать очередь откликов")
    ap.add_argument("--dry", action="store_true", help="только показать")
    ap.add_argument("--email-language", action="store_true",
                    help="найти заявки email с устаревшим языком")
    ap.add_argument("--apply", action="store_true",
                    help="применить исправление языка")
    args = ap.parse_args()

    if args.email_language:
        stats = reset_email_language(dry=not args.apply)
        print("email-заявок с неверным языком: %d" % stats.get("найдено", 0))
        if args.apply:
            print("возвращено в DISCOVERED: %d" % stats.get("сброшено", 0))
        else:
            print("(сухой прогон, база не менялась; добавь --apply)")
        return 0

    stats = reset(dry=args.dry)
    print("в очереди: %d" % stats.get("всего", 0))
    if args.dry:
        print("(сухой прогон, база не менялась)")
        return 0
    print("возвращено в DISCOVERED: %d" % stats.get("сброшено", 0))

    from .pipeline import prepare_all_discovered
    print("\nпересборка...")
    res = prepare_all_discovered()
    for k, v in sorted(res.items(), key=lambda kv: -kv[1]):
        print("  %-20s %d" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
