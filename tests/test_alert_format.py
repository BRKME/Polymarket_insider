"""
Tests for the compact alert format (scan_events._format_alert) and the
reasoning/presentation split in event_scanner.

The old format repeated the same numbers three times (header, prose paragraph,
entry line) across four divider rules. The new contract:
  • the decision lives in the first line: NO price · edge · deadline
  • Grok's estimate vs market is ONE line; Grok's why is ONE line
  • suspicious is ONE warning line, not a paragraph
  • no '—————' dividers, no prose restating the header

Run: python -m pytest tests/test_alert_format.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import event_scanner as es
import scan_events as se


def _candidate(**kw):
    base = dict(
        question="US-Iran nuclear deal before 2027?",
        condition_id="0xAAA",
        market_yes_price=0.64,
        no_price=0.36,
        ai_yes_estimate=0.28,
        edge=0.36,
        liquidity=134497.0,
        end_date="2026-12-31T12:00:00Z",
        reasoning="Persistent core disagreements make a full deal unlikely",
        ai_conf="medium",
        event_slug="us-iran-nuclear-deal-before-2027",
    )
    base.update(kw)
    return es.Candidate(**base)


class TestReasoningIsJustWhy:
    def test_evaluate_reasoning_carries_only_grok_why(self):
        from datetime import datetime, timezone, timedelta
        end = (datetime.now(timezone.utc) + timedelta(days=100)).isoformat()
        m = {"question": "Will X happen?", "outcomes": '["Yes","No"]',
             "outcomePrices": '["0.80","0.20"]', "liquidity": 50000,
             "endDate": end, "conditionId": "0x1"}
        c = es.evaluate(m, lambda q, d=None, e=None: {
            "prob": 0.30, "conf": "high", "why": "clean structural reason"})
        assert c.reasoning == "clean structural reason"
        # no duplicated prose restating the numbers
        assert "переоценён" not in c.reasoning
        assert c.ai_conf == "high"


class TestCompactAlertV2:
    """v2 (UX-фидбек оператора 10.06): действие глаголом, одна рамка
    вероятностей, цена входа в центах, длинная заморозка флагом, без
    смешения стрелок, довод без двойного маркера."""

    def test_action_line_with_verb_and_cents(self):
        msg = se._format_alert(_candidate())
        assert "Купить NO ~36¢" in msg
        assert "ставка $30" in msg      # фиксированная с 04.10.2026

    def test_single_probability_frame(self):
        msg = se._format_alert(_candidate())
        # одна рамка: вероятность YES у рынка и у Grok + вывод
        assert "Рынок верит в YES: 64%" in msg
        assert "Grok: 28% (medium)" in msg
        assert "переоценка 36пп" in msg
        # цена NO не дублируется второй рамкой "NO 36%"
        assert "NO 36%" not in msg

    def test_long_lock_flagged(self):
        c = _candidate(end_date="2027-12-31T12:00:00Z")
        msg = se._format_alert(c)
        assert "⏳" in msg and "заморозк" in msg

    def test_short_horizon_not_flagged(self):
        msg = se._format_alert(_candidate())   # ~200 дней... подберём короче
        c = _candidate(end_date="2026-07-01T12:00:00Z")
        msg = se._format_alert(c)
        assert "⏳" not in msg

    def test_rationale_without_double_marker(self):
        c = _candidate(reasoning="• Fixed target makes it unlikely")
        msg = se._format_alert(c)
        assert "→ •" not in msg and "Почему: •" not in msg
        assert "Fixed target" in msg

    def test_exposure_line_disambiguated(self):
        msg = se._format_alert(_candidate())
        assert "уже открыто" in msg     # $ экспозиции не путается с размером

    def test_checklist_without_arrows(self):
        msg = se._format_alert(_candidate())
        assert "Чек:" in msg
        # стрелка остаётся только у вывода переоценки
        assert msg.count("→") <= 1
