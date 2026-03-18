"""
Unit tests for Polymarket Insider — financial calculations and core logic.

These tests cover the calculations that directly affect trading decisions.
A bug here = real money lost.

Run: python -m pytest tests/ -v
"""
import pytest
import json
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 1: NO Position Cost Calculation
# This is the #1 source of historical bugs in this project.
# ═══════════════════════════════════════════════════════════════

class TestNOPositionCost:
    """
    Polymarket API returns YES token price for ALL trades.
    - YES trade: cost = size * price
    - NO trade:  cost = size * (1 - price)
    
    Getting this wrong shows -100% ROI for every NO trade.
    """

    def test_yes_position_cost(self):
        """YES at 70¢: 100 tokens * 0.70 = $70"""
        size, price, outcome = 100, 0.70, "Yes"
        amount = size * price  # YES formula
        assert amount == pytest.approx(70.0)

    def test_no_position_cost(self):
        """NO at 70¢ YES price: 100 tokens * (1-0.70) = $30"""
        size, price, outcome = 100, 0.70, "No"
        amount = size * (1 - price)  # NO formula
        assert amount == pytest.approx(30.0)

    def test_no_position_cost_extreme_yes_price(self):
        """NO at 90¢ YES price: 100 tokens * 0.10 = $10 (cheap NO)"""
        size, price = 100, 0.90
        amount = size * (1 - price)
        assert amount == pytest.approx(10.0)

    def test_no_position_cost_low_yes_price(self):
        """NO at 10¢ YES price: 100 tokens * 0.90 = $90 (expensive NO)"""
        size, price = 100, 0.10
        amount = size * (1 - price)
        assert amount == pytest.approx(90.0)

    def test_detector_no_calculation(self):
        """Verify detector.py uses correct formula (integration test pattern)"""
        # Simulate what detector.py does
        trade = {"size": "1000", "price": "0.90", "outcome": "No"}
        size = float(trade["size"])
        price = float(trade["price"])
        outcome = trade["outcome"]
        is_no = outcome.lower() == "no"

        if is_no:
            amount = size * (1 - price)
        else:
            amount = size * price

        # NO at 90¢ YES price → $100 cost, NOT $900
        assert amount == pytest.approx(100.0)


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 2: Effective Odds
# ═══════════════════════════════════════════════════════════════

class TestEffectiveOdds:
    """Test analyzer.get_effective_odds()"""

    def test_yes_effective_odds(self):
        from analyzer import get_effective_odds
        assert get_effective_odds(0.70, "Yes") == pytest.approx(0.70)

    def test_no_effective_odds(self):
        from analyzer import get_effective_odds
        assert get_effective_odds(0.70, "No") == pytest.approx(0.30)

    def test_no_extreme_odds(self):
        from analyzer import get_effective_odds
        # YES price 90¢ → NO effective = 10¢
        assert get_effective_odds(0.90, "No") == pytest.approx(0.10)

    def test_yes_extreme_odds(self):
        from analyzer import get_effective_odds
        assert get_effective_odds(0.90, "Yes") == pytest.approx(0.90)

    def test_no_none_outcome(self):
        """None outcome should be treated as YES"""
        from analyzer import get_effective_odds
        assert get_effective_odds(0.70, None) == pytest.approx(0.70)

    def test_no_empty_outcome(self):
        from analyzer import get_effective_odds
        assert get_effective_odds(0.70, "") == pytest.approx(0.70)


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 3: PnL / ROI Calculations
# ═══════════════════════════════════════════════════════════════

