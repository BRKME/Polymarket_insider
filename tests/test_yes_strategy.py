"""YES-стратегия: ставим YES, где рыночная цена YES в зоне 50-65% И Grok
согласен по направлению (тоже склонён к YES). Разворот прежней логики: плывём
ПО рынку, а не против. Опора на калиброванную зону, где Grok надёжен."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yes_strategy import yes_gate, yes_edge, YES_MIN, YES_MAX


def test_gate_accepts_mid_zone():
    assert yes_gate(0.60) is True      # 60% — в зоне
    assert yes_gate(0.50) is True      # нижняя граница включительно
    assert yes_gate(0.65) is True      # верхняя граница (сужена 02.08 с 0.70)


def test_gate_rejects_outside():
    assert yes_gate(0.45) is False     # ниже — монетка/андердог
    assert yes_gate(0.85) is False     # выше — дорогой фаворит, ed"ge нет
    assert yes_gate(0.90) is False


def test_edge_when_grok_agrees():
    # рынок YES 0.60, Grok YES 0.72 -> согласие + Grok выше -> edge положительный
    e = yes_edge(market_yes=0.60, grok_yes=0.72)
    assert e is not None and e > 0


def test_no_edge_when_grok_disagrees():
    # Grok склонён к NO (0.30) — направление не совпало -> нет сигнала
    assert yes_edge(market_yes=0.60, grok_yes=0.30) is None


def test_agreement_by_direction_enough():
    # Grok чуть ниже рынка, но всё ещё YES-склонён (>0.5) -> согласие есть
    e = yes_edge(market_yes=0.65, grok_yes=0.55)
    assert e is not None            # согласия по направлению достаточно


def test_grok_must_be_yes_leaning():
    # Grok ровно 0.5 — не склонён ни туда ни сюда -> нет согласия
    assert yes_edge(market_yes=0.60, grok_yes=0.50) is None


# ── Сужение зоны 02.08.2026 ──────────────────────────────────────────────────
# На n=13 средняя цена входа 60.3c, WR 62% → безубыточный WR при 60c = 60%,
# запас всего 2пп. Арифметика: при WR 62% всё дороже 62c имеет отрицательный EV
# (вход 70c → ROI -12%). Верх зоны срезан до 65% (узкий буфер на рост WR).
def test_zone_upper_bound_tightened():
    assert YES_MAX == 0.65


def test_expensive_favourite_rejected():
    assert yes_gate(0.66) is False      # дороже безубытка — отрицательный EV
    assert yes_gate(0.70) is False      # прежняя граница больше не проходит


def test_profitable_range_still_accepted():
    assert yes_gate(0.50) is True
    assert yes_gate(0.60) is True
    assert yes_gate(0.65) is True       # новая граница включительно


# Размер ставки: Келли убран 04.10.2026 (ставка оператора фиксирована $30) —
# см. tests/test_flat_stake.py.
