"""Нормализация, хеши, похожесть. Ноль внешних зависимостей."""
import hashlib
import re
from difflib import SequenceMatcher
from html.parser import HTMLParser

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_DIGIT = re.compile(r"\d+")

# Числовой токен: 40%, 2x, 7 лет, 250 т.р., 2.5
NUMBER = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|x|к|k|m|млн|тыс|т\.р\.|лет|год[а-я]*|"
                    r"years?|мес[а-я]*|months?)?", re.IGNORECASE)


def norm(text: str) -> str:
    """lowercase, схлопнуть пробелы, убрать пунктуацию и цифры."""
    t = (text or "").lower()
    t = _PUNCT.sub(" ", t)
    t = _DIGIT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def norm_keep_digits(text: str) -> str:
    """Как norm, но цифры остаются (для дедупа заголовков)."""
    t = (text or "").lower()
    t = _PUNCT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def norm_hash(text: str) -> str:
    """Хеш нормализованного текста — для дедупа сообщений/описаний."""
    return sha256(norm(text))


def similarity(a: str, b: str) -> float:
    """Похожесть нормализованных текстов, 0..1."""
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def max_similarity(text: str, corpus) -> float:
    """Максимальная похожесть text к любому из corpus."""
    n = norm(text)
    best = 0.0
    for other in corpus:
        r = SequenceMatcher(None, n, norm(other)).ratio()
        if r > best:
            best = r
    return best


def numbers(text: str) -> list:
    """Все числовые токены строкой, как встречены."""
    return [m.group(0).strip() for m in NUMBER.finditer(text or "") if m.group(0).strip()]


def bare_numbers(text: str) -> set:
    """Только числовые значения (без единиц), нормализованные: {'40','2','7'}."""
    out = set()
    for tok in re.findall(r"\d+(?:[.,]\d+)?", text or ""):
        out.add(tok.replace(",", "."))
    return out


# ── Очистка входящего письма ─────────────────────────────────────────────
#
# Зачем это здесь, а не «когда-нибудь потом». Письмо приходит вместе с
# процитированной историей переписки, и classify.py разбирает текст
# регулярками, не отличая цитату от нового текста. Сценарий разворачивается
# сам: наш автоответ на «когда созвонимся» содержит предложенные слоты
# («пн 26.08 в 11:00 (UTC+3)»), рекрутёр отвечает «Спасибо, посмотрю» с
# цитатой, классификатор видит время В ЦИТАТЕ и ставит SLOT_PROPOSED,
# движок заводит владельцу карточку со слотами, которых рекрутёр не
# предлагал, владелец жмёт «подтвердить» — и бот отправляет живому человеку
# подтверждение интервью, которого никто не назначал.
#
# Второй путь тише, но глушит автоматику целиком: корпоративный дисклеймер
# в подвале ловится как технический вопрос или разговор о деньгах, и каждое
# письмо из компании с длинным футером уходит в «нужен человек».

# Строка-врезка перед цитатой. Форм много и они разные у каждого клиента:
#   «On Tue, 25 Aug 2026 at 10:12, X <a@b> wrote:»
#   «вт, 26 авг. 2026 г. в 10:12, Suren <s@g.com> написал:»
#   «В пн, 25 авг. 2026 г., Иван Петров писал:»
# Общее у всех одно: строка кончается глаголом письма и двоеточием. На этом
# и держимся, вместо попытки перечислить формы дат каждого почтовика.
_ON_WROTE = re.compile(
    r"^[ \t>]*.{0,200}?\b(?:"
    r"wrote|написал\w*|писал\w*|пишет|schrieb|écrit|scritto|escribió"
    r")\s*:\s*$", re.I | re.M)

# Разделители блока пересылки.
_FWD_MARK = re.compile(
    r"^[ \t>]*-{0,10}\s*(?:"
    r"Original\s+Message|Forwarded\s+message|Begin\s+forwarded\s+message|"
    r"Исходное\s+сообщение|Пересылаемое\s+сообщение|"
    r"Начало\s+пересылаемого\s+сообщения|Переслано"
    r")\s*-{0,10}\s*$", re.I | re.M)

# Строка шапки Outlook. Одиночная строка «Тема: обсудим оффер» — нормальный
# текст письма, поэтому засчитываем только плотную группу таких строк.
_HDR_LINE = re.compile(
    r"^[ \t>]*(?:От|Кому|Копия|Отправлено|Тема|Дата|"
    r"From|To|Cc|Bcc|Sent|Subject|Date|Reply-To)\s*:\s+\S", re.I)

# Юридические подвалы и футеры рассылок — режем от совпадения до конца.
_DISCLAIMER = re.compile(
    r"(?:^|\n)[ \t>]*(?:"
    r"(?:Данное|Настоящее|Это)\s+(?:письмо|сообщение)\s+и\s+(?:любые\s+)?(?:вложени|приложени)|"
    r"Информация,?\s+содержащаяся\s+в\s+(?:настоящем|данном|этом)\s+(?:письме|сообщении)|"
    r"Настоящее\s+сообщение\s+.{0,40}?\s+конфиденциальн|"
    r"Если\s+вы\s+(?:не\s+являетесь\s+(?:его\s+)?(?:адресатом|получателем)|"
    r"получили\s+это\s+письмо\s+по\s+ошибке)|"
    r"This\s+(?:e-?mail|message)\s+(?:and\s+any\s+(?:attachments|files)\s+)?"
    r"(?:is|are|may\s+be)\s+(?:confidential|intended|privileged)|"
    r"CONFIDENTIALITY\s+(?:NOTICE|STATEMENT)|DISCLAIMER\s*:|"
    r"If\s+you\s+(?:are\s+not\s+the\s+intended\s+recipient|"
    r"have\s+received\s+this\s+.{0,20}?in\s+error)|"
    r"(?:Please|Пожалуйста,?)\s+(?:consider\s+the\s+environment|подумайте\s+об\s+экологии)|"
    r"Отписаться\s+от\s+рассылки|Unsubscribe|View\s+this\s+email\s+in\s+your\s+browser"
    r")", re.I)