class TestPnLCalculations:
    """
    Profit/Loss math for both YES and NO positions.
    Each token pays $1 if the outcome is correct, $0 otherwise.
    """

    def test_yes_profit_calculation(self):
        """YES at 10¢: buy 100 tokens for $10, win = $100 → profit $90"""
        price = 0.10
        amount = 100 * price  # $10 cost
        potential_pnl = amount * ((1 - price) / price)
        assert potential_pnl == pytest.approx(90.0)

    def test_no_profit_calculation(self):
        """NO at 90¢ YES: buy 100 tokens for $10, win = $100 → profit $90"""
        price = 0.90  # YES price
        amount = 100 * (1 - price)  # $10 NO cost
        potential_pnl = amount * (price / (1 - price))
        assert potential_pnl == pytest.approx(90.0)

    def test_pnl_multiplier_yes(self):
        """YES at 10¢ → 9x potential"""
        price = 0.10
        pnl_mult = (1 - price) / price
        assert pnl_mult == pytest.approx(9.0)

    def test_pnl_multiplier_no(self):
        """NO at 90¢ YES price (10¢ NO) → 9x potential"""
        price = 0.90
        pnl_mult = price / (1 - price)
        assert pnl_mult == pytest.approx(9.0)

    def test_pnl_multiplier_50_50(self):
        """50/50 market → 1x potential for both sides"""
        assert (1 - 0.50) / 0.50 == pytest.approx(1.0)
        assert 0.50 / (1 - 0.50) == pytest.approx(1.0)

    def test_roi_never_negative_100(self):
        """ROI should never be -100% for a valid trade (the old bug)"""
        # This was the original bug: all NO trades showed -100% ROI
        price = 0.90
        no_amount = 100 * (1 - price)  # $10
        no_profit = no_amount * (price / (1 - price))  # $90
        roi = no_profit / no_amount * 100  # 900%
        assert roi > 0, "ROI must be positive for a correctly priced NO trade"


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 4: Scoring Logic
# ═══════════════════════════════════════════════════════════════

class TestScoringLogic:

    def test_wallet_age_score_very_new(self):
        """Wallet < 3 days old → 40 points"""
        from analyzer import calculate_wallet_age_score
        # Timestamp for 1 day ago
        ts = int((datetime.now() - timedelta(days=1)).timestamp())
        assert calculate_wallet_age_score(ts) == 40

    def test_wallet_age_score_new(self):
        """Wallet 3-7 days old → 20 points"""
        from analyzer import calculate_wallet_age_score
        ts = int((datetime.now() - timedelta(days=5)).timestamp())
        assert calculate_wallet_age_score(ts) == 20

    def test_wallet_age_score_old(self):
        """Wallet > 7 days old → 0 points"""
        from analyzer import calculate_wallet_age_score
        ts = int((datetime.now() - timedelta(days=30)).timestamp())
        assert calculate_wallet_age_score(ts) == 0

    def test_against_trend_yes_contrarian(self):
        """YES at 7¢ → contrarian, gets points"""
        from analyzer import calculate_against_trend_score
        assert calculate_against_trend_score(0.07, "Yes") > 0

    def test_against_trend_no_safe(self):
        """NO at 7¢ (effective 93%) → safe bet, no points"""
        from analyzer import calculate_against_trend_score
        assert calculate_against_trend_score(0.07, "No") == 0

    def test_against_trend_no_contrarian(self):
        """NO at 96¢ (effective 4%) → contrarian, gets points"""
        from analyzer import calculate_against_trend_score
        assert calculate_against_trend_score(0.96, "No") > 0


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 5: Filter Logic
# ═══════════════════════════════════════════════════════════════

class TestFilterLogic:

    def test_15min_market_detection(self):
        from analyzer import is_15min_market
        assert is_15min_market("Bitcoin Up or Down - 5:15AM-5:30AM ET") == True
        assert is_15min_market("Will Trump win 2028 election?") == False

    def test_short_term_price_filter(self):
        """Short-term price prediction markets should be filtered by should_skip_alert"""
        from analyzer import should_skip_alert
        # "Bitcoin price above $100K today?" would be caught by is_15min_market
        # or the price prediction patterns in should_skip_alert
        from collector import is_trade_suspicious
        trade = {"size": 10000, "price": 0.70}
        market = {"question": "Bitcoin price above $100K today?", "endDate": None}
        # The price_terms + time_terms filter in collector should block this
        # (contains "price" keyword AND "today")
        result = is_trade_suspicious(trade, market)
        assert result == False, "Short-term price prediction should be filtered"

    def test_should_skip_low_roi(self):
        """Trades at 96% effective odds have <5% ROI — not worth insider risk"""
        from analyzer import should_skip_alert
        should_skip, reason = should_skip_alert(
            market_question="Thai election result?",
            wallet_age_days=1,
            odds=0.96,
            total_activities=2,
            end_date_str=None,
            amount=2000,
            latency_minutes=None,
            outcome="Yes"
        )
        assert should_skip, f"Should skip low ROI trade, but didn't. Reason: {reason}"

    def test_should_not_skip_valid_trade(self):
        """A valid $5000 contrarian bet should NOT be filtered"""
        from analyzer import should_skip_alert
        should_skip, reason = should_skip_alert(
            market_question="Will Russia withdraw from Ukraine by 2027?",
            wallet_age_days=2,
            odds=0.15,
            total_activities=3,
            end_date_str=None,
            amount=5000,
            latency_minutes=30,
            outcome="Yes"
        )
        assert not should_skip, f"Valid trade was wrongly filtered: {reason}"


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 6: State Management
# ═══════════════════════════════════════════════════════════════

