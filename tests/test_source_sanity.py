"""Сторож против управляющих символов в исходниках.

Реальный случай, повторившийся дважды: правка файла через heredoc съедала
обратный слэш, и `\\b` в регулярке превращался в настоящий символ backspace
(0x08). Код при этом остаётся синтаксически верным, тесты проходят, а
регулярка молча перестаёт совпадать.

Что сломалось на практике, пока это не поймали:
  - ingest/recontact.py — признаки контакта «cv», «dm», «тг», «hr» не искались,
    и часть настоящих контактов рекрутёров не восстановилась;
  - match/scorer.py — «sre» не опознавался как профильная роль;
  - tailor/message.py — фильтр сериализованного мусора пропускал null/true/false.

Ни одно из трёх не давало ошибки. Поэтому проверка структурная, а не по
поведению: любой управляющий символ в исходнике — почти наверняка съеденный
escape, и его нужно увидеть сразу.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Таб (0x09), перевод строки (0x0a) и возврат каретки (0x0d) законны.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sources():
    for folder in ("jobhunter", "tests"):
        yield from (ROOT / folder).rglob("*.py")


@pytest.mark.parametrize("path", sorted(_sources()), ids=lambda p: p.name)
def test_no_control_characters(path):
    text = path.read_text(encoding="utf-8")
    bad = []
    for num, line in enumerate(text.splitlines(), 1):
        m = CONTROL.search(line)
        if m:
            bad.append("%s:%d — символ %#04x в %r"
                       % (path.name, num, ord(m.group()), line.strip()[:60]))
    assert not bad, ("управляющие символы в исходнике (съеденный escape?):\n"
                     + "\n".join(bad))


def test_email_not_fabricated_from_prose():
    """«Apply at acme.com» — это фраза, а не адрес (по ней уходили письма)."""
    from jobhunter.ingest.base import extract_email
    for text in ("Learn more at jobhunter.io", "Apply at acme.com/jobs",
                 "See our openings at lever.co", "growing at scale.We hire"):
        assert extract_email(text) == "", text
    assert extract_email("john [at] company [dot] io") == "john@company.io"
    assert extract_email("bob at acme dot com") == "bob@acme.com"
