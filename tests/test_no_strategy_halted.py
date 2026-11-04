"""Ворота вердикта сработали (02.08.2026, n=278, Δ=-0.073): NO-стратегия на
оценках Grok не бьёт рынок. Алерты по NO выключены, журналирование остаётся —
данные копятся бесплатно. YES остаётся до раздельного вердикта."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scan_events import should_alert_side, NO_STRATEGY_ALERTS


def test_no_alerts_disabled():
    assert NO_STRATEGY_ALERTS is False
    assert should_alert_side("NO") is False


def test_yes_alerts_paper_since_0411():
    # 04.11.2026: YES тоже на бумаге — вместо него брифинг (tests/test_briefing.py)
    assert should_alert_side("YES") is False


def test_default_side_treated_as_no():
    assert should_alert_side(None) is False
    assert should_alert_side("") is False
