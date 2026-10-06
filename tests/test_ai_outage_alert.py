"""Отказ xAI должен быть слышен (ночь на 06.10.2026).

Ночной скан получил 403 «used all available credits or reached its monthly
spending limit», но в Telegram ничего не ушло: условие тревоги было «все
оценки провалились», а после починки кэша часть оценок приходит из кэша —
провалились не все, и тревога молчала. Отказ биллинга — тревога всегда.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan_events as se


def test_billing_block_alerts_even_with_cache_hits():
    msg = se._ai_outage_message({"calls": 10, "fails": 6}, billing_blocked=True)
    assert msg and "лимит" in msg and "xAI" in msg


def test_total_failure_still_alerts():
    msg = se._ai_outage_message({"calls": 5, "fails": 5}, billing_blocked=False)
    assert msg and "Grok недоступен" in msg


def test_partial_failure_without_billing_block_silent():
    assert se._ai_outage_message({"calls": 10, "fails": 2}, billing_blocked=False) is None


def test_few_calls_all_cached_silent():
    assert se._ai_outage_message({"calls": 0, "fails": 0}, billing_blocked=False) is None
