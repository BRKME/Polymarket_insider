"""Синхронизация журнала с реальным портфелем — обнаружение ПРОДАЖ.

Дыра, найденная 09.08.2026: журнал помечает позицию закрытой только когда рынок
РЕЗОЛВИТСЯ. Когда оператор выходит сам, система не знает — строка висит 'open',
экспозиция завышается, mark_to_market шлёт сигналы по проданному. 08.08 шесть
таких позиций пришлось закрывать вручную; 09.08 журнал числил $109.61 вложенных
при реальном портфеле $84.89.

Источник истины — /positions кошелька (тот же, что у daily_status). Позиции нет
среди реально открытых → она продана.

ЗАЩИТА ОТ ЛОЖНОГО ЗАКРЫТИЯ: если API вернул пустой список, НИЧЕГО не закрываем.
Пустой ответ почти всегда означает сбой сети/лимит, а не то, что оператор
распродал весь портфель. Ошибка здесь дорога: закрытая по ошибке позиция
исчезает из мониторинга и из статистики.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple

JOURNAL = Path("event_journal.jsonl")


def _is_tracked_position(row: dict) -> bool:
    """Строка журнала = реальная позиция: открыта и с подтверждённым входом."""
    if str(row.get("status", "open")).lower() != "open":
        return False
    try:
        return float(row.get("stake_actual") or 0) > 0
    except (TypeError, ValueError):
        return False


def detect_sold(rows: List[dict], wallet_positions: List[dict]) -> List[dict]:
    """Строки журнала, которых больше нет в реальном портфеле → проданы."""
    if not wallet_positions:
        return []                      # пустой ответ API — не повод закрывать
    try:
        from daily_status import filter_open_positions
        live = filter_open_positions(wallet_positions)
    except Exception:
        live = wallet_positions
    live_cids = {str(p.get("conditionId", "")) for p in live}
    # v2 status=CLOSED — прямое свидетельство продажи. По нему закрываем даже
    # при пустом live: оператор закрыл всё (04.11.2026), и защита «пустой
    # портфель — не повод» держала 18 проданных позиций открытыми в журнале.
    closed_cids = {str(p.get("conditionId", "")) for p in wallet_positions
                   if str(p.get("status") or "").upper() == "CLOSED"} - live_cids
    if not live:
        return [r for r in rows if _is_tracked_position(r)
                and str(r.get("condition_id", "")) in closed_cids]
    return [r for r in rows
            if _is_tracked_position(r)
            and str(r.get("condition_id", "")) not in live_cids]


def apply_sold(rows: List[dict],
               wallet_positions: List[dict]) -> Tuple[List[dict], int]:
    """Помечает проданные закрытыми. Возвращает (строки, сколько закрыто)."""
    sold = detect_sold(rows, wallet_positions)
    sold_ids = {id(r) for r in sold}
    n = 0
    for r in rows:
        if id(r) in sold_ids:
            r["status"] = "closed"
            r["closed_reason"] = "sold_detected"   # вышел сам, НЕ резолв рынка
            r["closed_at"] = datetime.now(timezone.utc).isoformat()
            n += 1
    return rows, n


def _load() -> List[dict]:
    try:
        return [json.loads(l) for l in JOURNAL.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def _save(rows: List[dict]) -> None:
    with JOURNAL.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run() -> None:
    rows = _load()
    if not rows:
        print("журнал пуст")
        return
    try:
        from daily_status import _fetch_positions
        positions = _fetch_positions() or []
    except Exception as e:
        print(f"портфель не прочитан: {e}")
        return
    if not positions:
        print("портфель пуст/недоступен — ничего не закрываем (защита)")
        return
    tracked = [r for r in rows if _is_tracked_position(r)]
    rows, n = apply_sold(rows, positions)
    print(f"позиций в журнале: {len(tracked)} · в кошельке: {len(positions)}")
    if n:
        _save(rows)
        print(f"помечено проданными: {n}")
    else:
        print("расхождений нет")


if __name__ == "__main__":
    run()
