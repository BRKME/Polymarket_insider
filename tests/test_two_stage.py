"""Двухэтапная оценка: дешёвый скрин без поиска -> дорогой поиск только на
прошедших скрин по edge. Режет дорогие поиск-вызовы ($5/1000)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from event_scanner import make_two_stage_estimator


def test_weak_edge_skips_expensive_search():
    search_calls = []
    cheap_calls = []
    def estimator(q, desc=None, end=None, use_search=True):
        if use_search:
            search_calls.append(q)
            return {"prob": 0.10, "conf": "high", "why": "searched"}
        cheap_calls.append(q)
        return {"prob": 0.48, "conf": "medium", "why": "cheap"}  # слабый edge
    fn = make_two_stage_estimator(estimator, yes_price_for=lambda q: 0.50,
                                  screen_edge_min=0.10)
    # market YES 0.50, cheap est YES 0.48 -> edge 0.02 < 0.10 -> поиск НЕ нужен
    fn("Some market")
    assert len(cheap_calls) == 1
    assert len(search_calls) == 0      # дорогой поиск пропущен


def test_strong_edge_triggers_search():
    search_calls = []
    def estimator(q, desc=None, end=None, use_search=True):
        if use_search:
            search_calls.append(q)
            return {"prob": 0.12, "conf": "high", "why": "searched"}
        return {"prob": 0.15, "conf": "medium", "why": "cheap"}  # сильный edge
    fn = make_two_stage_estimator(estimator, yes_price_for=lambda q: 0.50,
                                  screen_edge_min=0.10)
    # cheap YES 0.15, market 0.50 -> edge 0.35 >= 0.10 -> поиск подтверждает
    result = fn("Strong market")
    assert len(search_calls) == 1
    assert result["why"] == "searched"  # финал — из поиска


def test_cheap_screen_none_skips_search():
    search_calls = []
    def estimator(q, desc=None, end=None, use_search=True):
        if use_search:
            search_calls.append(q); return {"prob": 0.1, "conf": "high", "why": "s"}
        return None      # скрин не дал оценки
    fn = make_two_stage_estimator(estimator, yes_price_for=lambda q: 0.50,
                                  screen_edge_min=0.10)
    assert fn("x") is None
    assert len(search_calls) == 0


# ── YES-кандидаты тоже проверяются поиском (04.10.2026) ──────────────────────
# Поиск включался только по NO-разрыву (рынок − Grok ≥ EDGE_MIN). Для YES-
# кандидатов (Grok согласен с рынком) разрыв ~0, и Grok всегда отвечал по
# памяти: «кандидаты не определены» о выборах через месяц после праймериз.

def _yes_confirm(yes_price, cheap_prob):
    import yes_strategy as ys
    return ys.yes_gate(yes_price) and cheap_prob > ys.GROK_YES_THRESHOLD


def test_yes_candidate_triggers_search():
    search_calls = []
    def estimator(q, desc=None, end=None, use_search=True):
        if use_search:
            search_calls.append(q)
            return {"prob": 0.42, "conf": "medium", "why": "searched"}
        return {"prob": 0.56, "conf": "low", "why": "cheap"}   # согласие по памяти
    fn = make_two_stage_estimator(estimator, yes_price_for=lambda q: 0.54,
                                  screen_edge_min=0.15, yes_confirm=_yes_confirm)
    result = fn("Will the Democrats win the Nevada governor race?")
    assert len(search_calls) == 1
    assert result["why"] == "searched"   # в ставку идёт оценка с фактами


def test_yes_disagreement_skips_search():
    search_calls = []
    def estimator(q, desc=None, end=None, use_search=True):
        if use_search:
            search_calls.append(q)
        return {"prob": 0.45, "conf": "low", "why": "cheap"}
    fn = make_two_stage_estimator(estimator, yes_price_for=lambda q: 0.54,
                                  screen_edge_min=0.15, yes_confirm=_yes_confirm)
    fn("Some market")
    assert search_calls == []            # не YES-кандидат и слабый NO-разрыв


def test_scanner_wires_yes_confirm(monkeypatch):
    import scan_events as se
    captured = {}
    real = se.es.make_two_stage_estimator
    def spy(*a, **kw):
        captured.update(kw)
        return real(*a, **kw)
    monkeypatch.setattr(se.es, "make_two_stage_estimator", spy)
    monkeypatch.setattr(se, "_load_ai_cache", lambda: {})
    se._make_logging_estimator([])
    assert callable(captured.get("yes_confirm"))
    assert captured["yes_confirm"](0.54, 0.56) is True
    assert captured["yes_confirm"](0.54, 0.45) is False
    assert captured["yes_confirm"](0.80, 0.90) is False   # вне YES-зоны
