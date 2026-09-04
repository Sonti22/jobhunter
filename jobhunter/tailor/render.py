"""Рендер подогнанного резюме в PDF (reportlab, кириллица через DejaVu).

Дизайн наследует build_cv.py. Каждый файл получает уникальный CreationDate —
иначе Telegram дедуплицирует одинаковые байты при отправке разным адресатам.
"""
import hashlib
import os
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# Где искать DejaVu. Путь был захардкожен под Windows, и в контейнере
# генерация резюме падала целиком — а вместе с ней весь конвейер подготовки
# откликов. Отказ был тихим: заявка просто не доходила до очереди.
_FONT_DIRS = (
    r"C:\Windows\Fonts",                        # Windows
    "/usr/share/fonts/truetype/dejavu",          # Debian/Ubuntu
    "/usr/share/fonts/dejavu",                   # Fedora/Alpine
    "/usr/local/share/fonts",
    "/Library/Fonts",                            # macOS
)
_FONT_FILES = {"DJ": "DejaVuSans.ttf", "DJ-B": "DejaVuSans-Bold.ttf",
               "DJ-I": "DejaVuSans-Oblique.ttf"}
_REG = False


class FontsMissing(RuntimeError):
    """Нет шрифта с кириллицей — резюме собрать нечем."""


def _font_path(filename: str) -> str:
    for folder in _FONT_DIRS:
        candidate = os.path.join(folder, filename)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _register():
    global _REG
    if _REG:
        return
    # Обязателен только обычный шрифт: без него кириллица не наберётся вовсе.
    # Жирный и курсив — оформление; ронять из-за них подготовку всех откликов
    # неверно, тем более что курсив живёт в отдельном пакете (dejavu-extra) и
    # его нет в базовой поставке.
    regular = _font_path(_FONT_FILES["DJ"])
    if not regular:
        raise FontsMissing(
            "не найден %s. Поставь пакет со шрифтами DejaVu "
            "(в Debian: fonts-dejavu-core) или положи файл в один из: %s"
            % (_FONT_FILES["DJ"], ", ".join(_FONT_DIRS)))
    pdfmetrics.registerFont(TTFont("DJ", regular))
    for name in ("DJ-B", "DJ-I"):
        path = _font_path(_FONT_FILES[name]) or regular
        pdfmetrics.registerFont(TTFont(name, path))
    pdfmetrics.registerFontFamily("DJ", normal="DJ", bold="DJ-B", italic="DJ-I")
    _REG = True


INK = colors.HexColor("#14181f")
MUTED = colors.HexColor("#5b6472")
ACCENT = colors.HexColor("#1f4e79")
RULE = colors.HexColor("#c3ccd8")
W = A4[0] - 32 * mm

_MONTHS_RU = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_MONTHS_EN = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_period(start, end, lang):
    mon = _MONTHS_RU if lang == "ru" else _MONTHS_EN
    now = "н. в." if lang == "ru" else "present"

    def one(d):
        y, m = d.split("-")
        return "%s %s" % (mon[int(m)], y)

    return "%s — %s" % (one(start), one(end) if end else now)


def _styles():
    return {
        "name": ParagraphStyle("name", fontName="DJ-B", fontSize=19, leading=22, textColor=INK, spaceAfter=2),
        "role": ParagraphStyle("role", fontName="DJ", fontSize=11.2, leading=14, textColor=ACCENT, spaceAfter=6),
        "meta": ParagraphStyle("meta", fontName="DJ", fontSize=8.2, leading=11.4, textColor=MUTED),
        "h2": ParagraphStyle("h2", fontName="DJ-B", fontSize=9.8, leading=12, textColor=ACCENT, spaceBefore=2, spaceAfter=2),
        "body": ParagraphStyle("body", fontName="DJ", fontSize=8.6, leading=11.8, textColor=INK, alignment=TA_JUSTIFY),
        "jobtitle": ParagraphStyle("jobtitle", fontName="DJ-B", fontSize=9.2, leading=11.8, textColor=INK),
        "dates": ParagraphStyle("dates", fontName="DJ", fontSize=8.3, leading=12, textColor=MUTED, alignment=TA_RIGHT),
        "bullet": ParagraphStyle("bullet", fontName="DJ", fontSize=8.5, leading=11.4, textColor=INK, leftIndent=9, bulletIndent=1, spaceAfter=1.3),
        "skill": ParagraphStyle("skill", fontName="DJ", fontSize=8.5, leading=11.6, textColor=INK),
    }


