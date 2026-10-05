"""Ручные позиции (решение оператора 05.10.2026).

Позиции по перевёрнутым алертам (сырой Grok ≤ 0.5) оператор частично оставил:
это ставки по честной рыночной цене, не сигналы системы. Помеченные ручными:
- не получают «ЗАКРОЙ» по перевёрнутому алерту (иначе ежедневный шум);
- получают обычные ценовые сигналы выхода — это реальные деньги;
- не входят в статистику стратегии (side_split).
Сигнал по перевёрнутому алерту для непомеченных — один раз, без повторов.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exit_dedup
import manual_positions as mp
import mark_to_market as mtm


def _pos(cid="m", entry=0.58):
    return {"condition_id": cid, "question": "Will the Democrats win the Maine Senate race in 2026?",
            "side": "YES", "status": "open", "alerted_at": "2026-10-02T03:00:00+00:00",
            "ai_yes_estimate": 0.688, "market_yes_price": entry, "stake_actual": 15.25,
            "fill_source": "onchain", "entry_price_actual": entry}


CALIB = [{"condition_id": "m", "estimated_at": "2026-10-02T02:55:00+00:00",
          "ai_yes_estimate": 0.04}]


def _fetch(price):
    return lambda cid: {"outcomes": '["Yes","No"]', "outcomePrices": f'["{price}","{1-price:.2f}"]'}


def test_registry_has_kept_positions():
    reg = mp.load()
    for cid in ("0x34918035a590555ba1fb1d6a019d766ab6e557d63e7f3140b63b4550eee7f417",   # Texas
                "0x66bbf6d55e0296278858b3147689f3df9259374f158f9f028b608baa322a639c",   # Maine
                "0x707eb5c6df23e32e0b770dd0ccd1245abb7fac0dcfd24d8b68dd99e5ca737180"):  # Nevada
        assert cid in reg
    # Israel×Iran оператор закрывает — не ручная
    assert "0x1c4c6a698f0591ddf0c96e1beb56f8c75112ef849331bf3b0388f14830a7421d" not in reg


def test_manual_flip_not_flagged():
    sig = mtm.scan_open_positions([_pos()], fetch_fn=_fetch(0.58), calib_rows=CALIB,
                                  manual={"m": {}})
    assert sig == []


def test_manual_still_gets_price_exit():
    # ручная позиция, цена выросла до уровня частичной фиксации — сигнал нужен
    sig = mtm.scan_open_positions([_pos(entry=0.55)], fetch_fn=_fetch(0.82),
                                  calib_rows=CALIB, manual={"m": {}})
    assert len(sig) == 1 and sig[0]["action"] != "CLOSE_FLIP"


def test_unmarked_flip_still_flagged():
    sig = mtm.scan_open_positions([_pos()], fetch_fn=_fetch(0.58), calib_rows=CALIB,
                                  manual={})
    assert sig[0]["action"] == "CLOSE_FLIP"


def test_flip_signal_sent_once():
    seen = {}
    t0 = datetime(2026, 10, 5, 19, tzinfo=timezone.utc)
    assert mtm._should_send(seen, "m", "CLOSE_FLIP", t0) is True
    assert mtm._should_send(seen, "m", "CLOSE_FLIP", t0 + timedelta(days=3)) is False
    # обычные сигналы — по-прежнему с суточным повтором
    assert mtm._should_send(seen, "x", "TAKE_PARTIAL", t0) is True
    assert mtm._should_send(seen, "x", "TAKE_PARTIAL", t0 + timedelta(days=2)) is True


def test_flip_wording_not_imperative():
    sig = mtm.scan_open_positions([_pos()], fetch_fn=_fetch(0.58), calib_rows=CALIB,
                                  manual={})
    text = mtm._format_signal(sig[0])
    assert "НЕ СИГНАЛ СИСТЕМЫ" in text
    assert "по текущей цене" in text


def test_side_split_excludes_manual(monkeypatch):
    import side_split as ss
    monkeypatch.setattr(mp, "load", lambda: {"m": {}})
    rows = [dict(_pos(), won=True), dict(_pos(cid="z"), won=True)]
    yes, _ = ss.split_by_side(rows)
    assert [r["condition_id"] for r in yes] == ["z"]
