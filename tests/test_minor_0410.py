"""Мелкие правки подачи по аудиту 04.10.2026."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import event_scanner as es
import scan_events as se
import yes_strategy as ys


def _yes(monkeypatch):
    monkeypatch.setattr(config, "BANKROLL", 200.0)
    monkeypatch.setattr(se, "_load_journal_rows", lambda: [])
    return es.Candidate(question="Will the Republicans win the Georgia governor race?",
                        condition_id="0xG", market_yes_price=0.52, no_price=0.48,
                        ai_yes_estimate=0.6, ai_yes_raw=0.6, edge=0.08,
                        liquidity=110_000, end_date="2026-11-04T04:59:00Z",
                        reasoning="x", ai_conf="medium", side="YES")


def test_alert_zone_label_from_constants(monkeypatch):
    # зона сужена 02.08 до 50-65%, алерт писал «50-70%»
    msg = se._format_alert(_yes(monkeypatch))
    zone = f"{ys.YES_MIN*100:.0f}-{ys.YES_MAX*100:.0f}%"
    assert zone in msg
    assert "50-70%" not in msg


def test_alert_end_date_is_lower_bound(monkeypatch):
    # endDate рынка — не дата выплаты: подсчёт/второй тур отодвигают резолв
    msg = se._format_alert(_yes(monkeypatch))
    assert "резолв ≥04.11.2026" in msg


def test_side_split_reports_yes_since_restart():
    import side_split as ss
    rows = [
        {"condition_id": "old", "alerted_at": "2026-09-01T00:00:00+00:00",
         "side": "YES", "won": True},
        {"condition_id": "new", "alerted_at": "2026-10-05T00:00:00+00:00",
         "side": "YES", "won": False},
    ]
    yes, _ = ss.split_by_side(rows)
    cur = ss.since_restart(yes)
    assert [r["condition_id"] for r in cur] == ["new"]


def test_next_checkpoint_registered():
    from v5_weekly_status import CHECKPOINTS
    future = [c for c in CHECKPOINTS
              if c[1] > datetime(2026, 10, 4, tzinfo=timezone.utc)]
    assert future, "после 02.09 нет ни одного будущего чекпойнта"
