"""YES-гейт судит по СЫРОЙ оценке Grok, а не по откалиброванной.

Баг (найден 04.10.2026): посткалибровка заменяет оценку Grok средним по корзине,
а таблица немонотонна — сырые 0.0-0.2 превращались в 0.85, 0.2-0.4 в 0.90.
Grok, считавший событие маловероятным, выдавался за «согласие по YES»: Maine
Senate сырые 4% → в алерте 69%, ставка $15; Nevada governor сырые 42% → 53%.
Так было 12 из 82 YES-алертов. «Согласие по направлению» — это мнение самого
Grok, и проверять его надо на том, что Grok сказал.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import calibration_map as cm
import event_scanner as es

# Таблица в форме боевой на 04.10.2026 (немонотонная, все корзины n>=25 кроме
# нижней — калибровка почти целиком заменяет сырое число константой корзины).
_TABLE = {
    "0.0-0.2": {"actual": 0.85, "n": 20},
    "0.2-0.4": {"actual": 0.896551724137931, "n": 29},
    "0.4-0.6": {"actual": 0.5294117647058824, "n": 51},
    "0.6-0.8": {"actual": 0.782608695652174, "n": 92},
    "0.8-1.0": {"actual": 0.9928057553956835, "n": 139},
}


def _market(question, yes_price):
    end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    return {
        "question": question,
        "conditionId": "0x" + str(abs(hash(question)))[:10],
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{yes_price}", "{1 - yes_price:.4f}"]',
        "liquidity": 100_000,
        "endDate": end,
        "description": "Resolves YES if the candidate wins.",
    }


def _scan(monkeypatch, question, yes_price, raw_prob, conf="medium"):
    monkeypatch.setattr(cm, "load_table", lambda: _TABLE)
    est = lambda q, d=None, e=None: {"prob": raw_prob, "conf": conf, "why": "x"}
    return es.scan_yes([_market(question, yes_price)], est)


def test_grok_against_yes_is_not_agreement_after_calibration(monkeypatch):
    # Maine: рынок 60%, Grok сырые 4% (против) → калибровка 0.69 — НЕ согласие
    assert _scan(monkeypatch, "Will the Democrats win the Maine Senate race?",
                 0.60, 0.04) == []


def test_grok_slightly_against_is_not_agreement(monkeypatch):
    # Nevada: рынок 53.5%, Grok сырые 42% → калибровка 0.5294 — НЕ согласие
    assert _scan(monkeypatch, "Will the Democrats win the Nevada governor race?",
                 0.535, 0.42) == []


def test_raw_agreement_still_passes(monkeypatch):
    # Grok сырые 75% при рынке 58% — настоящее согласие, сигнал остаётся
    out = _scan(monkeypatch, "Will the Republicans win the Iowa Senate race?",
                0.58, 0.75)
    assert len(out) == 1
    c = out[0]
    assert c.side == "YES"
    assert c.ai_yes_raw == 0.75
    assert abs(c.edge - (0.75 - 0.58)) < 1e-9   # edge по сырому числу


def test_alert_shows_raw_grok(monkeypatch):
    # В алерте «Grok: N%» — то, что сказал Grok, а не константа корзины
    import scan_events as se
    out = _scan(monkeypatch, "Will the Republicans win the Iowa Senate race?",
                0.58, 0.75)
    text = se._format_alert(out[0])
    assert "Grok: 75%" in text
    assert "Grok: 78%" not in text      # 0.7826 — среднее корзины 0.6-0.8


# ── Уверенность (04.10.2026) ─────────────────────────────────────────────────
# NO-ветка требует conf >= medium (MIN_CONFIDENCE), YES-ветка пропускала low:
# оба алерта 04.10 (Georgia, Nevada) — low, всего таких 21 из 82.

def test_low_confidence_rejected(monkeypatch):
    assert _scan(monkeypatch, "Will the Republicans win the Iowa Senate race?",
                 0.58, 0.75, conf="low") == []


def test_medium_confidence_passes(monkeypatch):
    assert len(_scan(monkeypatch, "Will the Republicans win the Iowa Senate race?",
                     0.58, 0.75, conf="medium")) == 1
