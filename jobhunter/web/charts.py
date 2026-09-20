"""Графики страницы статистики: серверный SVG, без JS-библиотек и внешних запросов.

Пульт слушает только localhost и живёт без интернета, поэтому график собирается строкой.
Правила оформления: тонкие столбцы со скруглённым верхом, зазор 2 px между сегментами,
неяркая сетка, подписи чернилами текста (цвет несёт только сам столбец), легенда всегда
при двух и более рядах, у каждого столбца — подсказка, рядом — таблица-дублёр.

Цвета — первые три слота категориальной палитры: именно эта тройка проходит попарную
проверку на различимость, включая дальтонизм. Четвёртый ряд в стопку не добавляем —
отдельный показатель получает отдельный график на той же оси дат.
"""
from __future__ import annotations

import html

SERIES = (("mail", "Отклики почтой", "#2a78d6"),
          ("direct", "Прямые письма", "#eb6834"),
          ("tg", "Telegram", "#1baf7a"))
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e4df", "#ffffff"
W, PAD_L, PAD_R, PAD_T, PAD_B = 920, 34, 8, 10, 22


def _nice_max(value: int) -> int:
    for step in (4, 8, 12, 20, 40, 60, 100, 200, 400, 1000):
        if value <= step:
            return step
    return value


def _top_rounded(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    r = min(r, h, w / 2)
    return ("M%.1f %.1f V%.1f Q%.1f %.1f %.1f %.1f H%.1f Q%.1f %.1f %.1f %.1f V%.1f Z"
            % (x, y + h, y + r, x, y, x + r, y, x + w - r, x + w, y, x + w, y + r, y + h))


def bars(rows: list, keys: tuple, height: int = 170, label: str = "", width: int = W) -> str:
    """Столбцы по дням. keys — ((ключ, подпись, цвет), …); несколько ключей складываются в стопку.

    width — ширина в единицах viewBox: график на полстраницы рисуем в половинной ширине,
    иначе он сжимается вместе с подписями и те становятся нечитаемыми.
    """
    if not rows:
        return "<p class='muted'>ещё нет данных</p>"
    top = _nice_max(max(sum(r[k] for k, _, _ in keys) for r in rows) or 1)
    plot_w, plot_h = width - PAD_L - PAD_R, height - PAD_T - PAD_B
    slot = plot_w / len(rows)
    bar_w = max(4.0, min(22.0, slot - 4))
    out = ["<svg viewBox='0 0 %d %d' role='img' aria-label='%s' style='width:100%%;height:auto;"
           "display:block;background:%s'>" % (width, height, html.escape(label), SURFACE)]
    for frac in (0.0, 0.5, 1.0):
        y = PAD_T + plot_h * (1 - frac)
        out.append("<line x1='%d' x2='%d' y1='%.1f' y2='%.1f' stroke='%s' stroke-width='1'/>"
                   % (PAD_L, width - PAD_R, y, y, GRID))
        out.append("<text x='%d' y='%.1f' font-size='11' fill='%s' text-anchor='end'>%d</text>"
                   % (PAD_L - 6, y + 4, MUTED, round(top * frac)))
    every = max(1, len(rows) // (10 if width >= W else 5))
    for i, r in enumerate(rows):
        x = PAD_L + slot * i + (slot - bar_w) / 2
        base = PAD_T + plot_h
        parts = [(k, name, color, r[k]) for k, name, color in keys if r[k]]
        tip = "%s — %s" % (r["date"].strftime("%d.%m"),
                           ", ".join("%s: %d" % (n, v) for _, n, _, v in parts) or "ничего")
        out.append("<g><title>%s</title>" % html.escape(tip))
        # прозрачная полоса на всю высоту: попасть в тонкий столбец мышью трудно
        out.append("<rect x='%.1f' y='%d' width='%.1f' height='%d' fill='transparent'/>"
                   % (PAD_L + slot * i, PAD_T, slot, plot_h))
        for n, (_, _, color, value) in enumerate(parts):
            h = plot_h * value / top
            y = base - h
            gap = 2.0 if n else 0.0                      # зазор между сегментами стопки
            if n == len(parts) - 1:
                out.append("<path d='%s' fill='%s'/>" % (_top_rounded(x, y, bar_w, max(1.0, h - gap)), color))
            else:
                out.append("<rect x='%.1f' y='%.1f' width='%.1f' height='%.1f' fill='%s'/>"
                           % (x, y, bar_w, max(1.0, h - gap), color))
            base = y
        total = sum(v for *_, v in parts)
        if total and total == max(sum(x_[k] for k, _, _ in keys) for x_ in rows):
            out.append("<text x='%.1f' y='%.1f' font-size='11' fill='%s' text-anchor='middle'>%d</text>"
                       % (x + bar_w / 2, base - 4, INK, total))   # подписан только максимум
        out.append("</g>")
        if i % every == 0 or i == len(rows) - 1:
            out.append("<text x='%.1f' y='%d' font-size='11' fill='%s' text-anchor='middle'>%s</text>"
                       % (x + bar_w / 2, height - 6, MUTED, r["date"].strftime("%d.%m")))
    out.append("</svg>")
    return "".join(out)


def legend(keys: tuple) -> str:
    return "<div style='display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:%s;margin:2px 0 6px'>%s</div>" % (
        MUTED, "".join("<span><span style='display:inline-block;width:10px;height:10px;border-radius:2px;"
                       "background:%s;margin-right:6px'></span>%s</span>" % (c, html.escape(n)) for _, n, c in keys))


def table(rows: list) -> str:
    """Те же числа таблицей: график не должен быть единственным способом их прочитать."""
    body = "".join("<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td></tr>"
                   % (r["date"].strftime("%d.%m"), r["mail"], r["direct"], r["tg"], r["failed"],
                      r["manual"], r["replies"]) for r in reversed(rows))
    return ("<details><summary class='muted'>Те же числа таблицей</summary><table><tr><th>день</th>"
            "<th>почтой</th><th>прямые</th><th>Telegram</th><th>сбои</th><th>вручную</th><th>ответы</th></tr>%s"
            "</table></details>" % body)
