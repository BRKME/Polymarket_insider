"""Брифинг для решений оператора: выборы и геополитика.

Решение 04.11.2026 (аудит, политика §0.5): сигналы бота за 40 рынков не
обогнали рынок (+1пп, шум ±8пп), реальные деньги по ним −20%; весь плюс
кошелька — собственные ставки оператора на выборы и геополитику. Поэтому бот
больше не зовёт в ставку, а находит рынки этих категорий и приносит сводку
фактов. Купить или нет — решает оператор.

Что попадает в брифинг: выборы/геополитика, YES 10-90¢, резолв через 1-60
дней, ликвидность ≥ $25k. Одно сообщение на событие (у многоисходных событий —
самый ликвидный исход, остальные строкой), не больше BRIEFS_PER_SCAN за скан.
Повтор — только если цена ушла на ≥15пп и прошло ≥3 дней.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import category_exposure as cx

SEEN_FILE = Path("brief_seen.json")

BRIEF_CATEGORIES = {"elections", "geopolitics"}
BRIEF_CATEGORY_RU = {"elections": "выборы", "geopolitics": "геополитика"}
YES_MIN, YES_MAX = 0.10, 0.90
DAYS_MIN, DAYS_MAX = 1, 60
MIN_LIQUIDITY = 25_000
BRIEFS_PER_SCAN = 2          # поиск Grok ~5¢ за вызов: 2 скана в день → ≤$6/мес
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


def _eligible(m: dict, now: datetime) -> bool:
    if category(m) not in BRIEF_CATEGORIES:
        return False
    y, end = yes_price(m), _end(m)
    if y is None or end is None or not YES_MIN <= y <= YES_MAX:
        return False
    days = (end - now).total_seconds() / 86400
    try:
        liq = float(m.get("liquidity") or 0)
    except (TypeError, ValueError):
        liq = 0
    return DAYS_MIN <= days <= DAYS_MAX and liq >= MIN_LIQUIDITY


def _due(cid: str, ev: str, price: float, seen: dict, now: datetime) -> bool:
    """Брифинг ещё не отправлялся — или цена сильно ушла с прошлого раза."""
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
                   limit: int = BRIEFS_PER_SCAN) -> List[dict]:
    by_event: dict = {}
    for m in markets:
        if not _eligible(m, now):
            continue
        ev = event_slug(m)
        best = by_event.get(ev)
        if best is None or float(m.get("liquidity") or 0) > float(best.get("liquidity") or 0):
            by_event[ev] = m
    picked = [m for ev, m in by_event.items()
              if _due(m.get("conditionId", ""), ev, yes_price(m), seen, now)]
    picked.sort(key=lambda m: (_end(m), -float(m.get("liquidity") or 0)))
    return picked[:limit]


# ── ответ Grok ──────────────────────────────────────────────────────────────

def _field(text: str, name: str) -> str:
    m = re.search(rf"^{name}:\s*(.*?)(?=^[A-Z_]+:|\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_brief(text: Optional[str]) -> Optional[dict]:
    if not text or "FACTS:" not in text:
        return None
    facts = [re.sub(r"^[-•*\s]+", "", l).strip()
             for l in _field(text, "FACTS").splitlines()]
    facts = [f for f in facts if f][:5]
    if not facts:
        return None
    prob = None
    pm = re.search(r"^PROB:\s*([0-9]{1,3}(?:\.[0-9]+)?)", text, re.M)
    if pm:
        prob = max(0.0, min(1.0, float(pm.group(1)) / 100.0))
    cm = re.search(r"^CONF:\s*(low|medium|high)", text, re.M | re.I)
    return {"facts": facts, "decides": _field(text, "DECIDES"),
            "next": _field(text, "NEXT"), "risks": _field(text, "RISKS"),
            "prob": prob, "conf": cm.group(1).lower() if cm else "low"}


# ── текст ───────────────────────────────────────────────────────────────────

def _cents(p: float) -> str:
    return f"{p * 100:.0f}¢"


def format_brief(m: dict, brief: dict, price_7d: Optional[float], now: datetime,
                 others: Optional[List[Tuple[str, float]]] = None) -> str:
    y = yes_price(m)
    end = _end(m)
    days = max(0, int((end - now).total_seconds() // 86400)) if end else None
    cat = BRIEF_CATEGORY_RU.get(category(m), category(m))
    mkt = [f"YES {_cents(y)}"]
    if price_7d is not None:
        d = round((y - price_7d) * 100)
        if d:
            mkt.append(f"{'▲' if d > 0 else '▼'}{abs(d)}пп за 7д")
        else:
            mkt.append("за 7д без изменений")
    if end:
        mkt.append(f"резолв {end:%d.%m} (через {days} дн.)")
    lines = [f"📋 Брифинг · {cat}", m.get("question", ""), "Рынок: " + " · ".join(mkt)]
    if others:
        lines.append("Другие исходы: " + ", ".join(f"{n} {_cents(p)}" for n, p in others))
    lines.append("—————————————————————")
    lines.append(f"Что известно (поиск Grok, {now:%d.%m}):")
    lines += [f"• {f}" for f in brief["facts"]]
    for label, key in (("Что решит исход", "decides"), ("Ближайшее", "next"),
                       ("Риски", "risks")):
        if brief.get(key):
            lines.append(f"{label}: {brief[key]}")
    lines.append("—————————————————————")
    if brief.get("prob") is not None:
        lines.append(f"Grok: {brief['prob'] * 100:.0f}% ({brief['conf']}) — справочно: на 40 "
                     "прошлых рынках его оценка рынок не обгонял")
    lines.append("Это не сигнал — решение за тобой.")
    slug = event_slug(m)
    if slug and not slug.startswith("0x"):
        lines.append(f"https://polymarket.com/event/{slug}")
    return "\n".join(lines)


def _others(chosen: dict, markets: List[dict]) -> List[Tuple[str, float]]:
    ev, out = event_slug(chosen), []
    for m in markets:
        if m is chosen or event_slug(m) != ev:
            continue
        y = yes_price(m)
        if y is None or y < 0.02:
            continue
        name = str(m.get("groupItemTitle") or m.get("question") or "")[:40]
        out.append((name, y))
    return sorted(out, key=lambda x: -x[1])[:3]


# ── прогон ──────────────────────────────────────────────────────────────────

def load_seen() -> dict:
    try:
        return json.loads(SEEN_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0))


def run_briefs(markets: List[dict], seen: dict, now: datetime,
               send_fn: Callable[[str], bool],
               brief_fn: Callable[[str, str, str], Optional[dict]],
               price_ago_fn: Callable[[dict, int], Optional[float]],
               limit: int = BRIEFS_PER_SCAN) -> int:
    """Отправить брифинги по отобранным рынкам. Возвращает число отправленных.

    Без ответа Grok брифинг не шлётся и не отмечается — следующий скан
    попробует снова (сводка без фактов бесполезна)."""
    sent = 0
    for m in select_markets(markets, seen, now, limit=limit):
        brief = brief_fn(m.get("question", ""), m.get("description", ""),
                         m.get("endDate", ""))
        if not brief:
            continue
        msg = format_brief(m, brief, price_ago_fn(m, 168), now, _others(m, markets))
        if send_fn(msg):
            sent += 1
            seen[m.get("conditionId", "")] = {"price": yes_price(m), "ts": now.isoformat(),
                                              "event": event_slug(m)}
    return sent
