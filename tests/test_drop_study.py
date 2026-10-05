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


# ── первый прогон в Actions (05.10): 99 рынков, 0 историй ────────────────────

def test_end_ts_uses_earliest_of_closed_and_end():
    # рынок, закрытый досрочно: endDate в будущем, closedTime — реальный конец
    m = {"endDate": "2027-05-30T23:59:00Z", "closedTime": "2026-09-01 12:00:00+00"}
    from datetime import datetime, timezone
    assert ds._end_ts(m) == int(datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp())


def test_end_ts_plain_end_date():
    from datetime import datetime, timezone
    assert ds._end_ts({"endDate": "2026-09-01T12:00:00Z"}) == \
        int(datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp())


def test_pagination_does_not_stop_on_page_cap(monkeypatch):
    # Gamma отдаёт максимум 100 за запрос — нельзя считать <500 концом списка
    pages = {0: [_mkt(i) for i in range(100)], 100: [_mkt(i) for i in range(100, 150)],
             150: []}
    calls = []

    def fake_get(url, params, tries=3):
        calls.append(params["offset"])
        return pages.get(params["offset"], [])
    monkeypatch.setattr(ds, "_get", fake_get)
    out = ds.fetch_closed_markets(1000, 0, now_ts=2_000_000_000)
    assert len(out) == 150
    assert calls[:2] == [0, 100]


def _mkt(i):
    return {"question": f"q{i}", "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]',
            "clobTokenIds": '["a","b"]', "volumeNum": 50_000,
            "endDate": "2026-09-01T00:00:00Z"}


# ── прогон 05.10 23:29: 326 рынков из 4000 — Gamma перестаёт отдавать страницы
# глубоко в одном запросе. Выборка идёт недельными окнами.

def test_fetch_walks_weekly_windows(monkeypatch):
    windows = []

    def fake_get(url, params, tries=3):
        if params["offset"] == 0:
            windows.append((params["end_date_min"], params["end_date_max"]))
            w = len(windows)
            return [dict(_mkt(w * 1000 + i), conditionId=f"c{w}-{i}") for i in range(3)]
        return []
    monkeypatch.setattr(ds, "_get", fake_get)
    out = ds.fetch_closed_markets(10_000, 0, now_ts=1_800_000_000, lookback_days=28)
    assert len(windows) == 4                      # 28 дней = 4 недели
    assert len(out) == 12                         # по 3 рынка из каждого окна
    assert windows[0][1] > windows[1][1]          # от свежих к старым
    assert windows[0][0] == windows[1][1]         # окна стыкуются без дыр


def test_fetch_dedups_across_windows(monkeypatch):
    def fake_get(url, params, tries=3):
        return [dict(_mkt(1), conditionId="same")] if params["offset"] == 0 else []
    monkeypatch.setattr(ds, "_get", fake_get)
    assert len(ds.fetch_closed_markets(100, 0, now_ts=1_800_000_000, lookback_days=21)) == 1
