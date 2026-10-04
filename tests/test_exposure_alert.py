"""Экспозиция в алерте: лимит категории и перегруз банка видны и не
сопровождаются советом докупить.

Найдено 04.10.2026: в elections открыто $160 (80% банка) — алерт ставил
⚠️ЛИМИТ и тут же советовал «размер ~$15». Всего подтверждённых ставок ~$310
при BANKROLL=200 — нигде не показывалось. NY-17 House seat попадал в «other»,
занижая elections.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import category_exposure as cx
import config
import event_scanner as es
import scan_events as se


def _yes_cand(question="Will the Republicans win the Georgia governor race in 2026?"):
    return es.Candidate(
        question=question, condition_id="0xG", market_yes_price=0.525,
        no_price=0.475, ai_yes_estimate=0.53, ai_yes_raw=0.55, edge=0.03,
        liquidity=110_000, end_date="2026-11-04T04:59:00Z", reasoning="x",
        ai_conf="medium", event_slug="georgia-governor-winner-2026", side="YES")


def _row(q, stake):
    return {"question": q, "status": "open", "stake_actual": stake,
            "condition_id": q}


def _patch(monkeypatch, rows, bank=200.0):
    monkeypatch.setattr(se, "_load_journal_rows", lambda: rows)
    monkeypatch.setattr(config, "BANKROLL", bank)


def test_house_seat_is_elections():
    assert cx.classify("Will the Democratic Party win the NY-17 House seat?") == "elections"


def test_midterm_is_elections():
    assert cx.classify("Will Democrats win the House in the 2026 midterms?") == "elections"


def test_over_cap_alert_does_not_suggest_size(monkeypatch):
    _patch(monkeypatch, [_row("Will the Democrats win the Ohio governor race?", 160.0)])
    msg = se._format_alert(_yes_cand())
    assert "⚠️ЛИМИТ" in msg
    assert "размер ~$" not in msg
    assert "не докупать" in msg


def test_under_cap_alert_keeps_size(monkeypatch):
    _patch(monkeypatch, [_row("Will the Democrats win the Ohio governor race?", 20.0)])
    msg = se._format_alert(_yes_cand())
    assert "ЛИМИТ" not in msg
    assert "размер ~$" in msg


def test_total_open_over_bankroll_flagged(monkeypatch):
    _patch(monkeypatch, [_row("Will Bitcoin hit $200k?", 150.0),
                         _row("Will Apple stock rise?", 160.0)])
    msg = se._format_alert(_yes_cand())
    assert "всего открыто $310" in msg
    assert "банк $200" in msg


def test_total_under_bankroll_silent(monkeypatch):
    _patch(monkeypatch, [_row("Will Bitcoin hit $200k?", 20.0)])
    msg = se._format_alert(_yes_cand())
    assert "всего открыто" not in msg
