"""
fill_matcher.py — auto-fill ACTUAL entry price & stake into the journal from
Polymarket's Data API. No manual entry: we read our own on-chain trades and
match them to open journal positions.

Why this is honest (not the alert-price proxy we're trying to escape):
the /activity endpoint returns OUR settled trades by proxy wallet, each with
the real fill `price` and `usdcSize` (dollars actually spent). We match on
`conditionId` (identical to the journal's condition_id — verified live), NO
side, BUY. Several partial entries on one market are aggregated into a
size-weighted average price and a summed stake.

If no matching trade exists (didn't bet, or bet differently), the position is
left UNCONFIRMED — never back-filled with a made-up price. verify_journal
already prefers entry_price_actual/stake_actual when present, so confirmed
positions score on real fills and the rest stay out of the count.

Read-only against the API. Address is public. No keys.

Run:  python fill_matcher.py
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, List, Dict

JOURNAL = Path("event_journal.jsonl")
DATA_API = "https://data-api.polymarket.com"
# Public proxy wallet whose trades we read. Override via env if it ever changes.
import os
PROXY_WALLET = os.getenv("POLYMARKET_PROXY_WALLET") or "0xbd8338C6D4e1E25B28D1A95Db926D2CeF689632f"  # пустой секрет в Actions = отсутствующий


# ── pure matching logic (no network) ────────────────────────────────────────


DEBUG = os.getenv("DEBUG_FILLS") == "1"


def _debug_trades(cid: str, trades: list) -> None:
    """Печать причин отказа по каждому трейду — для разовой диагностики."""
    print(f"  [debug] {cid[:14]}…: {len(trades)} activity rows")
    for t in trades[:8]:
        why = []
        if t.get("conditionId") != cid:
            why.append("cid≠")
        if str(t.get("side", "")).upper() != "BUY":
            why.append(f"side={t.get('side')}")
        if str(t.get("outcome", "")).strip().lower() != "no":
            why.append(f"outcome={t.get('outcome')!r}")
        print(f"    type={t.get('type')} side={t.get('side')} "
              f"outcome={t.get('outcome')!r} px={t.get('price')} "
              f"usdc={t.get('usdcSize')} -> {'OK' if not why else ' '.join(why)}")


def match_position(condition_id: str, trades: List[Dict],
                   side: str = "NO") -> Optional[Dict]:
    """Aggregate our BUY trades on `condition_id` for `side` into one fill.

    side: "NO" (осн. стратегия) или "YES" (средняя зона). Полевой баг 08.08.2026:
    функция жёстко искала outcome='no', поэтому реальные YES-покупки оператора
    не сопоставлялись — stake_actual не проставлялся, позиции считались
    фантомами и не мониторились вовсе.

    Returns {entry_price_actual, stake_actual, shares_actual, fill_txs,
             fill_source, filled_at} where entry_price is size-weighted.
    """
    want = "yes" if str(side).upper() == "YES" else "no"
    mine = []
    for t in trades:
        if t.get("conditionId") != condition_id:
            continue
        if str(t.get("side", "")).upper() != "BUY":
            continue
        if str(t.get("outcome", "")).strip().lower() != want:
            continue
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0 or not (0 < price < 1):
            continue
        # usdcSize is the dollars actually spent; fall back to size*price.
        try:
            usdc = float(t.get("usdcSize"))
            if usdc <= 0:
                usdc = size * price
        except (TypeError, ValueError):
            usdc = size * price
        mine.append((size, price, usdc, t.get("transactionHash", "")))

    if not mine:
        return None

    total_size = sum(s for s, _, _, _ in mine)
    total_usdc = sum(u for _, _, u, _ in mine)
    if total_size <= 0:
        return None
    vwap = sum(s * p for s, p, _, _ in mine) / total_size
    return {
        "entry_price_actual": round(vwap, 6),
        "stake_actual": round(total_usdc, 6),
        "shares_actual": round(total_size, 6),
        "fill_txs": len(mine),
        "fill_source": "onchain",
        "filled_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_fills(rows: List[Dict],
                fetch_trades_fn: Callable[[str], List[Dict]]) -> tuple:
    """Fill open, not-yet-filled positions. Returns (rows, n_filled).

    Skips closed positions and ones already carrying an onchain fill (so we
    never overwrite a confirmed entry). Unmatched positions are left untouched
    (unconfirmed) — no made-up price.
    """
    n = 0
    for r in rows:
        if str(r.get("status", "open")).lower() != "open":
            continue
        if r.get("fill_source") == "onchain" and r.get("entry_price_actual"):
            continue                      # already confirmed
        cid = r.get("condition_id", "")
        if not cid:
            continue
        try:
            trades = fetch_trades_fn(cid) or []
        except Exception:
            continue
        if DEBUG:
            _debug_trades(cid, trades)
        fill = match_position(cid, trades, side=str(r.get("side", "NO")))
        if fill:
            r.update(fill)
            n += 1
    return rows, n


# ── network ──────────────────────────────────────────────────────────────────

_ACTIVITY_CACHE: Optional[List[Dict]] = None
API_FAILED = False


def _fetch_all_activity() -> List[Dict]:
    """Вся активность кошелька одним запросом (кэш на прогон).

    Продакшн-вариант с параметрами market+type возвращал HTTP 400 на каждый
    cid (диагностика 11.06, fills_debug) — Data API отвергает такую комбинацию.
    Рабочая диагностика 10.06 брала /activity только с user+limit и матчила
    conditionId на клиенте; делаем так же. Бонус: 1 запрос вместо N.
    """
    global _ACTIVITY_CACHE
    if _ACTIVITY_CACHE is not None:
        return _ACTIVITY_CACHE
    import data_api
    global API_FAILED
    rows = data_api.fetch("activity", {"user": PROXY_WALLET}, max_rows=5000)
    if rows is None:
        # v1 выключили 02.11.2026 — и это две недели было лишь строкой в логе.
        # Теперь прогон краснеет, и workflow_watchdog сообщает в канал.
        API_FAILED = True
        print(f"  ⚠️ Data API: {data_api.LAST_ERROR}")
    out: List[Dict] = rows or []
    _ACTIVITY_CACHE = out
    print(f"  activity rows fetched: {len(out)}")
    return out


def _fetch_trades(condition_id: str) -> List[Dict]:
    """Наши TRADE-строки по одному рынку — клиентский фильтр общей активности."""
    return [t for t in _fetch_all_activity()
            if t.get("conditionId") == condition_id
            and str(t.get("type", "")).upper() == "TRADE"]


def _load_journal() -> List[Dict]:
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


def _save_journal(rows: List[Dict]) -> None:
    with JOURNAL.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run() -> None:
    rows = _load_journal()
    if not rows:
        print("No journal — nothing to fill.")
        return
    open_n = sum(1 for r in rows if str(r.get("status", "open")).lower() == "open")
    print(f"[{datetime.now(timezone.utc).isoformat()}] fill-match: "
          f"{open_n} open positions")
    rows, n = apply_fills(rows, fetch_trades_fn=_fetch_trades)

    # Сверка с реальным портфелем: позиции, которых там больше нет, оператор
    # продал сам. Без этого журнал расходится с реальностью при каждом ручном
    # выходе (09.08.2026: журнал числил $109.61 при портфеле $84.89).
    sold_n = 0
    try:
        import position_sync as ps
        from daily_status import _fetch_positions
        positions = _fetch_positions() or []
        if positions:
            rows, sold_n = ps.apply_sold(rows, positions)
            if sold_n:
                print(f"  🔻 обнаружено продаж: {sold_n} — помечены закрытыми")
        else:
            print("  портфель недоступен — сверка продаж пропущена (защита)")
    except Exception as e:
        print(f"  сверка продаж не выполнена: {e}")

    if n or sold_n:
        _save_journal(rows)
        if n:
            print(f"  ✅ auto-filled {n} position(s) from on-chain trades.")
    else:
        print("  no new fills matched (positions stay unconfirmed).")


if __name__ == "__main__":
    run()
    if API_FAILED:
        raise SystemExit("Data API недоступен — входы не сверены")
