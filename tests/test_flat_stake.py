"""Ставка оператора фиксирована: $30 на всё (решение 04.10.2026).

Банк (BANKROLL) и кап категории убраны: оператор не размеряет ставки от банка,
а проценты «от банка $200» при ~$310 в позициях только вводили в заблуждение.
Экспозиция остаётся видна в долларах и числе ставок — без «⚠️ЛИМИТ».
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import category_exposure as cx
import config
import event_scanner as es
import scan_events as se
import yes_strategy as ys


def _cand(price=0.52, side="YES"):
    return es.Candidate(
        question="Will the Republicans win the Georgia governor race in 2026?",
        condition_id="0xG", market_yes_price=price, no_price=1 - price,
        ai_yes_estimate=0.6, ai_yes_raw=0.6, edge=0.08, liquidity=110_000,
        end_date="2026-11-04T04:59:00Z", reasoning="x", ai_conf="medium",
        event_slug="georgia-governor-winner-2026", side=side)


def _rows(n, stake=30.0):
    return [{"question": f"Will the Democrats win the X{i} governor race?",
             "condition_id": f"c{i}", "status": "open", "stake_actual": stake}
            for i in range(n)]


def test_config_has_no_bankroll():
    assert not hasattr(config, "BANKROLL")
    assert not hasattr(config, "CATEGORY_EXPOSURE_CAP")
    assert config.OPERATOR_STAKE == 30.0


def test_alert_shows_flat_stake(monkeypatch):
    monkeypatch.setattr(se, "_load_journal_rows", lambda: [])
    msg = se._format_alert(_cand())
    assert "ставка $30" in msg
    assert "размер ~$" not in msg


def test_alert_exposure_in_dollars_and_count(monkeypatch):
    monkeypatch.setattr(se, "_load_journal_rows", lambda: _rows(6))
    msg = se._format_alert(_cand())
    assert "в elections уже открыто $180 (6 ставок)" in msg
    assert "банка" not in msg and "ЛИМИТ" not in msg and "BANKROLL" not in msg


def test_above_breakeven_warned(monkeypatch):
    # при WR 62% вход дороже 62¢ имеет отрицательное матожидание
    monkeypatch.setattr(se, "_load_journal_rows", lambda: [])
    assert "дороже безубытка" in se._format_alert(_cand(price=0.64))
    assert "дороже безубытка" not in se._format_alert(_cand(price=0.55))


def test_breakeven_helper():
    assert ys.above_breakeven(0.64) is True
    assert ys.above_breakeven(0.62) is False
    assert ys.above_breakeven(None) is False


def test_format_exposure_without_bankroll():
    line = cx.format_exposure({"elections": 180.0, "crypto": 30.0},
                              counts={"elections": 6, "crypto": 1})
    assert line == "экспозиция: elections $180 (6) · crypto $30 (1)"


def test_exposure_counts():
    assert cx.exposure_counts(_rows(3)) == {"elections": 3}
