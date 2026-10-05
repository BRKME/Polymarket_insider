"""Регрессия: кэш оценок Grok должен переживать запуск.

Обёртка-счётчик отказов в run() теряла _cache_store, run() не находил его
и не сохранял ai_cache.json — каждый запуск заново платил за те же рынки.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan_events as se


def _market(q, yes, cid):
    return {"question": q, "conditionId": cid,
            "outcomes": '["Yes", "No"]', "outcomePrices": f'["{yes}", "{1 - yes}"]',
            "liquidity": 50000, "endDate": "2027-01-01T00:00:00Z"}


def test_cache_survives_run_and_skips_repeat_call(tmp_path, monkeypatch):
    monkeypatch.setattr(se, "CALIB", tmp_path / "calib.jsonl")
    monkeypatch.setattr(se, "AI_CACHE", tmp_path / "ai_cache.json")
    calls = []

    def fake_estimate(q, description=None, end_date=None, use_search=True):
        calls.append((q, use_search))
        return {"prob": 0.70, "conf": "medium", "why": "x"}

    monkeypatch.setattr(se, "estimate_probability", fake_estimate)
    m = _market("Will X happen by 2027?", 0.75, "0xabc")

    # запуск 1: как в run() — логирующий estimator, поверх него счётчик
    stats = {"calls": 0, "fails": 0}
    est = se._count_failures(se._make_logging_estimator([m]), stats)
    assert est(m["question"])["prob"] == 0.70
    assert stats == {"calls": 1, "fails": 0}
    assert hasattr(est, "_cache_store")          # run() сохраняет кэш по нему
    se._save_ai_cache(est._cache_store)
    n_first = len(calls)

    # запуск 2: та же цена — оценка из кэша, Grok не вызывается
    est2 = se._count_failures(se._make_logging_estimator([m]), {"calls": 0, "fails": 0})
    assert est2(m["question"])["prob"] == 0.70
    assert len(calls) == n_first
