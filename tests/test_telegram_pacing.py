"""Пауза между холодными сообщениями Telegram — не короче получаса.

До 22.09 gap_seconds() держал медиану в секундах (60-300с): несколько
сообщений внутри одной сессии уходили за пару минут, и сама плотность
пачки — независимо от текста — уже похожа на спам-паттерн для Telegram
(6 PeerFlood к 22.09). Решение владельца 22.09: не быстрее одного
холодного сообщения одному адресату раз в получаса.
"""
import random

from jobhunter.outreach import policy


def test_gap_seconds_never_faster_than_half_hour():
    rng = random.Random(1)
    samples = [policy.gap_seconds(rng) for _ in range(500)]
    assert min(samples) >= 1800.0
    assert max(samples) <= 2700.0 + 30.0


def test_gap_seconds_has_variation_not_a_fixed_interval():
    """Ровно одинаковый интервал — тоже машинный паттерн, риск не ниже пачки."""
    rng = random.Random(2)
    samples = {round(policy.gap_seconds(rng)) for _ in range(50)}
    assert len(samples) > 10
