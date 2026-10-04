"""Спорт не должен проходить в YES/NO-сканер (04.10.2026).

Фильтр сканера и классификатор экспозиции жили раздельно: «leading at
halftime» и «World Cup» классификатор знал как sports, а сканер пропускал.
В журнал YES попали: Barcelona LALIGA, France/FC Barcelona leading at halftime,
Spain eliminated in the World Cup, Dodgers NLCS, Ballon d'Or ×2, US Open ×2.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import category_exposure as cx
import event_scanner as es

LEAKED = [
    "Will Barcelona win the 2026-27 LALIGA Championship?",
    "France leading at halftime?",
    "FC Barcelona leading at halftime?",
    "Will Spain be eliminated in the Semifinals of the World Cup?",
    "Will Los Angeles Dodgers win the 2026 National League Championship Series?",
    "Will Lamine Yamal win the 2026 Ballon d'Or?",
    "Will Harry Kane win the 2026 Ballon d'Or?",
    "Will Jannik Sinner win the 2026 Men's US Open?",
]


@pytest.mark.parametrize("q", LEAKED)
def test_scanner_excludes_leaked_sport(q):
    assert es._is_sport_or_hft(q) is True


@pytest.mark.parametrize("q", LEAKED)
def test_exposure_counts_as_sports(q):
    assert cx.classify(q) == "sports"


@pytest.mark.parametrize("q", [
    "Will the Democrats win the Nevada governor race in 2026?",
    "Will Bitcoin dip to $80,000 in October?",
    "Israel x Iran ceasefire continues through December 31?",
    "Will the Fed increase interest rates by 25 bps after the September meeting?",
])
def test_non_sport_not_excluded(q):
    assert es._is_sport_or_hft(q) is False
