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


# ── разбивка по видам спорта и типам рынков (06.10.2026) ────────────────────
# Вопрос оператора: какие виды спорта включать — только «ограниченные» рынки,
# без киберспорта; где итог предсказуемее. Порядок важен: первый совпавший.
import re as _re

_SPORT_RULES = [
    ("esports", ("lol-", "cs2-", "dota", "val-"),
     r"\b(lck|lpl|msi|worlds|esports|valorant|dota|counter-strike|cs2|league of legends)\b"),
    ("nfl", ("nfl-",), r"\b(nfl|super bowl)\b"),
    ("nba", ("nba-",), r"\b(nba|nba finals)\b"),
    ("mlb", ("mlb-",), r"\b(mlb|world series)\b"),
    ("nhl", ("nhl-",), r"\b(nhl|stanley cup)\b"),
    ("college", ("cbb-", "cfb-", "ncaa"), r"\b(ncaa|march madness|college football)\b"),
    ("tennis", ("atp-", "wta-"),
     r"\b(atp|wta|wimbledon|us open|roland garros|french open|australian open|grand slam)\b"),
    ("mma", ("ufc-", "box-"), r"\b(ufc|boxing|knockout|heavyweight)\b"),
    ("motorsport", ("f1-", "nascar-", "motogp-"), r"\b(f1|formula 1|grand prix|nascar|motogp|drivers' champion|constructors' champion)\b"),
    ("golf", ("pga-", "golf-"), r"\b(pga|masters|open championship|ryder cup|golf|lpga)\b"),
    ("cricket", ("ipl-", "cricket-"), r"\b(ipl|cricket|t20|test match|odi)\b"),
    ("soccer", ("epl-", "ucl-", "uel-", "laliga-", "seriea-", "bundesliga-", "ligue1-",
                "mls-", "fifwc-", "uecl-"),
     r"\b(fc|premier league|champions league|la ?liga|serie a|bundesliga|ligue 1|mls|"
     r"world cup|halftime|both halves|leading at|ballon d'or|uefa|copa)\b"),
]


def sport_kind(question: str, slug: str = "") -> Optional[str]:
    """Вид спорта по слагу события (надёжнее) или тексту; None — не спорт."""
    sl = str(slug or "").lower()
    q = str(question or "").lower()
    for kind, prefixes, rx in _SPORT_RULES:
        if sl.startswith(prefixes) or _re.search(rx, q):
            return kind
    if " vs. " in q or " vs " in q:
        return "other_sport"
    return None


def market_kind(question: str, slug: str = "") -> str:
    """Тип рынка: match (матч один на один), spread_total (фора/тотал),
    period (тайм/период/«ведёт в перерыве»), outright (кто выиграет из многих:
    турнир, гонка, сезон, «вылетит в…»)."""
    q = str(question or "").lower()
    if _re.search(r"o/u|over/under|spread|\(-?\+?\d+\.5\)|[-+]\d+\.5|total", q):
        return "spread_total"
    if _re.search(r"halftime|half|leading at|period|quarter|inning|set \d|1st|first", q):
        return "period"
    if " vs. " in q or " vs " in q:
        return "match"
    return "outright"


def edge_ci(group: List[dict], iters: int = 2000, seed: int = 11) -> Optional[dict]:
    """YES − цена по группе с 95% бутстреп-интервалом."""
    if len(group) < 10:
        return None
    rnd = random.Random(seed)

    def edge(g):
        return sum(o["yes"] - o["price"] for o in g) / len(g)
    boots = sorted(edge([rnd.choice(group) for _ in group]) for _ in range(iters))
    return {"n": len(group), "edge": edge(group),
            "lo": boots[int(0.025 * iters)], "hi": boots[int(0.975 * iters)]}


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


PAGE = 100   # Gamma отдаёт не больше 100 за запрос (прогон 05.10 встал на 99)


WINDOW_DAYS = 7
MAX_PAGES_PER_WINDOW = 20


def fetch_closed_markets(max_markets: int, min_volume: float,
                         now_ts: Optional[int] = None,
                         lookback_days: int = 365) -> List[dict]:
    """Закрытые бинарные рынки, закончившиеся за последние lookback_days.

    Недельными окнами по endDate, от свежих к старым: в одном длинном запросе
    Gamma перестаёт отдавать страницы после нескольких сотен (05.10: 326
    рынков из 4000). end_date_max ≤ сейчас: досрочно закрытые рынки с endDate
    в 2027 иначе уводят окно истории в будущее (05.10: 0 историй из 99).
    """
    now_ts = now_ts or int(time.time())
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, seen = [], set()
    hi = now_ts
    lo_limit = now_ts - lookback_days * 86400
    while hi > lo_limit and len(out) < max_markets:
        lo = max(lo_limit, hi - WINDOW_DAYS * 86400)
        offset, pages = 0, 0
        while pages < MAX_PAGES_PER_WINDOW and len(out) < max_markets:
            page = _get(GAMMA, {"closed": "true", "limit": PAGE, "offset": offset,
                                "order": "endDate", "ascending": "false",
                                "end_date_max": iso(hi), "end_date_min": iso(lo),
                                "volume_num_min": min_volume})
            if not page:
                break
            pages += 1
            for m in page:
                key = m.get("conditionId") or m.get("question")
                if key in seen:
                    continue
                try:
                    if float(m.get("volumeNum") or m.get("volume") or 0) < min_volume:
                        continue
                except (TypeError, ValueError):
                    continue
                if final_outcome(m) is None or not m.get("clobTokenIds"):
                    continue
                end = _end_ts(m)
                if end is None or end > now_ts:
                    continue
                seen.add(key)
                out.append(m)
            offset += len(page)
        _DIAG["windows"] = _DIAG.get("windows", 0) + 1
        hi = lo
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


