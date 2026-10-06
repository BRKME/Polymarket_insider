"""Разбивка исследования по видам спорта и типам рынков (06.10.2026).

Вопрос оператора: какие виды спорта и соревнований включить — только
«ограниченные» рынки (конкретный матч/событие, не сезонные фьючерсы), без
киберспорта; где итог предсказуемее.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drop_study as ds


@pytest.mark.parametrize("q,slug,kind", [
    ("Falcons vs. Saints", "nfl-atl-no-2026-10-06", "nfl"),
    ("Lakers vs. Celtics", "nba-lal-bos-2026-11-02", "nba"),
    ("Yankees vs. Red Sox", "mlb-nyy-bos-2026-09-20", "mlb"),
    ("Rangers vs. Bruins", "nhl-nyr-bos-2026-10-20", "nhl"),
    ("Arsenal vs. Chelsea", "epl-ars-che-2026-10-04", "soccer"),
    ("France leading at halftime?", "fifwc-fra-esp-2026-07-14", "soccer"),
    ("Will Jannik Sinner win the 2026 Men's US Open?", "", "tennis"),
    ("Sinner vs. Alcaraz", "atp-sinner-alcaraz-2026-07-12", "tennis"),
    ("Jones vs. Aspinall", "ufc-jones-aspinall-2026-08-01", "mma"),
    ("Will Kimi Antonelli win the 2026 F1 Belgian Grand Prix?", "", "motorsport"),
    ("Will Scheffler win the Masters?", "", "golf"),
    ("Will a team from LCK win MSI 2026?", "", "esports"),
    ("T1 vs. Gen.G", "lol-t1-geng-2026-05-01", "esports"),
    ("Will the Democrats win the Nevada governor race?", "", None),
])
def test_sport_kind(q, slug, kind):
    assert ds.sport_kind(q, slug) == kind


@pytest.mark.parametrize("q,slug,kind", [
    ("Falcons vs. Saints", "nfl-atl-no-2026-10-06", "match"),
    ("Arsenal vs. Chelsea: O/U 2.5", "epl-ars-che-2026-10-04", "spread_total"),
    ("Spread: Lakers (-4.5)", "nba-lal-bos-2026-11-02", "spread_total"),
    ("France leading at halftime?", "fifwc-fra-esp-2026-07-14", "period"),
    ("Will Barcelona win the 2026-27 LALIGA Championship?", "", "outright"),
    ("Will Spain be eliminated in the Semifinals of the World Cup?", "", "outright"),
    ("Will Jannik Sinner win the 2026 Men's US Open?", "", "outright"),
    ("Will Kimi Antonelli win the 2026 F1 Belgian Grand Prix?", "", "outright"),
])
def test_market_kind(q, slug, kind):
    assert ds.market_kind(q, slug) == kind


def test_edge_ci_single_group():
    g = [{"yes": 1, "price": 0.6}] * 30 + [{"yes": 0, "price": 0.6}] * 10
    ci = ds.edge_ci(g)
    assert abs(ci["edge"] - (0.75 - 0.6)) < 1e-9
    assert ci["lo"] < ci["edge"] < ci["hi"]
    assert ds.edge_ci(g[:3]) is None          # мало точек
