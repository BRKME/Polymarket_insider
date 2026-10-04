"""Одна точка на рынок в калибровке и Brier.

Баг (найден 04.10.2026): калибровочный журнал пишет строку на КАЖДЫЙ вызов
Grok, а один и тот же рынок переоценивается при каждом сбросе кэша. В таблице
калибровки было n=331 пар при 84 различных рынках; один рынок давал 42 из 139
точек верхней корзины. Порог доверия FULL_TRUST_N=25 выполнялся формально, а
статистика была раздутым знаменателем.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calibration_map as cm


def _row(cid, ts, ai, mkt=0.6):
    return {"condition_id": cid, "estimated_at": ts,
            "ai_yes_estimate": ai, "market_yes_price": mkt}


def test_keeps_earliest_estimate_per_market():
    rows = [_row("a", "2026-09-02", 0.9), _row("a", "2026-09-01", 0.3),
            _row("b", "2026-09-05", 0.5), _row("a", "2026-09-03", 0.8)]
    out = cm.first_per_market(rows)
    assert len(out) == 2
    by = {r["condition_id"]: r for r in out}
    assert by["a"]["ai_yes_estimate"] == 0.3     # самая ранняя оценка
    assert by["b"]["ai_yes_estimate"] == 0.5


def test_journal_rows_use_alerted_at():
    rows = [{"condition_id": "x", "alerted_at": "2026-09-02", "status": "re_alert"},
            {"condition_id": "x", "alerted_at": "2026-09-01", "status": "open"}]
    out = cm.first_per_market(rows)
    assert len(out) == 1 and out[0]["status"] == "open"


def test_rows_without_cid_dropped():
    assert cm.first_per_market([{"estimated_at": "1", "ai_yes_estimate": 0.5}]) == []


def test_calibration_pairs_one_per_market():
    import journal_resolver as jr

    class _Cache:
        _data = {"a": {"outcome": "Yes"}, "b": {"outcome": "No"}}

    rows = [_row("a", f"2026-09-0{i}", 0.9) for i in range(1, 8)] + \
           [_row("b", "2026-09-01", 0.2)]
    pairs = jr.calibration_pairs(rows, _Cache())
    assert len(pairs) == 2


def test_weekly_brier_counts_markets_not_estimates():
    import v5_weekly_status as v5
    from datetime import datetime, timezone
    calib = []
    for k in range(12):          # 12 рынков, каждый переоценён 5 раз
        for i in range(5):
            r = _row(f"m{k}", f"2026-09-0{i+1}", 0.7)
            r["horizon_days"] = 10
            calib.append(r)
    block = v5.build_kpi_block([], calib, resolve_fn=lambda cid: False)
    assert "n=12/" in block
    assert "n=60/" not in block


def test_side_split_dedups_re_alerts():
    import side_split as ss
    rows = [{"condition_id": "a", "alerted_at": "1", "side": "YES", "won": True,
             "ai_yes_estimate": 0.7, "market_yes_price": 0.6},
            {"condition_id": "a", "alerted_at": "2", "side": "YES", "won": True,
             "ai_yes_estimate": 0.7, "market_yes_price": 0.6, "status": "re_alert"}]
    yes, _ = ss.split_by_side(rows)
    assert ss.side_stats(yes)["n"] == 1
