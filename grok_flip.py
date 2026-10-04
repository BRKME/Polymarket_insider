"""Позиции, открытые по «перевёрнутым» YES-алертам.

До 04.10.2026 YES-гейт судил о согласии Grok по откалиброванной оценке, а
калибровка подменяла число средним корзины: Grok «против» (Maine 4%, Lamine
Yamal 12%, Variational 32%) выдавался за «согласие по YES». Гейт исправлен, но
реальные ставки по таким алертам остались. Оператор решил их закрыть — этот
модуль их находит, mark_to_market шлёт по ним 🔴 ЗАКРОЙ.

Сырая оценка берётся из calibration_journal.jsonl: последняя запись по рынку
не позже момента алерта (кэш-хиты не журналируются, поэтому это именно та
оценка, по которой алерт ушёл).
"""
from __future__ import annotations
from typing import Dict, List, Optional

GROK_YES_THRESHOLD = 0.50   # как в yes_strategy: согласие — строго выше 0.5


def index_calibration(calib_rows: List[dict]) -> Dict[str, List[tuple]]:
    """condition_id -> [(estimated_at, raw_prob), ...] по возрастанию времени."""
    idx: Dict[str, List[tuple]] = {}
    for r in calib_rows or []:
        cid = r.get("condition_id") or ""
        p = r.get("ai_yes_estimate")
        ts = str(r.get("estimated_at") or "")
        if not cid or p is None or not ts:
            continue
        try:
            idx.setdefault(cid, []).append((ts, float(p)))
        except (TypeError, ValueError):
            continue
    for v in idx.values():
        v.sort()
    return idx


def raw_at_alert(row: dict, idx: Dict[str, List[tuple]]) -> Optional[float]:
    """Сырая оценка Grok, по которой ушёл алерт (последняя не позже алерта)."""
    alerted = str(row.get("alerted_at") or "")
    best = None
    for ts, p in idx.get(row.get("condition_id") or "", []):
        if alerted and ts > alerted:
            break
        best = p
    return best


def is_flipped(row: dict, idx: Dict[str, List[tuple]]) -> bool:
    """YES-позиция, где сам Grok НЕ был за YES (сырая оценка ≤ 0.5)."""
    if str(row.get("side", "NO")).upper() != "YES":
        return False
    raw = raw_at_alert(row, idx)
    return raw is not None and raw <= GROK_YES_THRESHOLD