def _section(S, title):
    return [Spacer(1, 5), Paragraph(title.upper(), S["h2"]),
            HRFlowable(width="100%", thickness=0.7, color=RULE, spaceBefore=1, spaceAfter=3)]


def _job_header(S, title, dates):
    t = Table([[Paragraph(title, S["jobtitle"]), Paragraph(dates, S["dates"])]],
              colWidths=[W * 0.72, W * 0.28])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return t


def _safe(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("_")[:60]


def render_cv(data: dict, out_dir: str, filename_hint: str = "",
              unique_seed: str = "") -> tuple:
    """Строит PDF. Возвращает (path, sha256)."""
    _register()
    S = _styles()
    lang = data["lang"]
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    t_summary = "О себе" if lang == "ru" else "Summary"
    t_exp = "Опыт работы" if lang == "ru" else "Experience"
    t_skills = "Навыки" if lang == "ru" else "Skills"
    t_edu = "Образование" if lang == "ru" else "Education"
    t_lang = "Языки" if lang == "ru" else "Languages"

    base = _slug(filename_hint or data["name"])
    fname = "%s_%s.pdf" % (base, (lang or "ru"))
    path = os.path.join(out_dir, fname)

    story = [Paragraph(_safe(data["name"]), S["name"]),
             Paragraph(_safe(data["headline"]), S["role"]),
             HRFlowable(width="100%", thickness=1.1, color=ACCENT, spaceAfter=5),
             Paragraph(_safe(data["contacts"]), S["meta"]),
             Paragraph("%s: %s" % (t_lang, _safe(data["languages"])), S["meta"])]

    story += _section(S, t_summary)
    story.append(Paragraph(_safe(data["summary"]), S["body"]))

    story += _section(S, t_exp)
    for j in data["jobs"]:
        head = "%s — %s" % (_safe(j["company"]), _safe(j["role"]))
        flow = [_job_header(S, head, _fmt_period(j["start"], j["end"], lang))]
        for b in j["bullets"]:
            flow.append(Paragraph(_safe(b), S["bullet"], bulletText="\u2013"))
        flow.append(Spacer(1, 4))
        story += [KeepTogether(flow[:2])] + flow[2:]

    story += _section(S, t_skills)
    story.append(Paragraph("  •  ".join(_safe(x) for x in data["skills"]), S["skill"]))

    story += _section(S, t_edu)
    story.append(Paragraph(_safe(data["education"]), S["body"]))

    # уникальные метаданные — чтобы байты (и хеш) отличались между адресатами
    seed = unique_seed or filename_hint or data["name"]
    stamp = "D:2026%s" % (int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 10**10)

    doc = BaseDocTemplate(path, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                          topMargin=12 * mm, bottomMargin=12 * mm,
                          title="%s — %s — %s" % (data["name"], data["headline"], stamp),
                          author=data["name"], subject=data["headline"],
                          creator="jobhunter", producer="jobhunter")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")

    def deco(canvas, _doc):
        canvas.saveState()
        canvas.setFont("DJ", 7.0)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 16 * mm, 7 * mm,
                               "%s  •  %s  •  %d" % (data["name"], data["headline"], _doc.page))
        canvas.setCreator("jobhunter")
        canvas.restoreState()

    doc.addPageTemplates([PageTemplate(id="all", frames=[frame], onPage=deco)])
    doc.build(story)

    raw = Path(path).read_bytes()
    return path, hashlib.sha256(raw).hexdigest()


