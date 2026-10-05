"""Честность оценщика Grok — разбор провала на выборах в Бразилии (04.10.2026).

Алерты Lula/Flávio/Renan Santos ушли с оценками 85-100% и доводом «опросы
накануне выборов уверенно ставят…». Причины:
- дешёвый вызов БЕЗ поиска получал тот же промпт «найди свежие факты через
  web_search» — инструмента не было, «свежие опросы» генерировались;
- YES-кандидат до 04.10 никогда не шёл на поиск — в алерт шла оценка из памяти;
- в промпте не было сегодняшней даты: модель не знала, что голосование идёт;
- промпт толкал к крайностям («уверенно лидирует → 80-95%»): Renan Santos
  за сутки 5% → 100%.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_cache
import ai_context as ac
import event_scanner as es


# ── промпт ───────────────────────────────────────────────────────────────────

def test_prompt_has_today():
    p = ac._build_estimator_prompt("Q?", None, "2026-10-05", today="2026-10-04",
                                   use_search=True)
    assert "Сегодня: 2026-10-04" in p


def test_no_search_prompt_forbids_fresh_facts():
    p = ac._build_estimator_prompt("Q?", None, "2026-10-05", today="2026-10-04",
                                   use_search=False)
    assert "ПОИСКА НЕТ" in p
    assert "найди свежие факты" not in p.lower()


def test_search_prompt_asks_for_search():
    p = ac._build_estimator_prompt("Q?", None, "2026-10-05", today="2026-10-04",
                                   use_search=True)
    assert "ПОИСКА НЕТ" not in p


def test_system_prompt_no_push_to_extremes():
    assert "80-95" not in ac.ESTIMATOR_SYSTEM
    assert "уже учёл" in ac.ESTIMATOR_SYSTEM      # опросы перед событием в цене


def test_no_search_conf_capped():
    # без поиска уверенность не выше medium — код, а не просьба в промпте
    assert ac._cap_conf_without_search({"prob": 0.9, "conf": "high"}, False)["conf"] == "medium"
    assert ac._cap_conf_without_search({"prob": 0.9, "conf": "high"}, True)["conf"] == "high"


# ── флаг «оценка с поиском» ──────────────────────────────────────────────────

def _two_stage(search_result):
    def under(q, d=None, e=None, use_search=True):
        if use_search:
            return search_result
        return {"prob": 0.85, "conf": "medium", "why": "cheap"}
    return es.make_two_stage_estimator(
        under, yes_price_for=lambda q: 0.57, screen_edge_min=0.15,
        yes_confirm=lambda y, p: True)


def test_searched_flag_set():
    est = _two_stage({"prob": 0.8, "conf": "high", "why": "s"})("Q")
    assert est["searched"] is True


def test_search_failure_marks_unsearched():
    est = _two_stage(None)("Q")
    assert est["searched"] is False


def test_cache_preserves_searched_flag():
    store = {}
    calls = []

    def under(q, d=None, e=None):
        calls.append(q)
        return {"prob": 0.8, "conf": "high", "why": "s", "searched": True}
    fn = ai_cache.make_cached_estimator(under, store, cid_for=lambda q: "c",
                                        yes_for=lambda q: 0.57)
    fn("Q")
    again = fn("Q")
    assert len(calls) == 1
    assert again["searched"] is True


def test_cache_legacy_entry_unsearched():
    store = {"c": {"prob": 0.8, "conf": "high", "why": "", "yes_price": 0.57,
                   "cached_at_epoch": 10**12}}
    fn = ai_cache.make_cached_estimator(lambda *a: None, store,
                                        cid_for=lambda q: "c",
                                        yes_for=lambda q: 0.57,
                                        now_epoch=lambda: 10**12)
    assert fn("Q")["searched"] is False


# ── YES-алерт только по оценке с поиском ─────────────────────────────────────

def _market(q="Will Flávio Bolsonaro finish in second place?", yes=0.575):
    from datetime import datetime, timedelta, timezone
    end = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    return {"question": q, "conditionId": "0xF", "outcomes": '["Yes","No"]',
            "outcomePrices": f'["{yes}","{1-yes:.4f}"]', "liquidity": 150_000,
            "endDate": end, "description": "Resolves by second-most votes."}


def test_scan_yes_rejects_unsearched(monkeypatch):
    import calibration_map as cm
    monkeypatch.setattr(cm, "load_table", lambda: {})
    est = lambda q, d=None, e=None: {"prob": 0.85, "conf": "high", "why": "x",
                                     "searched": False}
    assert es.scan_yes([_market()], est) == []


def test_scan_yes_accepts_searched(monkeypatch):
    import calibration_map as cm
    monkeypatch.setattr(cm, "load_table", lambda: {})
    est = lambda q, d=None, e=None: {"prob": 0.85, "conf": "high", "why": "x",
                                     "searched": True}
    assert len(es.scan_yes([_market()], est)) == 1


def test_calibration_log_records_searched(monkeypatch):
    import scan_events as se
    logged = []
    monkeypatch.setattr(se, "_append_calibration", logged.append)
    monkeypatch.setattr(se, "_load_ai_cache", lambda: {})
    monkeypatch.setattr(
        se, "estimate_probability",
        lambda q, d=None, e=None, use_search=True:
            {"prob": 0.85 if not use_search else 0.8, "conf": "medium", "why": "x"})
    m = _market(yes=0.575)
    est = se._make_logging_estimator([m])(m["question"], m["description"], m["endDate"])
    assert est["searched"] is True               # YES-кандидат ушёл на поиск
    assert logged and logged[-1]["searched"] is True


# ── якорь на котировку рынка (первый боевой скан 05.10, Variational) ────────
# С поиском Grok находит котировку самого рынка: «Polymarket даёт ~60% на
# >$1B…». Тогда «Grok согласен с рынком» = «рынок согласен сам с собой».

def test_system_prompt_forbids_market_odds():
    low = ac.ESTIMATOR_SYSTEM.lower()
    assert "polymarket" in low and "kalshi" in low


def test_detects_market_odds_in_reasoning():
    m = es.mentions_market_odds
    assert m("Polymarket даёт ~60% на >$1B при медиане 1.3-1.5B")
    assert m("Kalshi prices it at 58%")
    assert m("рынки предсказаний оценивают в 60%")
    assert m("букмекеры дают коэффициент 1.6")
    assert not m("Округ глубоко красный, рейтинги Likely R")
    assert not m("")


def test_scan_yes_rejects_anchored_estimate(monkeypatch):
    import calibration_map as cm
    monkeypatch.setattr(cm, "load_table", lambda: {})
    est = lambda q, d=None, e=None: {
        "prob": 0.65, "conf": "medium", "searched": True,
        "why": "Polymarket даёт ~60% на >$1B при сильных метриках"}
    assert es.scan_yes([_market()], est) == []
