"""Классификация экспозиции (04.10.2026): NY-17 House seat попадал в «other»,
занижая elections. Кап и банк убраны позже в тот же день — см. test_flat_stake."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import category_exposure as cx


def test_house_seat_is_elections():
    assert cx.classify("Will the Democratic Party win the NY-17 House seat?") == "elections"


def test_midterm_is_elections():
    assert cx.classify("Will Democrats win the House in the 2026 midterms?") == "elections"
