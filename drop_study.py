"""Исследование: цена YES упала в зону 50-65% перед событием — новость или перегиб?

Принципиальный вопрос для YES-стратегии (политика, открытые вопросы): метка
«⚠️ цена упала ≥10пп за сутки при ≤3д до события» — оставить меткой или
сделать запретом. Своих резолвов на это мало (n=1-2), поэтому скрипт берёт
историю закрытых бинарных рынков Polymarket и смотрит, как разрешались рынки,
впервые вошедшие в зону за 72ч до конца, в зависимости от движения цены за
предыдущие сутки: упала (drop) / стояла (flat) / выросла (rise).

Наблюдение на рынок одно — первая часовая точка в окне, где цена в зоне и
есть цена ~сутки назад. Так ведёт себя бот: алертит при первом входе в зону.

Сеть нужна (Gamma + CLOB) — запускается в Actions (drop_study.yml), из
контейнеров разработки API Polymarket закрыт.

Run:  python drop_study.py [--max-markets 3000]
"""
from __future__ import annotations

import argparse
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ZONE = (0.50, 0.65)
WINDOW_H = 72          # окно перед концом рынка
MIN_H_BEFORE = 2       # последние часы — уже почти известный исход
PREV_LO_H, PREV_HI_H = 18, 36   # «сутки назад» — как в боте (_price_ago)
MOVE_PP = 0.10

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
OUT = Path("research/drop_study.json")


# ── чистая логика ────────────────────────────────────────────────────────────

def classify(history: List[dict], end_ts: int) -> Optional[dict]:
    """Первая точка в окне [end−72ч, end−2ч] с ценой в зоне и ценой ~сутки
    назад -> {move: drop|flat|rise, price, delta, hours_before}."""
    pts = sorted((int(h["t"]), float(h["p"])) for h in history
                 if h.get("t") is not None and h.get("p") is not None)
    for t, p in pts:
        hb = (end_ts - t) / 3600
        if hb > WINDOW_H or hb < MIN_H_BEFORE:
            continue
        if not (ZONE[0] <= p <= ZONE[1]):
            continue
        prev = [pp for tt, pp in pts
                if t - PREV_HI_H * 3600 <= tt <= t - PREV_LO_H * 3600]
        if not prev:
            continue
        d = p - prev[-1]
        move = "drop" if d <= -MOVE_PP else "rise" if d >= MOVE_PP else "flat"
        return {"move": move, "price": p, "delta": d, "hours_before": hb}
    return None


def final_outcome(m: dict) -> Optional[int]:
    """1/0 по финальным ценам закрытого Yes/No-рынка, None если не бинарный
    или не разрешён однозначно."""
    try:
        outs = m.get("outcomes")
        prices = m.get("outcomePrices")
        outs = json.loads(outs) if isinstance(outs, str) else outs
        prices = json.loads(prices) if isinstance(prices, str) else prices
        if not outs or len(outs) != 2:
            return None
        labels = [str(o).strip().lower() for o in outs]
        if set(labels) != {"yes", "no"}:
            return None
        yes = float(prices[labels.index("yes")])
    except Exception:
        return None
    if yes >= 0.99:
        return 1
    if yes <= 0.01:
        return 0
    return None


def _stats(group: List[dict]) -> dict:
    n = len(group)
    if not n:
        return {"n": 0}
    yr = sum(o["yes"] for o in group) / n
    ap = sum(o["price"] for o in group) / n
    pnl = sum((o["yes"] / o["price"]) - 1 for o in group) / n
    return {"n": n, "yes_rate": yr, "avg_price": ap, "edge": yr - ap,
            "pnl_per_usd": pnl}


def summarize(obs: List[dict]) -> Dict[str, dict]:
    return {mv: _stats([o for o in obs if o["move"] == mv])
            for mv in ("drop", "flat", "rise")}


def bootstrap_diff(a: List[dict], b: List[dict], iters: int = 2000,
                   seed: int = 7) -> Optional[dict]:
    """95% интервал разницы edge (a − b) бутстрепом."""
    if len(a) < 5 or len(b) < 5:
        return None
    rnd = random.Random(seed)

    def edge(g):
        return sum(o["yes"] - o["price"] for o in g) / len(g)
    diffs = sorted(edge([rnd.choice(a) for _ in a]) - edge([rnd.choice(b) for _ in b])
                   for _ in range(iters))
    return {"diff": edge(a) - edge(b), "lo": diffs[int(0.025 * iters)],
            "hi": diffs[int(0.975 * iters)]}


# ── сеть (только в Actions) ──────────────────────────────────────────────────

def _get(url, params, tries=3):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(1 + i)
    return None


