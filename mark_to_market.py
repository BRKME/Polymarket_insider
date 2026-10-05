"""
mark_to_market.py — monitor OPEN NO positions and emit manual exit signals.

Manual betting mode: we never place or close trades automatically. This module
re-reads current prices for positions still marked open in event_journal.jsonl,
computes unrealised P&L at the actual recorded stake, and tells the operator (via
Telegram) when a position has matured enough to bank — so capital recycles
instead of sitting frozen until a far-off resolution.

Exit tiers (config):
  • NO >= EXIT_PARTIAL_PRICE  → take partial profit
  • NO >= EXIT_FULL_PRICE     → close the remainder
  • current edge <= EXIT_STOP_EDGE → our thesis has inverted; flag to cut

Pure decision logic is split out (decide_exit, position_pnl, current_no_price)
so it can be unit-tested offline. Network reads reuse resolution_tracker.

Run:  python mark_to_market.py
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Dict, List

from config import (
    EXIT_PARTIAL_PRICE, EXIT_FULL_PRICE, EXIT_STOP_EDGE,
    STAKE_MIN, STAKE_MAX,
)

JOURNAL = Path("event_journal.jsonl")


# ── pure logic (no network) ─────────────────────────────────────────────────

def position_stake(row: dict) -> float:
    """The stake to score a position by.

    Prefer the actual hand-entered stake; fall back to the midpoint of the
    allowed range when a position predates actual-fill logging.
    """
    s = row.get("stake_actual")
    try:
        s = float(s)
        if s > 0:
            return s
    except (TypeError, ValueError):
        pass
    return (STAKE_MIN + STAKE_MAX) / 2.0


def entry_no_price(row: dict) -> Optional[float]:
    """The NO price we actually entered at (preferred) or the alert price."""
    for key in ("entry_price_actual", "no_price"):
        v = row.get(key)
        try:
            v = float(v)
            if 0 < v < 1:
                return v
        except (TypeError, ValueError):
            continue
    return None


def position_pnl(entry: float, current: float, stake: float) -> dict:
    """Unrealised P&L for a NO position, normalised to shares bought with `stake`.

    On Polymarket a NO share costs `entry` and pays $1 if NO wins. With `stake`
    dollars we hold stake/entry shares; current mark value is shares*current.
    """
    if entry <= 0:
        return {"shares": 0.0, "value": 0.0, "unrealised": 0.0, "ret_pct": 0.0}
    shares = stake / entry
    value = shares * current
    unrealised = value - stake
    ret_pct = (current / entry - 1.0) * 100.0
    return {
        "shares": round(shares, 2),
        "value": round(value, 2),
        "unrealised": round(unrealised, 2),
        "ret_pct": round(ret_pct, 1),
    }


def decide_exit(current_no: float, current_edge: Optional[float]) -> Optional[str]:
    """Return an exit action label, or None to hold.

    current_edge = (1 - current_no) - ai_yes  — our mispricing, re-marked to the
    live price. Order of checks matters:

      1. Profit tiers first. If NO has run up to the exit bands, we exit to BANK
         the gain — that the re-marked edge looks small/negative there is
         expected (the market has converged toward our thesis, which is the win).
      2. Only BELOW the profit tiers does an inverted edge mean trouble: the
         price moved AGAINST us past our estimate, so the thesis is gone — cut.
    """
    if current_no >= EXIT_FULL_PRICE:
        return "CLOSE_FULL"   # banked almost all the edge
    if current_no >= EXIT_PARTIAL_PRICE:
        return "TAKE_PARTIAL"
    if current_edge is not None and current_edge <= EXIT_STOP_EDGE:
        return "CUT"          # edge inverted while price is against us — exit
    return None


# ── network-backed helpers (injectable) ─────────────────────────────────────

def current_no_price(condition_id: str, fetch_fn: Callable[[str], Optional[Dict]]) -> Optional[float]:
    """Current NO price from a fresh market read. fetch_fn injected for tests."""
    market = fetch_fn(condition_id)
    if not market:
        return None
    import event_scanner as es
    parsed = es._parse_prices(market)
    if not parsed:
        return None
    _, no_price = parsed
    return no_price


def _default_fetch(condition_id: str) -> Optional[Dict]:
    import resolution_tracker as rt
    m = rt.fetch_market_by_condition_id(condition_id)
    if not m:
        m = rt.fetch_market_by_clob(condition_id)
    return m


def _load_journal() -> List[dict]:
    if not JOURNAL.exists():
        return []
    rows = []
    for line in JOURNAL.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


# ── YES-позиции (средняя зона): зеркало NO-логики ───────────────────────────
# Пробел найден 08.08.2026: реальные позиции оператора — все YES, а мониторинг
# их пропускал, т.к. NO-правила при применении к YES дают обратный смысл.
# Для YES прибыль растёт, когда цена ИДЁТ ВВЕРХ (вход 57.8c → сейчас 73.7c).
YES_CUT_DRAWDOWN = 0.30       # просадка от входа, при которой тезис считаем сломанным


def decide_exit_yes(entry: float, current: float) -> Optional[str]:
    """Сигнал выхода для YES-позиции, или None (держим).

    Порядок как в NO-версии: сначала тиры прибыли (банкуем, когда рынок сошёлся
    к нашему тезису), и только потом — стоп по просадке.
    """
    if current is None or entry is None:
        return None
    if current >= EXIT_FULL_PRICE:
        return "CLOSE_FULL"
    if current >= EXIT_PARTIAL_PRICE:
        return "TAKE_PARTIAL"
    try:
        if entry > 0 and (current - entry) / entry <= -YES_CUT_DRAWDOWN:
            return "CUT"          # цена ушла против нас далеко — тезис сломан
    except (TypeError, ZeroDivisionError):
        pass
    return None


def position_pnl_yes(entry: float, current: float, stake: float) -> dict:
    """P&L YES-позиции: прибыль при РОСТЕ цены (обратно NO).

    Контракт ключей ОБЯЗАН совпадать с position_pnl: _format_signal написан под
    NO и ждёт shares/value/unrealised/ret_pct. Расхождение ({pnl, pct}) уронило
    mark_to_market тремя прогонами подряд 29.08.2026 — KeyError 'ret_pct' на
    первом же YES-сигнале, который дошёл до форматтера.
    """
    empty = {"shares": 0.0, "value": 0.0, "unrealised": 0.0, "ret_pct": 0.0}
    try:
        entry = float(entry); current = float(current); stake = float(stake)
    except (TypeError, ValueError):
        return empty
    if entry <= 0:
        return empty
    shares = stake / entry
    value = shares * current
    return {
        "shares": round(shares, 2),
        "value": round(value, 2),
        "unrealised": round(value - stake, 2),
        "ret_pct": round((current / entry - 1.0) * 100.0, 1),
    }


def _parse_yes_price(market: Optional[Dict]) -> Optional[float]:
    """Текущая цена YES из свежего чтения рынка."""
    if not market:
        return None
    try:
        outcomes = market.get("outcomes") or []
        prices = market.get("outcomePrices") or []
        if isinstance(outcomes, str):
            import json as _j
            outcomes = _j.loads(outcomes)
        if isinstance(prices, str):
            import json as _j
            prices = _j.loads(prices)
        for name, px in zip(outcomes, prices):
            if str(name).strip().lower() == "yes":
                return float(px)
    except Exception:
        return None
    return None


def _entry_yes_price(row: dict) -> Optional[float]:
    """Цена входа YES-позиции: подтверждённая ончейн, иначе цена из алерта."""
    for key in ("entry_price_actual", "market_yes_price"):
        v = row.get(key)
        if v is not None:
            try:
                p = float(v)
                if 0 < p < 1:
                    return p
            except (TypeError, ValueError):
                continue
    return None


def _has_real_fill(row: dict) -> bool:
    """Был ли РЕАЛЬНЫЙ вход: fill_matcher проставляет stake_actual, найдя сделку
    ончейн. В журнал пишутся все алерты, т.е. кандидаты — без этой проверки
    mark-to-market рисует P&L по позициям, которых у оператора нет.
    Полевой баг 08.08.2026: 44 из 50 «открытых» строк были фантомами, бот звал
    «фиксируй прибыль $59.92» по несуществующей позиции."""
    try:
        return float(row.get("stake_actual") or 0) > 0
    except (TypeError, ValueError):
        return False


def _is_open(row: dict) -> bool:
    """A position is open unless explicitly closed."""
    return str(row.get("status", "open")).lower() == "open"


def scan_open_positions(
    rows: List[dict],
    fetch_fn: Callable[[str], Optional[Dict]] = _default_fetch,
    calib_rows: Optional[List[dict]] = None,
    manual: Optional[Dict[str, dict]] = None,
) -> List[dict]:
    """Re-mark every open position; return those that warrant an exit signal.

    calib_rows (calibration_journal) — для поиска позиций по «перевёрнутым»
    YES-алертам (grok_flip): по ним CLOSE_FLIP независимо от цены."""
    import grok_flip
    flip_idx = grok_flip.index_calibration(calib_rows) if calib_rows else {}
    signals = []
    for row in rows:
        if not _is_open(row):
            continue
        # Только РЕАЛЬНЫЕ позиции: строка журнала без подтверждённого входа —
        # это алерт-кандидат, а не сделка. См. _has_real_fill.
        if not _has_real_fill(row):
            continue
        # YES-позиции (средняя зона) идут по ЗЕРКАЛЬНОЙ логике: прибыль растёт
        # при росте цены. Раньше они просто пропускались — реальные позиции
        # оператора оставались без сигналов выхода (баг 08.08.2026).
        if str(row.get("side", "NO")).upper() == "YES":
            cid = row.get("condition_id", "")
            if not cid:
                continue
            mkt = fetch_fn(cid)
            parsed = _parse_yes_price(mkt)
            if parsed is None:
                continue
            entry = _entry_yes_price(row)
            if entry is None:
                continue
            action = decide_exit_yes(entry, parsed)
            grok_raw = None
            if (flip_idx and grok_flip.is_flipped(row, flip_idx)
                    and cid not in (manual or {})):   # ручная — решение принято
                # Алерт ушёл, хотя Grok был против (баг калибровки до 04.10) —
                # оператор решил такие позиции закрыть. Важнее ценовых сигналов.
                action = "CLOSE_FLIP"
                grok_raw = grok_flip.raw_at_alert(row, flip_idx)
            if action:
                stake = position_stake(row)
                pnl = position_pnl_yes(entry, parsed, stake)
                signals.append({
                    "question": row.get("question", ""),
                    "condition_id": cid,
                    "side": "YES",
                    "entry_no": entry,          # для форматтера: цена входа
                    "current_no": round(parsed, 4),
                    "stake": stake,
                    "action": action,
                    "current_edge": None,
                    "grok_raw": grok_raw,
                    "grok_shown": row.get("ai_yes_estimate"),
                    **pnl,
                })
            continue
        cid = row.get("condition_id", "")
        if not cid:
            continue
        cur = current_no_price(cid, fetch_fn)
        if cur is None:
            continue
        entry = entry_no_price(row)
        if entry is None:
            continue
        stake = position_stake(row)
        ai_yes = row.get("ai_yes_estimate")
        # Re-marked edge: market now prices YES at (1 - current_no); compare to AI.
        current_edge = None
        try:
            current_edge = round((1.0 - cur) - float(ai_yes), 4)
        except (TypeError, ValueError):
            pass
        action = decide_exit(cur, current_edge)
        if action:
            pnl = position_pnl(entry, cur, stake)
            signals.append({
                "question": row.get("question", ""),
                "condition_id": cid,
                "entry_no": entry,
                "current_no": round(cur, 4),
                "stake": stake,
                "action": action,
                "current_edge": current_edge,
                **pnl,
            })
    return signals


def _format_signal(s: dict) -> str:
    label = {
        "CLOSE_FULL": "🟢 ЗАКРЫВАЙ ПОЛНОСТЬЮ",
        "TAKE_PARTIAL": "🟡 ЗАБЕРИ ЧАСТЬ",
        "CUT": "🔴 РЕЖЬ (edge развернулся)",
        "CLOSE_FLIP": "🟠 НЕ СИГНАЛ СИСТЕМЫ — алерт был ошибочным",
    }.get(s["action"], s["action"])
    # CUT в плюсе — не «цена против нас», а конвергенция: рынок сошёлся к
    # AI-оценке, остаток edge исчерпан. Действие то же (выход по правилу),
    # но паническая маркировка на прибыльной позиции дезориентирует (кейс
    # 15.07: NO 42→68¢, +61%, а сигнал кричал «РЕЖЬ»).
    if s["action"] == "CUT" and s.get("ret_pct", 0) > 0:
        label = "🟡 EDGE ИСЧЕРПАН (рынок сошёлся — фиксируй по правилу)"
    why = ""
    if s["action"] == "CLOSE_FLIP" and s.get("grok_raw") is not None:
        shown = s.get("grok_shown")
        shown_txt = f" · в алерте было {float(shown)*100:.0f}%" if shown is not None else ""
        why = (f"Grok на самом деле: {float(s['grok_raw'])*100:.0f}%{shown_txt} "
               f"(баг калибровки, исправлен 04.10)\n"
               f"Преимущества нет ни в одну сторону: держать или продать — решай "
               f"по текущей цене. Сообщение разовое; оставленную позицию можно "
               f"пометить ручной (manual_positions.json)\n")
    return (
        f"{label}\n{s['question']}\n"
        f"—————————————————————\n"
        f"{why}"
        f"Вход {'YES' if str(s.get('side','NO')).upper()=='YES' else 'NO'} "
        f"{s['entry_no']*100:.0f}% → сейчас {s['current_no']*100:.0f}% "
        f"({s['ret_pct']:+.0f}%)\n"
        f"Ставка ${s['stake']:.0f} · нереализ. P&L ${s['unrealised']:+.2f}"
    )


def _should_send(seen: dict, cid: str, action: str, now) -> bool:
    """CLOSE_FLIP — разово: это не изменение рынка, а сведение о прошлом
    алерте, повтор каждые сутки — шум (решение оператора 05.10.2026).
    Остальные действия — по exit_dedup (суточный повтор, эскалация сразу)."""
    import exit_dedup
    if action == "CLOSE_FLIP":
        if (seen.get(cid) or {}).get("action") == "CLOSE_FLIP":
            return False
        seen[cid] = {"action": action, "ts": now.isoformat()}
        return True
    return exit_dedup.should_notify(seen, cid, action, now)


def run() -> None:
    rows = _load_journal()
    open_n = sum(1 for r in rows if _is_open(r))
    print(f"[{datetime.now(timezone.utc).isoformat()}] mark-to-market: "
          f"{open_n} open positions")

    # Экспозиция по категориям — только видимость (доллары и число ставок).
    # Кап в % банка убран 04.10.2026 вместе с банком: ставка фиксированная.
    try:
        import category_exposure as cx
        print("  " + cx.format_exposure(cx.exposure_by_category(rows),
                                        counts=cx.exposure_counts(rows)))
    except Exception as e:
        print(f"  exposure calc failed: {e}")

    calib_rows = []
    try:
        calib_path = Path("calibration_journal.jsonl")
        if calib_path.exists():
            for line in calib_path.read_text().splitlines():
                if line.strip():
                    try:
                        calib_rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception as e:  # noqa: BLE001 — без журнала просто нет CLOSE_FLIP
        print(f"  calibration journal unreadable: {e}")
    import manual_positions
    signals = scan_open_positions(rows, calib_rows=calib_rows,
                                  manual=manual_positions.load())
    if not signals:
        print("  no exit signals — all open positions still maturing.")
        return

    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        import requests
        creds = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    except Exception:
        creds = False

    def _tg(msg: str) -> None:
        if creds:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                    timeout=10,
                ).raise_for_status()
            except Exception as e:
                print(f"  ❌ send failed: {e}")
        else:
            print("  (no telegram creds) " + msg.replace("\n", " | "))

    from datetime import datetime as _dt, timezone as _tz
    import exit_dedup
    seen = exit_dedup.load_seen()
    now_dt = _dt.now(_tz.utc)

    # Дедуп: тот же сигнал по той же позиции — не чаще раза в сутки,
    # эскалация действия шлётся сразу (баг 15.07: дубли каждый 2ч-крон).
    emitted = 0
    for s in signals:
        if not _should_send(seen, s["condition_id"],
                                        s["action"], now_dt):
            print(f"  (dedup) {s['question'][:40]} — {s['action']} уже слали")
            continue
        _tg(_format_signal(s))
        emitted += 1
    exit_dedup.save_seen(seen)
    print(f"  {emitted} exit signal(s) emitted, "
          f"{len(signals) - emitted} deduped.")


if __name__ == "__main__":
    run()