_SIG_DELIM = re.compile(r"^[ \t]*(?:--\s*|—+|_{2,}|-{3,}|\*{3,})[ \t]*$", re.M)
_SIG_PHRASE = re.compile(
    r"^[ \t]*(?:С\s+уважением|Всего\s+доброго|Хорошего\s+дня|"
    r"Best\s+regards|Kind\s+regards|Warm\s+regards|Regards|Sincerely|Cheers|BR)"
    r"\s*[,!.]?\s*$", re.I | re.M)

_QUOTE_LINE = re.compile(r"^[ \t]*>+")
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


class _Stripper(HTMLParser):
    """HTML → текст. Цитаты в письмах размечаются тегами, а не символом «>»,
    поэтому blockquote и контейнеры цитат Gmail/Outlook выбрасываются целиком
    вместе с содержимым — иначе процитированная история доедет до
    классификатора в чистом виде."""

    DROP = {"script", "style", "head", "blockquote"}
    DROP_ATTR = {"gmail_quote", "gmail_quote_container", "moz-cite-prefix",
                 "appendonsend", "divrplyfwdmsg"}
    BREAK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.depth = 0            # глубина внутри выбрасываемого поддерева

    def _is_quote(self, attrs) -> bool:
        for key, val in attrs:
            if key in ("class", "id") and val:
                if any(x in val.lower() for x in self.DROP_ATTR):
                    return True
        return False

    def handle_starttag(self, tag, attrs):
        if self.depth or tag in self.DROP or self._is_quote(attrs):
            self.depth += 1
            return
        if tag in self.BREAK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            return
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(html: str) -> str:
    """Текст из HTML-письма. Ноль внешних зависимостей."""
    parser = _Stripper()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # Битая разметка встречается регулярно; отдаём, что успели разобрать.
        pass
    out = parser.text()
    return re.sub(r"[ \t]{2,}", " ", out)


def _first_hit(text: str, *patterns) -> int:
    """Самая ранняя позиция среди совпадений. -1 — ни одного."""
    best = -1
    for pat in patterns:
        m = pat.search(text)
        if m and (best < 0 or m.start() < best):
            best = m.start()
    return best


def _outlook_header_block(text: str) -> int:
    """Начало плотной группы строк-заголовков (3+ в окне из 6). -1 — нет.

    Возвращается позиция ПЕРВОЙ строки-заголовка в группе, а не начало окна:
    иначе обрезка съедала бы полезный текст, стоящий выше в том же окне, — и
    письмо «Вакансия актуальна» с шапкой цитаты ниже превращалось в пустоту.
    """
    lines = text.split("\n")
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    for i in range(len(lines)):
        window = [j for j in range(i, min(i + 6, len(lines)))
                  if _HDR_LINE.match(lines[j])]
        if len(window) >= 3:
            return offsets[window[0]]
    return -1


def strip_quoted_reply(text: str) -> str:
    """Отрезает процитированную историю переписки."""
    t = (text or "").replace("\r\n", "\n")
    cut = _first_hit(t, _ON_WROTE, _FWD_MARK)
    hdr = _outlook_header_block(t)
    if hdr >= 0 and (cut < 0 or hdr < cut):
        cut = hdr
    if cut >= 0:
        t = t[:cut]
    # Хвост из строк с «>» — то же самое, но без словесного маркера.
    lines = t.split("\n")
    while lines and (not lines[-1].strip() or _QUOTE_LINE.match(lines[-1])):
        lines.pop()
    return "\n".join(lines)


def strip_signature(text: str) -> str:
    """Отрезает подпись и юридический подвал.

    Осторожно: письмо целиком из «Спасибо!» не должно превратиться в пустоту,
    поэтому режем, только если до подписи остаётся осмысленный текст, а сам
    хвост похож на подпись — короткий.
    """
    t = text or ""
    cut = _first_hit(t, _DISCLAIMER)
    for pat in (_SIG_DELIM, _SIG_PHRASE):
        m = pat.search(t)
        if not m:
            continue
        head, tail = t[:m.start()], t[m.start():]
        head_lines = [x for x in head.split("\n") if x.strip()]
        tail_lines = [x for x in tail.split("\n") if x.strip()]
        # Хотя бы одна содержательная строка до подписи — иначе письмо
        # целиком из «Спасибо!» с подписью обратилось бы в пустоту. И хвост
        # должен быть похож на подпись: короткий, а не второе письмо.
        if head_lines and len(tail_lines) <= 10 and len(tail) <= 500:
            if cut < 0 or m.start() < cut:
                cut = m.start()
    return t[:cut] if cut >= 0 else t


def clean_email_body(text: str, is_html: bool = False) -> str:
    """Текст письма без цитат, подписей и невидимых символов.

    Пустая строка означает «нового текста нет» — например, ответ состоит
    из одной цитаты. Вызывающий код в этом случае письмо сохраняет (факт
    ответа важен), но классификатору не отдаёт.
    """
    t = html_to_text(text) if is_html else (text or "")
    t = _INVISIBLE.sub("", t).replace("\xa0", " ").replace("\r\n", "\n")
    t = strip_signature(strip_quoted_reply(t))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return "" if len(t.replace(" ", "")) < 2 else t
