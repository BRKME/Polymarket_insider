"""
ai_cache.py — cache Grok estimates per condition_id.

The scanner runs every 6h and re-evaluates the same slow-moving event markets,
paying for a fresh Grok call each time. Event prices move slowly, so a recent
estimate is reusable UNLESS:
  • it's older than MAX_AGE_SECONDS, or
  • the market's YES price has moved by more than PRICE_MOVE_THRESHOLD
    (a real move can change the true probability — re-estimate).

Pure validity logic (is_valid) + a wrapper factory (make_cached_estimator) that
sits in front of estimate_probability. Store is a plain dict the caller
persists to disk between runs.
"""
from __future__ import annotations
import time
from typing import Callable, Optional, Dict

MAX_AGE_SECONDS = 7 * 24 * 3600     # 7 days
PRICE_MOVE_THRESHOLD = 0.05         # 5pp YES-price move invalidates the cache


def is_valid(entry: dict, now_epoch: float, current_yes: float) -> bool:
    """Is a cached estimate still usable at the current time & price?"""
    if not entry:
        return False
    cached_at = entry.get("cached_at_epoch")
    yes_price = entry.get("yes_price")
    if cached_at is None or yes_price is None or entry.get("prob") is None:
        return False
    if (now_epoch - cached_at) > MAX_AGE_SECONDS:
        return False
    try:
        if abs(float(current_yes) - float(yes_price)) > PRICE_MOVE_THRESHOLD:
            return False
    except (TypeError, ValueError):
        return False
    return True


def make_cached_estimator(
    underlying: Callable[..., Optional[dict]],
    store: Dict[str, dict],
    cid_for: Callable[[str], str],
    yes_for: Callable[[str], Optional[float]],
    now_epoch: Callable[[], float] = time.time,
) -> Callable[..., Optional[dict]]:
    """Wrap `underlying` estimator with a per-condition_id cache.

    cid_for(question) -> condition_id (cache key)
    yes_for(question) -> current market YES price (for move-invalidation)
    """
    def _cached(question: str, description: str = None, end_date: str = None):
        cid = cid_for(question)
        now = now_epoch()
        cur_yes = yes_for(question)
        entry = store.get(cid) if cid else None
        if entry and cur_yes is not None and is_valid(entry, now, cur_yes):
            # return just the estimate fields the scanner expects
            return {"prob": entry["prob"], "conf": entry.get("conf", "low"),
                    "why": entry.get("why", ""),
                    # старые записи кэша без флага — считаем «без поиска»
                    "searched": bool(entry.get("searched", False))}
        # miss -> call underlying (with whatever signature it supports)
        try:
            est = underlying(question, description, end_date)
        except TypeError:
            est = underlying(question)
        if est and est.get("prob") is not None and cid:
            store[cid] = {
                "prob": est.get("prob"),
                "conf": est.get("conf", "low"),
                "why": est.get("why", ""),
                "searched": bool(est.get("searched", False)),
                "yes_price": cur_yes,
                "cached_at_epoch": now,
            }
        return est

    return _cached