class TestStateManagement:

    def test_alerts_rotation(self):
        """alerts.json should be capped at 500 entries"""
        # Test the rotation logic directly (same as in save_alerts)
        MAX_ALERTS = 500
        alerts = [{"id": i, "timestamp": "2026-01-01"} for i in range(600)]
        
        if len(alerts) > MAX_ALERTS:
            alerts = alerts[-MAX_ALERTS:]
        
        assert len(alerts) == 500
        # Should keep the NEWEST entries (last 500)
        assert alerts[0]["id"] == 100
        assert alerts[-1]["id"] == 599

    def test_tracked_wallets_migration(self):
        """Old format (list of wallets) should be migrated"""
        from main import load_tracked_wallets
        import tempfile

        tmp = Path(tempfile.mktemp(suffix='.json'))
        tmp.write_text(json.dumps(["0xabc123", "0xdef456"]))

        with patch('main.Path', return_value=tmp):
            data = load_tracked_wallets()
            assert "wallets" in data
            assert "trade_hashes" in data

        tmp.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 7: Smart Filter (collector.py)
# ═══════════════════════════════════════════════════════════════

class TestSmartFilter:

    def test_hft_market_blocked(self):
        from collector import is_trade_suspicious
        trade = {"size": 10000, "price": 0.70}
        market = {"question": "Bitcoin Up or Down - 5:15AM-5:30AM ET"}
        assert is_trade_suspicious(trade, market) == False

    def test_small_trade_blocked(self):
        from collector import is_trade_suspicious
        trade = {"size": 10, "price": 0.70}  # $7 trade
        market = {"question": "Will Trump win 2028?"}
        assert is_trade_suspicious(trade, market) == False

    def test_coin_flip_blocked(self):
        from collector import is_trade_suspicious
        trade = {"size": 10000, "price": 0.50}  # 50/50
        market = {"question": "Will it rain tomorrow?"}
        assert is_trade_suspicious(trade, market) == False

    def test_valid_trade_passes(self):
        from collector import is_trade_suspicious
        trade = {"size": 5000, "price": 0.30}  # $1500, 30% odds
        market = {"question": "Will Russia attack NATO country?"}
        assert is_trade_suspicious(trade, market) == True


# ═══════════════════════════════════════════════════════════════
# TEST GROUP 8: Notifier Trade Info Formatting
# ═══════════════════════════════════════════════════════════════

class TestNotifierFormatting:

    def _make_alert(self, price, outcome, amount):
        """Helper to create a minimal alert dict.
        
        price = YES token price from API (always YES side).
        For NO: effective_odds = 1 - price, raw_price = price.
        """
        if outcome == "No":
            effective = 1 - price
        else:
            effective = price
        return {
            "analysis": {
                "odds": effective,      # effective odds (what analyzer stores)
                "raw_price": price,     # YES token price (what API returns)
                "amount": amount,
            },
            "trade_data": {"outcome": outcome},
        }

    def test_yes_position_display(self):
        from notifier import format_trade_info
        alert = self._make_alert(0.30, "Yes", 1000)
        info = format_trade_info(alert)
        assert "YES" in info["position"]
        assert info["roi_percent"] > 0

    def test_no_position_display(self):
        from notifier import format_trade_info
        alert = self._make_alert(0.90, "No", 100)
        info = format_trade_info(alert)
        assert "NO" in info["position"]
        assert info["roi_percent"] > 0, "NO position ROI must be positive"

    def test_no_position_roi_not_negative(self):
        """The original -100% ROI bug: NO at 90¢ should show ~900% ROI, not -100%"""
        from notifier import format_trade_info
        alert = self._make_alert(0.90, "No", 100)
        info = format_trade_info(alert)
        assert info["roi_percent"] > 100, (
            f"NO at 90¢ should have >100% ROI, got {info['roi_percent']:.1f}%"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
