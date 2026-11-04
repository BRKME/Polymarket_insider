"""Брифинг для решений оператора и бумажная YES-стратегия (решение 04.11.2026).

Аудит 04.11: YES-алерты, удержанные до резолва, — 40 рынков, 60% при цене
59¢ (+1пп, в пределах шума ±8пп); реальные деньги по сигналам бота −20%.
Весь плюс кошелька — собственные ставки оператора на выборы и геополитику.
Решение оператора: бот перестаёт звать в ставку и помогает решать —
находит рынки этих категорий и приносит сводку; YES-стратегия пишется в
журнал «на бумаге».
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import briefing as br
import scan_events as se

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


def _m(q="Will Puma Shen win the next Taipei Mayor election?", yes=0.31, days=25,
       liq=140_000, cid="0xA", ev="taipei-mayor-election-winner-2026"):
    end = (NOW + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    return {"question": q, "conditionId": cid, "outcomes": '["Yes","No"]',
            "outcomePrices": f'["{yes}","{1-yes:.3f}"]', "liquidity": str(liq),
            "endDate": end, "events": [{"slug": ev}], "description": "Rules.",
            "clobTokenIds": '["1","2"]'}


# ── бумажная YES-стратегия ──────────────────────────────────────────────────

def test_yes_alerts_are_paper():
    assert se.should_alert_side("YES") is False
    assert se.should_alert_side("NO") is False


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


def test_one_brief_per_event_most_liquid():
    ms = [_m(cid="shen", yes=0.31, liq=140_000),
          _m(q="Will Chiang Wan-an win the next Taipei Mayor election?", cid="chiang",
             yes=0.68, liq=103_000)]
    got = br.select_markets(ms, {}, NOW, limit=5)
    assert [m["conditionId"] for m in got] == ["shen"]


def test_limit_per_scan():
    ms = [_m(cid=f"c{i}", ev=f"e{i}", days=5 + i) for i in range(5)]
    assert len(br.select_markets(ms, {}, NOW, limit=2)) == 2


def test_brief_once_unless_big_move():
    seen = {"0xA": {"price": 0.31, "ts": (NOW - timedelta(days=4)).isoformat(),
                    "event": "taipei-mayor-election-winner-2026"}}
    assert br.select_markets([_m(yes=0.35)], seen, NOW, limit=5) == []
    assert len(br.select_markets([_m(yes=0.48)], seen, NOW, limit=5)) == 1   # +17пп
    fresh = {"0xA": dict(seen["0xA"], ts=(NOW - timedelta(days=1)).isoformat())}
    assert br.select_markets([_m(yes=0.48)], fresh, NOW, limit=5) == []      # рано


def test_event_already_briefed_via_other_outcome():
    seen = {"chiang": {"price": 0.68, "ts": (NOW - timedelta(days=1)).isoformat(),
                       "event": "taipei-mayor-election-winner-2026"}}
    assert br.select_markets([_m(cid="shen")], seen, NOW, limit=5) == []


# ── ответ Grok и текст ──────────────────────────────────────────────────────

GROK = """FACTS:
- 01.11: опрос TVBS — Chiang 48%, Shen 41%
- 03.11: Shen получил поддержку DPP-лидеров
DECIDES: победитель официального подсчёта ЦИК 29.11
NEXT: 15.11 теледебаты; 29.11 голосование
RISKS: явка молодёжи; скандал вокруг Chiang
PROB: 33
CONF: medium
"""


def test_parse_brief():
    b = br.parse_brief(GROK)
    assert len(b["facts"]) == 2 and b["facts"][0].startswith("01.11")
    assert "ЦИК" in b["decides"] and "дебаты" in b["next"]
    assert b["prob"] == 0.33 and b["conf"] == "medium"


def test_parse_brief_garbage_is_none():
    assert br.parse_brief("") is None
    assert br.parse_brief("no structure at all") is None


def test_format_brief_is_not_a_signal():
    m = _m()
    others = [("Chiang Wan-an", 0.68)]
    msg = br.format_brief(m, br.parse_brief(GROK), price_7d=0.24, now=NOW, others=others)
    assert msg.startswith("📋")
    assert "31¢" in msg and "▲7пп за 7д" in msg
    assert "опрос TVBS" in msg and "29.11 голосование" in msg
    assert "Chiang Wan-an 68¢" in msg
    assert "33%" in msg and "не обгонял" in msg          # оценка Grok — со справкой
    assert "Купить" not in msg and "ставка $" not in msg
    assert "решение за тобой" in msg.lower()


def test_format_brief_without_history():
    msg = br.format_brief(_m(), br.parse_brief(GROK), price_7d=None, now=NOW)
    assert "за 7д" not in msg


# ── прогон ──────────────────────────────────────────────────────────────────

def test_run_sends_and_records():
    sent, seen = [], {}
    n = br.run_briefs([_m()], seen, NOW, send_fn=lambda t: sent.append(t) or True,
                      brief_fn=lambda q, d, e: br.parse_brief(GROK),
                      price_ago_fn=lambda m, h: 0.24)
    assert n == 1 and len(sent) == 1
    assert seen["0xA"]["price"] == 0.31 and seen["0xA"]["event"].startswith("taipei")


def test_run_skips_when_grok_fails():
    sent, seen = [], {}
    n = br.run_briefs([_m()], seen, NOW, send_fn=lambda t: sent.append(t) or True,
                      brief_fn=lambda q, d, e: None, price_ago_fn=lambda m, h: None)
    assert n == 0 and sent == [] and seen == {}            # попробуем в следующий скан


def test_brief_prompt_forbids_market_odds():
    import ai_context as ac
    low = ac.BRIEF_SYSTEM.lower()
    assert "polymarket" in low and "kalshi" in low
    p = ac._build_brief_prompt("Q?", "Rules", "2026-11-29", today="2026-11-04")
    assert "Сегодня: 2026-11-04" in p and "Rules" in p
