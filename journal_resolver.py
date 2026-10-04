# -*- coding: utf-8 -*-
"""journal_resolver.py — персистентная резолюция event_journal.

Проблема (15.07): 55 строк журнала вечно 'open' (включая матчи 4 июля) —
резолюция нигде не сохранялась. Еженедельный отчёт перевыкачивал ВСЕ строки
заново (O(rows) запросов, растёт вечно), калибровка Grok-оценок пополнялась
только раз в неделю.

Что делает:
  1. Строки со статусом open/re_alert и end_date, прошедшим ≥ GRACE_HOURS
     назад, прогоняются через gamma-каскад resolution_tracker
     (condition_id → CLOB). Исход пишется в строку: status='resolved',
     actual_yes 0/1, блок resolution.
  2. Каждый исход кэшируется в resolutions.json — один сетевой запрос на
     condition_id навсегда. Нерезолвленные (None) НЕ кэшируются — ретрай
     на следующем прогоне.
  3. Калибровочная таблица пересобирается из calibration_journal по кэшу
     (никаких новых запросов) и сохраняется через calibration_map.

Запускается ежедневно перед daily_status (см. daily_status.yml).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

JOURNAL = Path("event_journal.jsonl")
CALIB = Path("calibration_journal.jsonl")
CACHE_PATH = Path("resolutions.json")
GRACE_HOURS = 2          # рынку нужно время зарезолвиться после end_date
RESOLVABLE = ("open", "re_alert")


# ── кэш резолюций ────────────────────────────────────────────────────────────

class ResolutionCache:
    """Дисковый кэш {condition_id: {'outcome': 'Yes'/'No', 'resolved_at': ts}}.

    Резолюция необратима — закэшированный исход валиден навсегда.
    None (не зарезолвлено/не найдено) не кэшируем: ретраим позже.
    """

    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self._data: Dict[str, dict] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    self._data = loaded
            except Exception:
                pass

    def put(self, cid: str, outcome: str) -> None:
        self._data[cid] = {
            "outcome": outcome,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_outcome(self, cid: str,
                    fetch: Callable[[str], Optional[dict]]) -> Optional[str]:
        """'Yes'/'No' из кэша или через fetch (с записью в кэш)."""
        rec = self._data.get(cid)
        if rec and rec.get("outcome") in ("Yes", "No"):
            return rec["outcome"]
        market = fetch(cid)
        if not market:
            return None
        outcome = _binary_outcome(market)
        if outcome:
            self.put(cid, outcome)
        return outcome

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, ensure_ascii=False,
                                        indent=0))


def _binary_outcome(market: dict) -> Optional[str]:
    """'Yes'/'No' из payload рынка через determine_resolution трекера.

    Небинарные исходы (имя команды и т.п.) отбрасываем: журнал событий —
    только бинарные Yes/No рынки, чужое не пишем в actual_yes.
    """
    import resolution_tracker as rt
    res = rt.determine_resolution(market)
    if res is None:
        return None
    r = str(res).strip().lower()
    if r in ("yes", "true", "1"):
        return "Yes"
    if r in ("no", "false", "0"):
        return "No"
    return None


def _default_fetch(cid: str) -> Optional[dict]:
    """Gamma-каскад трекера: /markets по conditionId, затем CLOB."""
    import resolution_tracker as rt
    market = rt.fetch_market_by_condition_id(cid)
    if not market:
        market = rt.fetch_market_by_clob(cid)
    return market


# ── резолюция журнала ────────────────────────────────────────────────────────

def ready_to_resolve(row: dict, now: datetime) -> bool:
    """Строка готова к резолюции: статус резолвибельный, end_date + грейс
    в прошлом. Грейс нужен, потому что сразу после end_date рынок ещё может
    не зарезолвиться (оракул/UMA), а мусорный None — лишний запрос завтра."""
    if str(row.get("status", "")).lower() not in RESOLVABLE:
        return False
    end_raw = row.get("end_date")
    if not end_raw:
        return False
    try:
        end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    return end + timedelta(hours=GRACE_HOURS) <= now


def resolve_journal(rows: List[dict], now: datetime, cache: ResolutionCache,
                    fetch: Callable[[str], Optional[dict]] = _default_fetch,
                    ) -> Tuple[List[dict], Dict[str, int]]:
    """Резолвит готовые строки на месте. Возвращает (rows, статистика)."""
    stats = {"resolved": 0, "yes": 0, "no": 0, "pending": 0}
    for row in rows:
        if not ready_to_resolve(row, now):
            continue
        cid = row.get("condition_id") or ""
        if not cid:
            continue
        outcome = cache.get_outcome(cid, fetch)
        if outcome is None:
            stats["pending"] += 1
            continue
        row["status"] = "resolved"
        row["actual_yes"] = 1.0 if outcome == "Yes" else 0.0
        row["resolution"] = {"outcome": outcome,
                             "resolved_at": now.isoformat(),
                             "source": "journal_resolver"}
        stats["resolved"] += 1
        stats["yes" if outcome == "Yes" else "no"] += 1
    return rows, stats


# ── пополнение калибровки ────────────────────────────────────────────────────

def calibration_pairs(calib_rows: List[dict],
                      cache: ResolutionCache) -> List[Tuple[float, float, float]]:
    """(market_yes, ai_yes, actual_yes) для строк с оценкой и резолвом в кэше.

    Только кэш, без сетевых запросов: полный живой прогон калибровочного
    журнала остаётся за еженедельным отчётом, здесь — бесплатный инкремент.
    """
    import calibration_map as cm
    pairs = []
    usable = [r for r in calib_rows
              if r.get("ai_yes_estimate") is not None
              and r.get("market_yes_price") is not None]
    for r in cm.first_per_market(usable):     # одна точка на рынок
        cid = r.get("condition_id") or ""
        ai = r.get("ai_yes_estimate")
        mkt = r.get("market_yes_price")
        rec = cache._data.get(cid)
        if not rec:
            continue
        actual = 1.0 if rec.get("outcome") == "Yes" else 0.0
        pairs.append((float(mkt), float(ai), actual))
    return pairs


def _table_n(table: dict) -> int:
    try:
        return sum(int(rec.get("n", 0)) for rec in table.values())
    except Exception:
        return 0


def should_save_table(new: dict, old: dict) -> bool:
    """Дневной пересбор из кэша не имеет права УСАДИТЬ боевую таблицу:
    еженедельный отчёт строит её живым перевыкачиванием и знает больше
    резолвов, чем свежий кэш. Сохраняем только рост выборки (тот же класс
    инцидента, что уже был с тестовым артефактом — см. save_table)."""
    return _table_n(new) >= _table_n(old)


# ── точка входа ──────────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _save_jsonl(path: Path, rows: List[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


def run() -> None:
    now = datetime.now(timezone.utc)
    rows = _load_jsonl(JOURNAL)
    if not rows:
        print("[resolver] журнал пуст — нечего резолвить")
        return
    cache = ResolutionCache()
    rows, stats = resolve_journal(rows, now, cache)
    cache.save()
    if stats["resolved"]:
        _save_jsonl(JOURNAL, rows)
    print(f"[resolver] resolved={stats['resolved']} "
          f"(yes={stats['yes']} no={stats['no']}) pending={stats['pending']}")

    # Инкремент калибровки из кэша (без сети).
    try:
        import calibration_map as cm
        pairs = calibration_pairs(_load_jsonl(CALIB), cache)
        if pairs:
            table = cm.build_calibration_table(pairs)
            old = cm.load_table()
            if should_save_table(table, old):
                cm.save_table(table)
                n = _table_n(table)
                print(f"[resolver] калибровка пересобрана: n={n}, "
                      f"корзин={len(table)}")
            else:
                print(f"[resolver] калибровка НЕ сохранена: новая выборка "
                      f"n={_table_n(table)} меньше текущей n={_table_n(old)} "
                      f"(кэш моложе еженедельного прогона)")
    except Exception as e:  # noqa: BLE001 — калибровка не должна ронять резолвер
        print(f"[resolver] calibration update failed: {e}")

    if stats["resolved"]:
        _notify(f"🏁 Журнал: резолвнуто {stats['resolved']} "
                f"(YES {stats['yes']} / NO {stats['no']}), "
                f"ждут оракула: {stats['pending']}")


def _notify(msg: str) -> None:
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        import requests
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=10).raise_for_status()
        else:
            print("  (no telegram creds) " + msg)
    except Exception as e:  # noqa: BLE001
        print(f"  notify failed: {e}")


if __name__ == "__main__":
    run()