def _parse_ts(v) -> Optional[int]:
    if not v:
        return None
    s = str(v).strip().replace(" ", "T").replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():   # '+00' -> '+00:00'
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _end_ts(m: dict) -> Optional[int]:
    """Фактический конец торговли: раннее из closedTime и endDate (досрочно
    закрытый рынок живёт по closedTime, endDate у него может быть в 2027)."""
    ts = [t for t in (_parse_ts(m.get("closedTime")), _parse_ts(m.get("endDate")))
          if t is not None]
    return min(ts) if ts else None


def fetch_history(token: str, end: int) -> List[dict]:
    """Часовая история цены YES за 5 суток до конца. Несколько вариантов
    запроса: CLOB по-разному отвечает для закрытых рынков."""
    for params in ({"market": token, "startTs": end - 5 * 86400, "endTs": end,
                    "fidelity": 60},
                   {"market": token, "interval": "max", "fidelity": 60},
                   {"market": token, "interval": "1w", "fidelity": 60}):
        h = _get(CLOB_HISTORY, params)
        hist = (h or {}).get("history") or []
        if hist:
            _DIAG["ok_" + ("range" if "startTs" in params else params["interval"])] += 1
            return hist
    _DIAG["empty"] += 1
    if _DIAG["empty"] <= 3:
        print(f"  [diag] empty history token={token[:12]}… end={end} resp={str(h)[:200]}")
    return []


_DIAG: Dict[str, int] = {"ok_range": 0, "ok_max": 0, "ok_1w": 0, "empty": 0}


def _slug_of(m: dict) -> str:
    ev = (m.get("events") or [{}])[0] if m.get("events") else {}
    return str(ev.get("slug") or m.get("slug") or "")


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
        hist = fetch_history(tok, end)
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
                      "sport_kind": sport_kind(q, _slug_of(m)),
                      "market_kind": market_kind(q, _slug_of(m)),
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
        "history_diag": dict(_DIAG),
        "all": block(obs),
        "non_sport": block([o for o in obs if not o["sport"]]),
        "sport": block([o for o in obs if o["sport"]]),
        "elections": block([o for o in obs if o["category"] == "elections"]),
        "by_sport": {k: edge_ci([o for o in obs if o.get("sport_kind") == k])
                     for k in sorted({o.get("sport_kind") or "-" for o in obs})},
        "by_sport_market": {f"{k}/{t}": edge_ci([o for o in obs if o.get("sport_kind") == k
                                                 and o.get("market_kind") == t])
                            for k in sorted({o.get("sport_kind") for o in obs if o.get("sport_kind")})
                            for t in ("match", "spread_total", "period", "outright")},
        "obs": obs,
    }
    return report


def _print(report: dict) -> None:
    print(f"\nmarkets={report['markets']} with_history={report['with_history']} "
          f"observations={report['observations']} diag={report.get('history_diag')}")
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


def _print_kinds(report: dict) -> None:
    for title, key in (("by sport (zone 50-65, ≤72h)", "by_sport"),
                       ("by sport / market kind", "by_sport_market")):
        print(f"\n=== {title} ===")
        for k, v in sorted((report.get(key) or {}).items(),
                           key=lambda kv: -(kv[1] or {}).get("n", 0)):
            if v:
                print(f"  {k:28s} n={v['n']:4d} YES−price={v['edge']:+.3f} "
                      f"(95% CI {v['lo']:+.3f}..{v['hi']:+.3f})")
    # Примеры рынков по спортивным группам — проверка глазами, что группа
    # не артефакт (прогон 06.10: tennis/match n=57, +48.6пп, CI ±0.5пп).
    obs = report.get("obs") or []
    for k in sorted({o.get("sport_kind") for o in obs if o.get("sport_kind")}):
        g = [o for o in obs if o.get("sport_kind") == k]
        print(f"\n--- samples: {k} (n={len(g)}) ---")
        for o in g[:12]:
            print(f"  yes={o['yes']} p={o['price']:.3f} {o.get('market_kind')} "
                  f"{o.get('end')} {o.get('question','')[:90]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-markets", type=int, default=4000)
    ap.add_argument("--min-volume", type=float, default=20_000)
    a = ap.parse_args()
    rep = run(a.max_markets, a.min_volume)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rep, ensure_ascii=False, indent=1))
    _print(rep)
    _print_kinds(rep)