# ── проверка машиночитаемости ──
# «ATS-оптимизация» на 90% — маркетинг: миф «75% резюме отсеивает робот» восходит
# к вендору Preptel, закрывшемуся в 2013 без единого исследования, а по опросу
# HR.com 2026 авто-отсев по содержанию настроен лишь у ~8% внедрений.
# Проверяемо ровно одно: извлекается ли из PDF текст и не рассыпался ли он.
# Это и проверяем — остальное не реализуем.

REQUIRED_FIELDS = ("email", "phone", "name")


def verify_parsable(pdf_path: str, data: dict) -> dict:
    """Извлекает текст обратно из PDF и проверяет, что ключевое уцелело."""
    result = {"ok": True, "problems": [], "chars": 0}
    try:
        from pypdf import PdfReader
    except ImportError:
        result["problems"].append("pypdf не установлен — проверка пропущена")
        return result

    try:
        text = "\n".join((p.extract_text() or "") for p in PdfReader(pdf_path).pages)
    except Exception as e:
        result["ok"] = False
        result["problems"].append("PDF не читается: %s" % e)
        return result

    result["chars"] = len(text)
    flat = re.sub(r"\s+", " ", text).lower()

    name = (data.get("name") or "").split()[0].lower() if data.get("name") else ""
    if name and name not in flat:
        result["problems"].append("имя не извлекается из PDF")
    contacts = (data.get("contacts") or "")
    for token in re.findall(r"[\w.+-]+@[\w.-]+|\+\d[\d ()-]{8,}", contacts):
        probe = re.sub(r"[ ()-]", "", token).lower()
        flat_digits = re.sub(r"[ ()-]", "", flat)
        if probe not in flat_digits:
            result["problems"].append("контакт не извлекается: %s" % token[:24])

    # заголовки секций должны присутствовать — иначе парсер не увидит структуру
    heads = ["опыт", "навык", "образован"] if data.get("lang") == "ru" \
        else ["experience", "skills", "education"]
    missing = [h for h in heads if h not in flat]
    if missing:
        result["problems"].append("не видны разделы: %s" % ", ".join(missing))

    # текст должен составлять заметную долю — иначе он ушёл в картинки
    expected = sum(len(b) for j in data.get("jobs", []) for b in j.get("bullets", []))
    if expected and result["chars"] < expected * 0.7:
        result["problems"].append("извлеклось %d знаков при ожидаемых ~%d"
                                  % (result["chars"], expected))

    result["ok"] = not result["problems"]
    return result


def resolve_cv(cv_path: str) -> str:
    """Пригодный к отправке путь к резюме, или пустая строка.

    Зачем: путь записывается в заявку при подготовке и живёт в базе месяцами.
    Между подготовкой и отправкой корень проекта может смениться — именно это
    случилось при переезде в Docker, где в заявках остались пути от корня
    диска Windows, невидимые изнутри контейнера. Проверка exists() тихо
    давала False, и отклик ушёл бы БЕЗ резюме, не сообщив об этом никак.

    Порядок поиска: сам путь, затем файл с тем же именем в текущем каталоге
    резюме, затем базовое резюме из настроек.
    """
    from pathlib import Path

    from ..config import get_settings

    s = get_settings()
    if cv_path and Path(cv_path).exists():
        return cv_path
    if cv_path:
        name = Path(cv_path.replace(chr(92), "/")).name
        folders = [s.cv_out]
        if s.base_cv_path:
            folders.append(str(Path(s.base_cv_path).parent))
        for folder in folders:
            if folder and (Path(folder) / name).exists():
                return str(Path(folder) / name)
    base = Path(s.base_cv_path) if s.base_cv_path else None
    return str(base) if base and base.exists() else ""
