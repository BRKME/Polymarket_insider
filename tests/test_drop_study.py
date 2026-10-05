"""Исследование «падение в зону перед событием» — чистая расчётная часть.

Вопрос (политика, открытые вопросы): цена YES упала ≥10пп за сутки и
оказалась в 50-65% при ≤3д до события — это новость (не покупать) или
перегиб толпы (покупать)? Своих резолвов мало (n=1-2), поэтому скрипт
гоняется в Actions по истории закрытых рынков Polymarket.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drop_study as ds

H = 3600


def _hist(points):
    """points: [(hours_before_end, price)] -> [{'t': epoch, 'p': price}]"""
    end = 1_000_000
    return end, [{"t": end - int(h * H), "p": p} for h, p in points]


def test_drop_into_zone_classified():
    end, hist = _hist([(60, 0.75), (36, 0.75), (12, 0.58), (6, 0.57)])
    obs = ds.classify(hist, end)
    assert obs is not None
    assert obs["move"] == "drop"
    assert abs(obs["price"] - 0.58) < 1e-9
    assert abs(obs["delta"] - (0.58 - 0.75)) < 1e-9


def test_flat_in_zone():
    end, hist = _hist([(60, 0.60), (36, 0.61), (12, 0.60)])
    assert ds.classify(hist, end)["move"] == "flat"


def test_rise_into_zone():
    end, hist = _hist([(60, 0.45), (36, 0.45), (12, 0.60)])
    assert ds.classify(hist, end)["move"] == "rise"


def test_outside_window_or_zone_ignored():
    end, hist = _hist([(200, 0.60), (150, 0.58)])        # дальше 72ч
    assert ds.classify(hist, end) is None
    end, hist = _hist([(60, 0.90), (30, 0.88), (12, 0.85)])  # вне зоны
    assert ds.classify(hist, end) is None


def test_needs_price_a_day_before():
    end, hist = _hist([(12, 0.58), (6, 0.57)])            # нет точки 24ч назад
    assert ds.classify(hist, end) is None


def test_summary_metrics():
    obs = [{"move": "drop", "price": 0.6, "yes": 0},
           {"move": "drop", "price": 0.6, "yes": 1},
           {"move": "flat", "price": 0.5, "yes": 1}]
    s = ds.summarize(obs)
    assert s["drop"]["n"] == 2
    assert abs(s["drop"]["yes_rate"] - 0.5) < 1e-9
    assert abs(s["drop"]["edge"] - (0.5 - 0.6)) < 1e-9
    assert abs(s["drop"]["pnl_per_usd"] - ((1 / 0.6 - 1) - 1) / 2) < 1e-9
    assert s["flat"]["n"] == 1


def test_outcome_from_final_prices():
    assert ds.final_outcome({"outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'}) == 1
    assert ds.final_outcome({"outcomes": '["Yes","No"]', "outcomePrices": '["0","1"]'}) == 0
    assert ds.final_outcome({"outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'}) is None
    assert ds.final_outcome({"outcomes": '["A","B"]', "outcomePrices": '["1","0"]'}) is None
