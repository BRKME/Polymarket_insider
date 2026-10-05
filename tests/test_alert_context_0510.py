"""Контекст YES-алерта — по разбору Бразилии (04.10.2026).

- Flávio 2-й и Lula 1-й упали на 14-17пп за ночь перед голосованием и так
  попали в зону 50-65%: рынок узнал новое, а алерт подал это как недооценку.
  Данных мало (5 случаев, 2 выигрыша) — поэтому метка, а не запрет; поле
  price_24h_ago пишется в журнал, чтобы проверить на чекпойнте.
- Lula 1-й, Flávio 2-й, Renan 3-й — одна ставка «порядок из опросов
  сохранится», а пришли тремя независимыми сигналами.
- 🔥 и размер росли с разрывом Grok−рынок; у YES этот разрыв силы не
  означает (политика §2) — 🔥 у YES убран.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import event_scanner as es
import scan_events as se

NOW = datetime(2026, 10, 4, 3, 10, tzinfo=timezone.utc)


def _c(q="Will Flávio Bolsonaro finish in second place?", price=0.575, edge=0.27,
       slug="brazil-presidential-election-first-round-2nd-place",
       days=1.0, ago=None, cid="0xF"):
    end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return es.Candidate(question=q, condition_id=cid, market_yes_price=price,
                        no_price=1 - price, ai_yes_estimate=0.85, ai_yes_raw=0.85,
                        edge=edge, liquidity=150_000, end_date=end, reasoning="x",
                        ai_conf="high", event_slug=slug, side="YES",
                        price_24h_ago=ago)


def _no_journal(monkeypatch):
    monkeypatch.setattr(se, "_load_journal_rows", lambda: [])


# ── 🔥 только у NO ───────────────────────────────────────────────────────────

def test_yes_never_fire(monkeypatch):
    _no_journal(monkeypatch)
    assert not se._format_alert(_c(edge=0.43)).startswith("🔥")


# ── падение цены перед событием ──────────────────────────────────────────────

def test_price_ago_from_calibration_log():
    rows = [
        {"condition_id": "0xF", "estimated_at": "2026-10-02T16:55:00+00:00", "market_yes_price": 0.745},
        {"condition_id": "0xF", "estimated_at": "2026-10-03T02:36:00+00:00", "market_yes_price": 0.745},
        {"condition_id": "0xF", "estimated_at": "2026-10-04T03:08:00+00:00", "market_yes_price": 0.575},
    ]
    assert se._price_ago("0xF", rows, NOW) == 0.745     # ~24ч назад


def test_price_ago_none_without_history():
    assert se._price_ago("0xF", [], NOW) is None


def test_sharp_drop_before_event_flagged(monkeypatch):
    _no_journal(monkeypatch)
    msg = se._format_alert(_c(price=0.575, ago=0.745, days=1.0))
    assert "упала на 17пп за сутки" in msg


def test_drop_far_from_event_not_flagged(monkeypatch):
    _no_journal(monkeypatch)
    assert "за сутки" not in se._format_alert(_c(price=0.575, ago=0.745, days=30))


def test_small_move_not_flagged(monkeypatch):
    _no_journal(monkeypatch)
    assert "за сутки" not in se._format_alert(_c(price=0.575, ago=0.62, days=1.0))


# ── связанные исходы одного события ──────────────────────────────────────────

def test_event_key_groups_brazil():
    k = se._event_key
    assert k("brazil-presidential-election-first-round-2nd-place") == \
        k("brazil-presidential-election-first-round-winner")
    assert k("georgia-governor-winner-2026") != k("nevada-governor-winner-2026")
    assert k("") == ""


def test_related_line_counts_batch_and_open():
    batch = [_c(cid="a", slug="brazil-presidential-election-first-round-winner"),
             _c(cid="b", slug="brazil-presidential-election-first-round-2nd-place"),
             _c(cid="c", slug="brazil-presidential-election-first-round-3rd-place"),
             _c(cid="d", slug="georgia-governor-winner-2026")]
    open_rows = [{"condition_id": "x", "status": "open", "stake_actual": 30,
                  "event_slug": "brazil-presidential-election-first-round-3rd-place"}]
    line = se._related_line(batch[0], batch, open_rows)
    assert "ещё 2 алерта" in line and "1 открытая позиция" in line
    assert se._related_line(batch[3], batch, open_rows) == ""


def test_related_line_in_alert(monkeypatch):
    _no_journal(monkeypatch)
    msg = se._format_alert(_c(), related_line="🔗 связано: ещё 2 алерта")
    assert "🔗 связано: ещё 2 алерта" in msg


# ── вчерашняя цена — из истории CLOB, а не из калибровочного журнала ─────────
# После починки кэша (de39e38, 05.10) Grok не вызывается до 7 дней, пока цена
# не сдвинется на 5пп, — калибровочный журнал перестал быть ежедневной лентой
# цен, и метка падения молча пропадала бы.

def test_price_ago_from_clob_history():
    now_ts = int(NOW.timestamp())
    hist = {"history": [{"t": now_ts - 30 * 3600, "p": 0.75},
                        {"t": now_ts - 24 * 3600, "p": 0.745},
                        {"t": now_ts - 2 * 3600, "p": 0.58}]}
    calls = []

    def fake_get(url, params):
        calls.append(params)
        return hist
    assert se._price_ago_clob("tok", NOW, get_fn=fake_get) == 0.745
    assert calls and calls[0]["market"] == "tok"


def test_price_ago_clob_failure_is_none():
    assert se._price_ago_clob("tok", NOW, get_fn=lambda u, p: None) is None
    assert se._price_ago_clob("", NOW, get_fn=lambda u, p: {"history": []}) is None


def test_yes_token_from_market():
    m = {"outcomes": '["Yes","No"]', "clobTokenIds": '["111","222"]'}
    assert se._yes_token(m) == "111"
    m = {"outcomes": '["No","Yes"]', "clobTokenIds": '["111","222"]'}
    assert se._yes_token(m) == "222"
    assert se._yes_token({}) is None
