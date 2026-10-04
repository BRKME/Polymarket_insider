"""
Tests for category_exposure.py — tag positions by thesis category and report
how much of the bankroll sits in each. The LIMIT itself stays an operator rule
(manual mode); this module only provides the visibility so the operator doesn't
have to keep a spreadsheet by hand.

Run: python -m pytest tests/test_category_exposure.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import category_exposure as ce
import config


class TestClassify:
    def test_geopolitics(self):
        assert ce.classify("US x Iran permanent peace deal by October 31, 2026?") \
            == "geopolitics"
        assert ce.classify("Will Russia and Ukraine sign a ceasefire?") \
            == "geopolitics"

    def test_crypto(self):
        assert ce.classify("Will Bitcoin dip to $50,000 by December 31, 2026?") \
            == "crypto"
        assert ce.classify("Will Ethereum hit $10k?") == "crypto"

    def test_elections(self):
        assert ce.classify(
            "Will Abelardo de la Espriella win the 2026 Colombian presidential election?"
        ) == "elections"

    def test_companies_markets(self):
        assert ce.classify("SpaceX IPO closing market cap above $2.2T?") \
            == "companies"

    def test_fallback_other(self):
        assert ce.classify("Will Jesus Christ return before GTA VI?") == "other"

    def test_empty_is_other(self):
        assert ce.classify("") == "other"


class TestExposure:
    def _rows(self):
        return [
            # open, onchain stake 40
            {"status": "open", "question": "US x Iran peace deal?",
             "stake_actual": 40.0, "category": "geopolitics"},
            # open, no actual stake -> midpoint fallback
            {"status": "open", "question": "Will Bitcoin dip to $50k?",
             "stake_actual": None, "category": "crypto"},
            # closed — must not count
            {"status": "closed", "question": "Old bet",
             "stake_actual": 70.0, "category": "geopolitics"},
            # open, no category tag -> classified on the fly from question
            {"status": "open", "question": "Will Ethereum hit $10k?",
             "stake_actual": 30.0},
        ]

    def test_open_only_and_confirmed_stakes_only(self):
        """15.07: фолбэк на середину диапазона убран — журнал состоит в
        основном из кандидатов сканера без филла, и при банке $200 фолбэк
        ($42.5/строку) ставил все категории над капом. Деньги в риске =
        только ончейн-подтверждённый stake_actual."""
        exp = ce.exposure_by_category(self._rows())
        assert exp["geopolitics"] == 40.0           # closed 70 not counted
        assert exp["crypto"] == 30.0                # без филла — не в риске

    # Кап в % банка убран 04.10.2026 (ставка фиксированная) — см. test_flat_stake.


class TestFormatLine:
    def test_summary_line_mentions_each_category(self):
        exp = {"geopolitics": 40.0, "crypto": 25.0}
        line = ce.format_exposure(exp)
        assert "geopolitics" in line and "crypto" in line
        assert "$40" in line and "$25" in line
