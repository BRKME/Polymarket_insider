"""Брифинг для решений оператора: выборы и геополитика.

Решение 04.11.2026 (аудит, политика §0.5, §2): сигналы бота за 40 рынков не
обогнали рынок (+1пп, шум ±8пп), реальные деньги по ним −20%; весь плюс
кошелька — собственные ставки оператора на выборы и геополитику. Бот больше
не зовёт в ставку. Grok выключен: политика решается неделями, а не часами, —
раз в день оператор запускает разбор в сессии Claude по инструкции
.claude/skills/daily-brief/SKILL.md.

    python3 briefing.py queue [--limit N]   рынки на разбор (без ИИ)
    python3 briefing.py log --cid ... --my-yes 0.4 --conf medium --note "..."
    python3 briefing.py score              оценки разбора против рынка

Отбор: выборы/геополитика, YES 10-90¢, резолв через 1-60 дней, ликвидность
≥ $25k; одна строка на событие (самый ликвидный исход, остальные — списком).
Разобранный рынок возвращается в очередь, только если цена ушла на ≥15пп и
прошло ≥3 дней. Журнал оценок research/brief_log.jsonl сверяется с резолвами:
без этого «разбор помогает» — вера, а не факт.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import category_exposure as cx

LOG_FILE = Path("research/brief_log.jsonl")

BRIEF_CATEGORIES = {"elections", "geopolitics"}
BRIEF_CATEGORY_RU = {"elections": "выборы", "geopolitics": "геополитика"}
YES_MIN, YES_MAX = 0.10, 0.90
DAYS_MIN, DAYS_MAX = 1, 60
MIN_LIQUIDITY = 25_000
QUEUE_LIMIT = 8
REBRIEF_MOVE = 0.15
REBRIEF_MIN_DAYS = 3


def _json_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


def yes_price(m: dict) -> Optional[float]:
    try:
        outs = [str(o).strip().lower() for o in _json_list(m.get("outcomes"))]
        return float(_json_list(m.get("outcomePrices"))[outs.index("yes")])
    except (ValueError, IndexError, TypeError):
        return None


def event_slug(m: dict) -> str:
    ev = m.get("events")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict) and ev[0].get("slug"):
        return str(ev[0]["slug"])
    return str(m.get("eventSlug") or m.get("conditionId") or "")


def _end(m: dict) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def category(m: dict) -> str:
    return cx.classify(m.get("question", ""), event_slug(m))


def _liq(m: dict) -> float:
    try:
        return float(m.get("liquidity") or 0)
    except (TypeError, ValueError):
        return 0.0


def _eligible(m: dict, now: datetime) -> bool:
    if category(m) not in BRIEF_CATEGORIES:
        return False
    y, end = yes_price(m), _end(m)
    if y is None or end is None or not YES_MIN <= y <= YES_MAX:
        return False
    days = (end - now).total_seconds() / 86400
    return DAYS_MIN <= days <= DAYS_MAX and _liq(m) >= MIN_LIQUIDITY


def _due(cid: str, ev: str, price: float, seen: dict, now: datetime) -> bool:
    """Рынок ещё не разбирался — или цена сильно ушла с прошлого разбора."""
    prev = seen.get(cid)
    if prev is None:
        # событие уже разобрано через другой исход — не дублировать
        return not any(v.get("event") == ev for v in seen.values())
    try:
        moved = abs(price - float(prev["price"])) >= REBRIEF_MOVE
        age = now - datetime.fromisoformat(prev["ts"])
    except (KeyError, TypeError, ValueError):
        return False
    return moved and age >= timedelta(days=REBRIEF_MIN_DAYS)


def select_markets(markets: List[dict], seen: dict, now: datetime,
                   limit: int = QUEUE_LIMIT) -> List[dict]:
    by_event: dict = {}
    for m in markets:
        if not _eligible(m, now):
            continue
        ev = event_slug(m)
        if ev not in by_event or _liq(m) > _liq(by_event[ev]):
            by_event[ev] = m
    picked = [m for ev, m in by_event.items()
              if _due(m.get("conditionId", ""), ev, yes_price(m), seen, now)]
    picked.sort(key=lambda m: (_end(m), -_liq(m)))
    return picked[:limit]


# ── очередь ─────────────────────────────────────────────────────────────────

def _others(chosen: dict, markets: List[dict]) -> List[list]:
    ev, out = event_slug(chosen), []
    for m in markets:
        if m is chosen or event_slug(m) != ev:
            continue
        y = yes_price(m)
        if y is None or y < 0.02:
            continue
        out.append([str(m.get("groupItemTitle") or m.get("question") or "")[:40], y])
    return sorted(out, key=lambda x: -x[1])[:5]


def build_queue(markets: List[dict], seen: dict, now: datetime,
                price_ago_fn: Callable[[dict, int], Optional[float]],
                limit: int = QUEUE_LIMIT) -> List[dict]:
    out = []
    for m in select_markets(markets, seen, now, limit=limit):
        end = _end(m)
        slug = event_slug(m)
        out.append({
            "condition_id": m.get("conditionId", ""),
            "event": slug,
            "category": category(m),
            "question": m.get("question", ""),
            "yes": yes_price(m),
            "yes_7d_ago": price_ago_fn(m, 168),
            "end_date": end.date().isoformat() if end else None,
            "days_left": int((end - now).total_seconds() // 86400) if end else None,
            "liquidity": round(_liq(m)),
            "others": _others(m, markets),
            "rules": str(m.get("description") or "").strip(),
            "url": f"https://polymarket.com/event/{slug}" if slug and not slug.startswith("0x") else "",
        })
    return out


def _cents(p: float) -> str:
    return f"{p * 100:.0f}¢"


def queue_markdown(queue: List[dict]) -> str:
    if not queue:
        return "Очередь пуста: новых рынков по выборам и геополитике нет."
    lines = []
    for i, e in enumerate(queue, 1):
        mv = ""
        if e.get("yes_7d_ago") is not None:
            d = round((e["yes"] - e["yes_7d_ago"]) * 100)
            mv = f" · {'▲' if d > 0 else '▼' if d < 0 else '='}{abs(d)}пп за 7д"
        lines.append(f"## {i}. {e['question']}")
        lines.append(f"{BRIEF_CATEGORY_RU.get(e['category'], e['category'])} · YES "
                     f"{_cents(e['yes'])}{mv} · резолв {e['end_date']} "
                     f"(через {e['days_left']} дн.) · ликвидность ${e['liquidity']:,}")
        if e.get("others"):
            lines.append("Другие исходы: " + ", ".join(f"{n} {_cents(p)}" for n, p in e["others"]))
        lines.append(f"cid: `{e['condition_id']}` · {e['url']}")
        rules = e.get("rules") or ""
        lines.append("Правила: " + (rules[:1200] + ("…" if len(rules) > 1200 else "")))
        lines.append("")
    return "\n".join(lines)


# ── светофор ────────────────────────────────────────────────────────────────

FAIR_PP = 5        # расхождение меньше — рынок оценён справедливо
STRONG_PP = 10     # не меньше (и conf ≥ medium) — сильный перекос


def verdict(my_yes: float, market_yes: float, conf: str) -> str:
    """Строка-итог по рынку (запрос оператора 04.11: «не понятна рекомендация»).

    Первым словом — вердикт (оператор: «в начале нужен вердикт»):
    ⚪ ПРОПУСТИТЬ, 🟡 НАБЛЮДАТЬ (слабый перекос), 🟢 ПРИСМОТРЕТЬСЯ (сильный).
    Дальше — вероятности в процентах (оператор 05.11: «вероятности в %, а не
    55 центов»), а при перекосе — какая сторона дешевле и сколько она стоит по
    разбору, в центах, как в интерфейсе (оператор 07.11: «зачем мне 20%, что
    мне с ними делать?»): «NO стоит 74¢, по разбору 80¢». Зелёный — только при
    conf medium/high: low значит «фактов мало», звать громко по такому нельзя.
    """
    gap = round((my_yes - market_yes) * 100)
    probs = f"рынок {market_yes * 100:.0f}% · разбор {my_yes * 100:.0f}%"
    if abs(gap) < FAIR_PP:
        return f"⚪ ПРОПУСТИТЬ · {probs}"
    if gap > 0:
        side, price, mine = "YES", market_yes, my_yes
    else:
        side, price, mine = "NO", 1 - market_yes, 1 - my_yes
    strong = abs(gap) >= STRONG_PP and conf in ("medium", "high")
    word = "🟢 ПРИСМОТРЕТЬСЯ" if strong else "🟡 НАБЛЮДАТЬ"
    return (f"{word} · {probs} · {side} стоит {price * 100:.0f}¢, "
            f"по разбору {mine * 100:.0f}¢")


# ── журнал оценок ───────────────────────────────────────────────────────────

def load_log(path: Path = LOG_FILE) -> List[dict]:
    try:
        return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    except OSError:
        return []


def append_log(path: Path, row: dict) -> None:
    for k in ("market_yes", "my_yes"):
        v = row.get(k)
        if v is None or not 0.0 <= float(v) <= 1.0:
            raise ValueError(f"{k} должен быть долей 0..1, получено {v!r}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def seen_from_log(rows: List[dict]) -> Dict[str, dict]:
    seen: Dict[str, dict] = {}
    for r in rows:                     # последняя запись по рынку — актуальная
        seen[r["condition_id"]] = {"price": r.get("market_yes"), "ts": r.get("ts"),
                                   "event": r.get("event", "")}
    return seen


def score(rows: List[dict], outcome_fn: Callable[[str], Optional[int]]) -> dict:
    """Brier разбора против рынка на зарезолвленных рынках.

    Одна точка на рынок — ПЕРВАЯ оценка (повторный разбор того же рынка —
    не новое наблюдение: урок псевдорепликации, политика §4)."""
    first: Dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: str(r.get("ts"))):
        first.setdefault(r["condition_id"], r)
    pairs = []
    for cid, r in first.items():
        y = outcome_fn(cid)
        if y is None:
            continue
        pairs.append((float(r["my_yes"]), float(r["market_yes"]), int(y)))
    n = len(pairs)
    if not n:
        return {"n": 0, "pending": len(first)}
    return {"n": n, "pending": len(first) - n,
            "brier_mine": sum((m - y) ** 2 for m, _, y in pairs) / n,
            "brier_market": sum((p - y) ** 2 for _, p, y in pairs) / n}


def _outcome(cid: str) -> Optional[int]:
    import journal_resolver as jr
    m = jr._default_fetch(cid)
    res = jr._binary_outcome(m) if m else None
    return {"Yes": 1, "No": 0}.get(res)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _live_markets() -> List[dict]:
    """Рынки Gamma. Болтовня коллектора — в stderr: иначе `queue --json`
    начинался со строки лога и не парсился (первый разбор 04.11)."""
    import contextlib
    import collector
    with contextlib.redirect_stdout(sys.stderr):
        return collector.get_active_markets(limit=1000)


def _price_ago(m: dict, hours: int) -> Optional[float]:
    import scan_events as se
    return se._price_ago_clob(se._yes_token(m), datetime.now(timezone.utc),
                              lo_h=hours - 12, hi_h=hours + 12)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue")
    q.add_argument("--limit", type=int, default=QUEUE_LIMIT)
    q.add_argument("--json", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("--cid", required=True)
    lg.add_argument("--my-yes", type=float, required=True, help="доля 0..1")
    lg.add_argument("--conf", choices=["low", "medium", "high"], required=True)
    lg.add_argument("--note", default="")
    sub.add_parser("score")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)

    if a.cmd == "queue":
        queue = build_queue(_live_markets(), seen_from_log(load_log()), now,
                            price_ago_fn=_price_ago, limit=a.limit)
        print(json.dumps(queue, ensure_ascii=False, indent=1) if a.json
              else queue_markdown(queue))
        return 0

    if a.cmd == "log":
        m = next((x for x in _live_markets() if x.get("conditionId") == a.cid), None)
        if m is None:
            import journal_resolver as jr
            m = jr._default_fetch(a.cid)
        if not m or yes_price(m) is None:
            print(f"рынок {a.cid} не найден — оценка не записана", file=sys.stderr)
            return 1
        end = _end(m)
        append_log(LOG_FILE, {
            "ts": now.isoformat(), "condition_id": a.cid, "event": event_slug(m),
            "question": m.get("question", ""), "category": category(m),
            "market_yes": yes_price(m), "my_yes": a.my_yes, "conf": a.conf,
            "end_date": end.date().isoformat() if end else None, "note": a.note})
        print(f"записано: {m.get('question', '')[:60]} · рынок {yes_price(m):.2f} · "
              f"разбор {a.my_yes:.2f} ({a.conf})")
        print("🚦 " + verdict(a.my_yes, yes_price(m), a.conf))
        return 0

    s = score(load_log(), _outcome)
    if not s["n"]:
        print(f"Зарезолвленных разборов пока нет (ждут резолва: {s['pending']}).")
    else:
        print(f"Разборов зарезолвлено: {s['n']} (ждут: {s['pending']})\n"
              f"Brier разбора {s['brier_mine']:.3f} · рынка {s['brier_market']:.3f} "
              f"(меньше — лучше; при n<30 разница — шум)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
