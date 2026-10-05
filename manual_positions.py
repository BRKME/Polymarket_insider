"""Ручные позиции оператора — реальные ставки, которые НЕ являются сигналами
системы (решение оператора 05.10.2026).

Первые — позиции по перевёрнутым YES-алертам (сырой Grok ≤ 0.5), которые
оператор решил оставить как ставку по честной рыночной цене. Для них:
- нет напоминания «по ошибочному алерту» (grok_flip) — решение принято;
- обычные ценовые сигналы выхода остаются — это деньги;
- в статистику стратегии (side_split) не входят.

Реестр — отдельный файл, а не поле в event_journal.jsonl: журнал пишут
боевые воркфлоу, ручная правка в нём конфликтует с их коммитами.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

REGISTRY = Path(__file__).resolve().parent / "manual_positions.json"


def load(path: Path = REGISTRY) -> Dict[str, dict]:
    """condition_id -> {question, reason, since}. Пусто, если файла нет."""
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def is_manual(row: dict, registry: Dict[str, dict]) -> bool:
    return bool(row.get("condition_id")) and row["condition_id"] in (registry or {})
