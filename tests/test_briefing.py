"""Брифинг для решений оператора (04.11.2026): очередь рынков + разбор с Claude.

Аудит 04.11: YES-сигналы бота за 40 рынков не обогнали рынок (+1пп, шум
±8пп), реальные деньги по ним −20%; плюс кошелька — собственные ставки
оператора на выборы и геополитику. Решение оператора: бот не зовёт в ставку.
Grok выключен — политика живёт неделями, а не часами: раз в день оператор
запускает разбор в сессии Claude (.claude/skills/daily-brief). Бот готовит
очередь рынков (briefing.py queue), Claude ищет факты и пишет оценку в журнал
(briefing.py log), журнал потом сверяется с резолвами (briefing.py score) —
чтобы знать, обгоняет ли разбор рынок, а не верить на слово.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import briefing as br
import scan_events as se

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def _m(q="Will Puma Shen win the next Taipei Mayor election?", yes=0.31, days=25,
       liq=140_000, cid="0xA", ev="taipei-mayor-election-winner-2026", title=None):
    end = (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    m = {"question": q, "conditionId": cid, "outcomes": '["Yes","No"]',
         "outcomePrices": f'["{yes}","{1-yes:.3f}"]', "liquidity": str(liq),
         "endDate": end, "events": [{"slug": ev}], "description": "Rules text.",
         "clobTokenIds": '["1","2"]'}
    if title:
        m["groupItemTitle"] = title
    return m


# ── Grok выключен, сигналов нет ─────────────────────────────────────────────

def test_grok_disabled_and_alerts_paper():
    assert se.GROK_ENABLED is False
    assert se.should_alert_side("YES") is False
    assert se.should_alert_side("NO") is False


def test_scan_makes_no_grok_calls(monkeypatch):
    calls, sent = [], []
    monkeypatch.setattr(se.collector, "get_active_markets", lambda limit=0: [_m()])
    monkeypatch.setattr(se, "estimate_probability",
                        lambda *a, **k: calls.append(a) or {"prob": 0.9})
    monkeypatch.setattr(se, "_send", lambda t: sent.append(t) or True)
    monkeypatch.setattr(se, "_save_seen", lambda s: None)
    se.run()
    assert calls == [] and sent == []


# ── отбор рынков ────────────────────────────────────────────────────────────

def test_selects_elections_and_geopolitics_only():
    ms = [_m(), _m(q="Will Bitcoin reach $150k by December 31?", cid="0xB", ev="btc"),
          _m(q="Israel x Iran ceasefire continues through December 15?", cid="0xC",
             ev="israel-x-iran-ceasefire", yes=0.42, days=41)]
    got = [m["conditionId"] for m in br.select_markets(ms, {}, NOW, limit=5)]
    assert got == ["0xA", "0xC"]                       # ближайший резолв первым


def test_skips_out_of_window_and_extremes():
    ms = [_m(cid="far", days=200), _m(cid="soon", days=0.5), _m(cid="lock", yes=0.95),
          _m(cid="thin", liq=5_000)]
    assert br.select_markets(ms, {}, NOW, limit=5) == []


def test_one_entry_per_event_most_liquid():
    ms = [_m(cid="shen", yes=0.31, liq=140_000),
          _m(q="Will Chiang Wan-an win the next Taipei Mayor election?", cid="chiang",
             yes=0.68, liq=103_000)]
    got = br.select_markets(ms, {}, NOW, limit=5)
    assert [m["conditionId"] for m in got] == ["shen"]


def test_limit():
    ms = [_m(cid=f"c{i}", ev=f"e{i}", days=5 + i) for i in range(5)]
    assert len(br.select_markets(ms, {}, NOW, limit=2)) == 2


def test_analysed_once_unless_big_move():
    seen = {"0xA": {"price": 0.31, "ts": (NOW - timedelta(days=4)).isoformat(),
                    "event": "taipei-mayor-election-winner-2026"}}
    assert br.select_markets([_m(yes=0.35)], seen, NOW, limit=5) == []
    assert len(br.select_markets([_m(yes=0.48)], seen, NOW, limit=5)) == 1   # +17пп
    fresh = {"0xA": dict(seen["0xA"], ts=(NOW - timedelta(days=1)).isoformat())}
    assert br.select_markets([_m(yes=0.48)], fresh, NOW, limit=5) == []      # рано


def test_event_already_analysed_via_other_outcome():
    seen = {"chiang": {"price": 0.68, "ts": (NOW - timedelta(days=1)).isoformat(),
                       "event": "taipei-mayor-election-winner-2026"}}
    assert br.select_markets([_m(cid="shen")], seen, NOW, limit=5) == []


# ── очередь ─────────────────────────────────────────────────────────────────

def test_queue_entry_has_what_analysis_needs():
    ms = [_m(title="Puma Shen"),
          _m(q="Will Chiang Wan-an win the next Taipei Mayor election?", cid="chiang",
             yes=0.68, liq=103_000, title="Chiang Wan-an")]
    q = br.build_queue(ms, {}, NOW, price_ago_fn=lambda m, h: 0.24, limit=5)
    assert len(q) == 1
    e = q[0]
    assert e["condition_id"] == "0xA" and e["category"] == "elections"
    assert e["yes"] == 0.31 and e["yes_7d_ago"] == 0.24 and e["days_left"] == 25
    assert e["others"] == [["Chiang Wan-an", 0.68]]
    assert e["rules"] == "Rules text."
    assert e["url"].endswith("/event/taipei-mayor-election-winner-2026")


def test_queue_markdown():
    e = br.build_queue([_m()], {}, NOW, price_ago_fn=lambda m, h: 0.24, limit=5)[0]
    md = br.queue_markdown([e])
    assert "Puma Shen" in md and "31¢" in md and "▲7пп за 7д" in md and "0xA" in md


# ── журнал оценок и проверка ────────────────────────────────────────────────

def test_log_entry_and_seen(tmp_path):
    path = tmp_path / "brief_log.jsonl"
    br.append_log(path, {"condition_id": "0xA", "event": "taipei", "question": "Q",
                         "market_yes": 0.31, "my_yes": 0.4, "conf": "medium",
                         "end_date": "2026-11-29", "ts": NOW.isoformat()})
    rows = br.load_log(path)
    assert rows[0]["my_yes"] == 0.4
    seen = br.seen_from_log(rows)
    assert seen["0xA"]["price"] == 0.31 and seen["0xA"]["event"] == "taipei"


def test_log_rejects_bad_probability(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        br.append_log(tmp_path / "l.jsonl", {"condition_id": "x", "market_yes": 0.3,
                                            "my_yes": 40, "ts": NOW.isoformat()})


def test_score_against_market():
    rows = [{"condition_id": "a", "market_yes": 0.6, "my_yes": 0.8, "ts": "1"},
            {"condition_id": "b", "market_yes": 0.3, "my_yes": 0.1, "ts": "1"},
            {"condition_id": "c", "market_yes": 0.5, "my_yes": 0.5, "ts": "1"}]
    out = {"a": 1, "b": 0, "c": None}                  # c ещё не зарезолвлен
    s = br.score(rows, outcome_fn=lambda cid: out[cid])
    assert s["n"] == 2
    assert abs(s["brier_mine"] - ((0.2 ** 2 + 0.1 ** 2) / 2)) < 1e-9
    assert abs(s["brier_market"] - ((0.4 ** 2 + 0.3 ** 2) / 2)) < 1e-9


def test_score_uses_first_estimate_per_market():
    rows = [{"condition_id": "a", "market_yes": 0.6, "my_yes": 0.8, "ts": "1"},
            {"condition_id": "a", "market_yes": 0.9, "my_yes": 0.95, "ts": "2"}]
    s = br.score(rows, outcome_fn=lambda cid: 1)
    assert s["n"] == 1 and abs(s["brier_market"] - 0.16) < 1e-9


def test_skill_instructions_exist():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, ".claude/skills/daily-brief/SKILL.md"),
                encoding="utf-8").read()
    for must in ("briefing.py queue", "briefing.py log", "briefing.py score",
                 "Polymarket", "Kalshi", "не сигнал"):
        assert must in text, must


def test_queue_json_is_pure_json(monkeypatch, capsys):
    import collector

    def chatty(limit=0):
        print("[2026-11-04] ✓ Fetched 1 markets")
        return [_m()]
    monkeypatch.setattr(collector, "get_active_markets", chatty)
    monkeypatch.setattr(br, "_price_ago", lambda m, h: None)
    monkeypatch.setattr(br, "load_log", lambda path=None: [])
    assert br.main(["queue", "--json"]) == 0
    json.loads(capsys.readouterr().out)


# ── светофор (запрос оператора 04.11: «не понятна рекомендация») ────────────
# Цвет — сила перекоса (стоит ли тратить внимание), текст — какая сторона
# дешевле и сколько она стоит по разбору (оператор 07.11). Зелёный только при уверенности не ниже medium: low —
# это «мало фактов», громко звать по такому нельзя.

def test_verdict_fair_market_is_white():
    v = br.verdict(0.10, 0.12, "low")
    assert v.startswith("⚪ ПРОПУСТИТЬ")


def test_verdict_weak_skew_to_no():
    v = br.verdict(0.08, 0.14, "low")                 # Россия–Украина 04.11
    assert v.startswith("🟡 НАБЛЮДАТЬ") and "NO стоит 86¢, по разбору 92¢" in v


def test_verdict_low_conf_never_green():
    assert br.verdict(0.40, 0.20, "low").startswith("🟡 НАБЛЮДАТЬ")


def test_verdict_strong_skew_to_yes():
    v = br.verdict(0.45, 0.32, "medium")
    assert v.startswith("🟢 ПРИСМОТРЕТЬСЯ") and "YES стоит 32¢, по разбору 45¢" in v


def test_verdict_strong_skew_to_no_high_conf():
    v = br.verdict(0.20, 0.42, "high")
    assert v.startswith("🟢 ПРИСМОТРЕТЬСЯ") and "NO стоит 58¢" in v


def test_verdict_word_first_and_short():
    # оператор 04.11: «в начале сообщения нужен вердикт — например, Наблюдать»
    v = br.verdict(0.08, 0.14, "low")
    assert v.split(" · ")[0] == "🟡 НАБЛЮДАТЬ"
    assert len(v) < 90


def test_verdict_probabilities_in_percent_prices_in_cents():
    # оператор 05.11: вероятности — в %; 07.11: цена стороны — в ¢, как в интерфейсе
    v = br.verdict(0.58, 0.55, "low")
    assert "рынок 55%" in v and "разбор 58%" in v and "¢" not in v
    v = br.verdict(0.08, 0.15, "low")
    assert "рынок 15%" in v and "разбор 8%" in v
    assert "NO стоит 85¢, по разбору 92¢" in v