def fetch_closed_markets(max_markets: int, min_volume: float) -> List[dict]:
    out, offset = [], 0
    while len(out) < max_markets:
        page = _get(GAMMA, {"closed": "true", "limit": 500, "offset": offset,
                            "order": "endDate", "ascending": "false",
                            "volume_num_min": min_volume})
        if not page:
            break
        for m in page:
            try:
                if float(m.get("volumeNum") or m.get("volume") or 0) < min_volume:
                    continue
            except (TypeError, ValueError):
                continue
            if final_outcome(m) is None or not m.get("clobTokenIds"):
                continue
            out.append(m)
        offset += 500
        if len(page) < 500 or offset > 50_000:
            break
    return out[:max_markets]


def _yes_token(m: dict) -> Optional[str]:
    try:
        toks = m["clobTokenIds"]
        toks = json.loads(toks) if isinstance(toks, str) else toks
        outs = m["outcomes"]
        outs = json.loads(outs) if isinstance(outs, str) else outs
        return toks[[str(o).lower() for o in outs].index("yes")]
    except Exception:
        return None


def _end_ts(m: dict) -> Optional[int]:
    for k in ("closedTime", "endDate"):
        v = m.get(k)
        if not v:
            continue
        try:
            s = str(v).replace("Z", "+00:00").replace(" ", "T")
            if len(s) > 6 and s[-3] != ":" and ("+" in s[-6:]):
                s = s[:-2] + ":" + s[-2:]
            return int(datetime.fromisoformat(s).timestamp())
        except Exception:
            continue
    return None


def run(max_markets: int, min_volume: float) -> dict:
    import event_scanner as es
    import category_exposure as cx
    markets = fetch_closed_markets(max_markets, min_volume)
    print(f"closed binary markets fetched: {len(markets)}")
    obs, n_hist = [], 0
    for i, m in enumerate(markets):
        tok, end = _yes_token(m), _end_ts(m)
        if not tok or not end:
            continue
        h = _get(CLOB_HISTORY, {"market": tok, "startTs": end - 5 * 86400,
                                "endTs": end, "fidelity": 60})
        hist = (h or {}).get("history") or []
        if not hist:
            continue
        n_hist += 1
        o = classify(hist, end)
        if o:
            q = m.get("question", "")
            o.update({"yes": final_outcome(m), "question": q[:120],
                      "category": cx.classify(q, slug=(m.get("events") or [{}])[0].get("slug")
                                              if m.get("events") else None),
                      "sport": es._is_sport_or_hft(q),
                      "end": datetime.fromtimestamp(end, timezone.utc).date().isoformat()})
            obs.append(o)
        if i % 200 == 0:
            print(f"  {i}/{len(markets)} histories={n_hist} obs={len(obs)}")
        time.sleep(0.05)

    def block(g):
        s = summarize(g)
        s["drop_vs_flat"] = bootstrap_diff([o for o in g if o["move"] == "drop"],
                                           [o for o in g if o["move"] == "flat"])
        return s
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "params": {"zone": ZONE, "window_h": WINDOW_H, "move_pp": MOVE_PP,
                   "min_volume": min_volume},
        "markets": len(markets), "with_history": n_hist, "observations": len(obs),
        "all": block(obs),
        "non_sport": block([o for o in obs if not o["sport"]]),
        "sport": block([o for o in obs if o["sport"]]),
        "elections": block([o for o in obs if o["category"] == "elections"]),
        "drops": [o for o in obs if o["move"] == "drop"][:200],
    }
    return report


def _print(report: dict) -> None:
    print(f"\nmarkets={report['markets']} with_history={report['with_history']} "
          f"observations={report['observations']}")
    for name in ("all", "non_sport", "sport", "elections"):
        print(f"\n=== {name} ===")
        b = report[name]
        for mv in ("drop", "flat", "rise"):
            s = b[mv]
            if s.get("n"):
                print(f"  {mv:5s} n={s['n']:4d} YES-rate={s['yes_rate']:.3f} "
                      f"avg price={s['avg_price']:.3f} edge={s['edge']:+.3f} "
                      f"pnl/$1 YES={s['pnl_per_usd']:+.3f}")
            else:
                print(f"  {mv:5s} n=0")
        d = b.get("drop_vs_flat")
        if d:
            print(f"  drop−flat edge: {d['diff']:+.3f} (95% CI {d['lo']:+.3f}..{d['hi']:+.3f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=4000)
    ap.add_argument("--min-volume", type=float, default=20_000)
    a = ap.parse_args()
    rep = run(a.max_markets, a.min_volume)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    _print(rep)
