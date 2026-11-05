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


# ── «Реализовано за 7д» — коротко (05.11: 18 строк в статусе, нечитаемо) ────

def test_realized_block_top3_and_rest():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    pos = [{"status": "CLOSED", "title": f"Market {i}", "realized_pnl": pnl,
            "last_event_at": int(now.timestamp()) - 3600}
           for i, pnl in enumerate([-54, 21, 19, 2, -3, 7, -4, 16])]
    block = ds.realized_block(pos, now=now)
    assert "$+4" in block and "(8 позиц." in block
    lines = [l for l in block.splitlines() if l.startswith(("🟢", "🔴"))]
    assert len(lines) == 3 and "Market 0" in lines[0]          # крупнейшие по модулю
    assert "ещё 5" in block
