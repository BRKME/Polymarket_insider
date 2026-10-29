"""Канал без спама (жалоба оператора 29.10.2026: «за полдня — как будто спам»).

Правило: в реальном времени — только НОВОЕ событие и только один раз;
ежедневные напоминания живут в одном месте — дневном статусе.
- сигнал выхода по позиции повторялся каждые сутки, пока её держат, и ещё раз
  дублировался в «Требуют решения» дневного статуса;
- «xAI отказал» уходил с каждым сканом (2 раза в день) весь срок отказа;
- «🔴 РЕЖЬ» по позиции за 2¢ — спасать нечего, продажа вернёт ~$1.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mark_to_market as mtm
import scan_events as se

T0 = datetime(2026, 10, 29, 6, tzinfo=timezone.utc)


# ── сигналы выхода: один раз на позицию и действие ──────────────────────────

def test_same_exit_signal_never_repeats():
    seen = {}
    assert mtm._should_send(seen, "c", "TAKE_PARTIAL", T0) is True
    assert mtm._should_send(seen, "c", "TAKE_PARTIAL", T0 + timedelta(days=1)) is False
    assert mtm._should_send(seen, "c", "TAKE_PARTIAL", T0 + timedelta(days=5)) is False


def test_escalation_still_sent():
    seen = {}
    mtm._should_send(seen, "c", "TAKE_PARTIAL", T0)
    assert mtm._should_send(seen, "c", "CLOSE_FULL", T0 + timedelta(hours=2)) is True


# ── обнулившаяся позиция: не «режь», а разовое «потеряно» ───────────────────

def _yes_pos(entry=0.52):
    return {"condition_id": "v", "question": "Will Aleksandar Vučić be the next Prime Minister of Serbia?",
            "side": "YES", "status": "open", "stake_actual": 25.47,
            "fill_source": "onchain", "entry_price_actual": entry, "market_yes_price": entry}


def _fetch(p):
    return lambda cid: {"outcomes": '["Yes","No"]', "outcomePrices": f'["{p}","{1-p:.3f}"]'}


def test_wiped_out_position_is_lost_not_cut():
    sig = mtm.scan_open_positions([_yes_pos()], fetch_fn=_fetch(0.02))
    assert len(sig) == 1 and sig[0]["action"] == "LOST"
    text = mtm._format_signal(sig[0])
    assert "РЕЖЬ" not in text and "обнулилась" in text


def test_real_drawdown_still_cut():
    sig = mtm.scan_open_positions([_yes_pos(0.60)], fetch_fn=_fetch(0.35))
    assert sig[0]["action"] == "CUT"


# ── тревога об отказе xAI: только на переходах ──────────────────────────────

def test_outage_alert_only_on_start():
    msg, st = se._outage_transition({}, blocked=True, ok_calls=0, now=T0)
    assert msg and "xAI" in msg and st.get("blocked_since")
    msg2, st2 = se._outage_transition(st, blocked=True, ok_calls=0, now=T0 + timedelta(hours=12))
    assert msg2 is None and st2["blocked_since"] == st["blocked_since"]


def test_outage_recovery_announced_once():
    st = {"blocked_since": T0.isoformat()}
    msg, st2 = se._outage_transition(st, blocked=False, ok_calls=5, now=T0 + timedelta(days=3))
    assert msg and "снова" in msg and not st2.get("blocked_since")
    msg3, _ = se._outage_transition(st2, blocked=False, ok_calls=5, now=T0 + timedelta(days=4))
    assert msg3 is None


def test_no_calls_no_recovery_claim():
    # всё из кэша — о восстановлении не судим
    st = {"blocked_since": T0.isoformat()}
    msg, st2 = se._outage_transition(st, blocked=False, ok_calls=0, now=T0 + timedelta(days=1))
    assert msg is None and st2.get("blocked_since")


def test_daily_status_carries_outage_line():
    import daily_status as ds
    line = ds._ai_status_line({"blocked_since": "2026-10-23T02:18:00+00:00"})
    assert "Grok" in line and "23.10" in line
    assert ds._ai_status_line({}) == ""
