"""Polymarket Data API v2 — один вход для всех модулей.

v1 выключен 02.11.2026: каждый v1-маршрут отвечает «Data API v1 has been
retired». Его заметили только 04.11 на аудите — дневной статус ушёл в
журнальный фолбэк, fill_matcher печатал HTTP-ошибку, тревог не было.

v2 отличается: префикс /v2, ответ в {"data", "pagination"}, курсор вместо
offset (offset и market дают HTTP 400), snake_case вместо camelCase.
Здесь всё это прячется: модули передают привычные параметры и получают
строки с прежними camelCase-полями (исходные snake_case тоже остаются).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

BASE = "https://data-api.polymarket.com/v2"
PAGE = 500

# Последняя ошибка API — чтобы вызывающий мог сказать о ней вслух, а не
# деградировать молча (политика §5).
LAST_ERROR: Optional[str] = None

_PARAM_RENAME = {
    "market": "condition", "conditionId": "condition",
    "sortBy": "sort_by", "sortDirection": "sort_direction",
    "timePeriod": "time_period", "orderBy": "sort_by",
}


def _default_get(url, params=None, timeout=None):
    import requests
    return requests.get(url, params=params, timeout=timeout)


def _camel(key: str) -> str:
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), key)


def legacy_row(row: dict) -> dict:
    out = dict(row)
    for k, v in row.items():
        if "_" in k:
            out.setdefault(_camel(k), v)
    return out


def legacy_position(row: dict) -> dict:
    out = legacy_row(row)
    for v1, v2 in (("size", "current_size"), ("curPrice", "current_price"),
                   ("initialValue", "entry_cost_usdc"), ("cashPnl", "unrealized_pnl")):
        if v2 in row:
            out[v1] = row[v2]
    return out


def legacy_leader(row: dict) -> dict:
    out = legacy_row(row)
    out["proxyWallet"] = row.get("user_id")
    out["userName"] = row.get("user_name")
    out["vol"] = row.get("volume")
    return out


def _translate(route: str, params: dict) -> dict:
    p = {}
    for k, v in (params or {}).items():
        if k == "offset" or v is None:
            continue
        if k == "sizeThreshold":
            p["filter_type"], p["filter_amount"] = "TOKENS", v
            continue
        p[_PARAM_RENAME.get(k, k)] = v
    if route == "leaderboard":
        for k in ("time_period", "category"):
            if isinstance(p.get(k), str):
                p[k] = p[k].lower()
    return p


def fetch_page(route: str, params: Optional[dict] = None, cursor: Optional[str] = None,
               timeout: int = 20, get: Callable = _default_get):
    """Одна страница: (строки, next_cursor) — или None при ошибке API.
    Для /value строки — объект (или None)."""
    global LAST_ERROR
    route = route.strip("/")
    q = _translate(route, params or {})
    if route != "value":
        q["limit"] = min(int(q.get("limit") or PAGE), 1000)
    if cursor:
        q["cursor"] = cursor
    try:
        r = get(f"{BASE}/{route}", params=q, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        LAST_ERROR = f"/v2/{route}: {e}"
        return None
    if r.status_code != 200:
        LAST_ERROR = f"/v2/{route}: HTTP {r.status_code} {str(r.text)[:120]}"
        return None
    body = r.json() or {}
    data = body.get("data")
    if route == "value":
        return (legacy_row(data) if isinstance(data, dict) else None), None
    conv = {"positions": legacy_position, "leaderboard": legacy_leader}.get(route, legacy_row)
    return [conv(x) for x in (data or [])], (body.get("pagination") or {}).get("next_cursor")


def fetch(route: str, params: Optional[dict] = None, max_rows: int = PAGE,
          timeout: int = 20, get: Callable = _default_get):
    """Строки маршрута v2 (list) или объект (/value). None при ошибке API.

    Курсор проходится до null или до max_rows строк."""
    params = dict(params or {})
    if route.strip("/") == "value":
        page = fetch_page(route, params, timeout=timeout, get=get)
        return page[0] if page else None
    params["limit"] = min(int(params.get("limit") or PAGE), max_rows)
    rows: list = []
    cursor = None
    while True:
        page = fetch_page(route, params, cursor, timeout=timeout, get=get)
        if page is None:
            return rows or None
        batch, cursor = page
        rows.extend(batch)
        if not cursor or len(rows) >= max_rows:
            return rows[:max_rows]


def wallet_positions(user: str, include_closed: bool = False,
                     get: Callable = _default_get) -> Optional[list]:
    """Открытые и ожидающие выплаты позиции кошелька (v1 /positions отдавал их
    одним списком; в v2 это разные status). include_closed — ещё и проданные /
    выплаченные (status=CLOSED, с realized_pnl): их v1 не отдавал вовсе.
    None, если API недоступен."""
    seen, out = set(), []
    for status in ("OPEN", "REDEEMABLE") + (("CLOSED",) if include_closed else ()):
        rows = fetch("positions", {"user": user, "status": status}, max_rows=5000, get=get)
        if rows is None:
            return None
        for r in rows:
            key = r.get("token_id") or (r.get("condition_id"), r.get("outcome_index"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out
