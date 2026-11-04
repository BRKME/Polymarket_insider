"""Журнальный фолбэк дневного статуса был слеп к стороне позиции (04.11.2026).

Когда Data API v1 выключили, статус 04.11 16:32 МСК ушёл в журнальный путь и
показал «Vučić YES 52¢→97¢ +86% — можно зафиксировать»: для YES-строки брал
вход YES, а текущую цену — NO (97¢). Реально YES стоил ~3¢, позиция продана
в минус. Призыв «зафиксировать» по выдуманной прибыли.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_status as ds


def _row(side, entry):
    return {"condition_id": "v", "question": "Will Aleksandar Vučić be the next Prime Minister of Serbia?",
            "side": side, "status": "open", "fill_source": "onchain",
            "entry_price_actual": entry, "stake_actual": 25.5, "horizon_days": 30}


def test_yes_position_uses_yes_price():
    msg = ds.build_daily_status([_row("YES", 0.52)], price_fn=lambda cid: 0.97)  # NO = 97¢
    assert "+86%" not in msg and "зафиксировать" not in msg
    assert "в плюсе 1" not in msg          # 3¢ — обвал/мусор, но не прибыль


def test_yes_position_gain_counted_correctly():
    msg = ds.build_daily_status([_row("YES", 0.52)], price_fn=lambda cid: 0.30)  # YES = 70¢
    assert "в плюсе 1" in msg and "+35%" in msg


def test_no_position_unchanged():
    msg = ds.build_daily_status([_row("NO", 0.52)], price_fn=lambda cid: 0.97)
    assert "в плюсе 1" in msg
