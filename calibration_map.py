"""Посткалибровка оценок Grok по фактической таблице корзин.

Вердикт (n=38): Grok систематически промахивается — в корзине 0.0-0.2 реально
YES 100%, в 0.2-0.4 реально 77%. Это измеренная функция ошибки. Мы её применяем
как поправку: сырую оценку Grok пересчитываем в откалиброванную по тому, что
РЕАЛЬНО случалось в этой корзине.

Критично: таблица копится на резолвах и уточняется. Где n мал (корзина из 3-5
точек) — доверять нельзя, поправка УСАЖИВАЕТСЯ к сырой оценке (shrinkage), чтобы
не выучить шум вместо смещения. По мере накопления резолвов поправка крепнет.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

CALIB_TABLE = Path("calibration_table.json")

# Корзины оценки Grok
_BUCKETS = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]

# Сколько резолвов в корзине нужно, чтобы доверять ей ПОЛНОСТЬЮ. Меньше —
# поправка усаживается к сырой оценке пропорционально n/FULL_TRUST_N.
FULL_TRUST_N = 25


def _bucket_key(p: float) -> str:
    for lo, hi in _BUCKETS:
        if lo <= p < hi:
            return f"{lo:.1f}-{min(hi,1.0):.1f}"
    return "0.8-1.0"


def first_per_market(rows: list) -> list:
    """Одна строка на рынок (condition_id) — самая ранняя по времени.

    Калибровочный журнал пишет строку на каждый вызов Grok, а рынок
    переоценивается при каждом сбросе кэша; журнал ставок несёт re_alert-
    повторы. Без схлопывания один рынок давал до 42 точек из 139 в корзине —
    раздутый знаменатель. Берём самую раннюю: она не зависит от того, сколько
    раз рынок попал в скан до резолва. Строки без condition_id отбрасываются.
    """
    first: dict = {}
    for r in rows or []:
        cid = r.get("condition_id") or ""
        if not cid:
            continue
        ts = str(r.get("estimated_at") or r.get("alerted_at") or "")
        cur = first.get(cid)
        if cur is None or ts < cur[0]:
            first[cid] = (ts, r)
    return [r for _, r in first.values()]


def build_calibration_table(resolved: list) -> dict:
    """Из списка (market_yes, ai_yes, actual_yes) строит таблицу корзин:
    {bucket: {actual: средняя реальная частота YES, n: число точек}}."""
    by_bucket: dict[str, list] = {}
    for _, ai, actual in resolved:
        k = _bucket_key(float(ai))
        by_bucket.setdefault(k, []).append(float(actual))
    table = {}
    for k, ys in by_bucket.items():
        table[k] = {"actual": sum(ys) / len(ys), "n": len(ys)}
    return table


def calibrate(raw_prob: float, table: Optional[dict]) -> float:
    """Пересчитывает сырую оценку Grok в откалиброванную по таблице корзин.

    С усадкой по n: если в корзине мало резолвов, доверяем её слабо и тянемся
    к сырой оценке. weight = min(1, n / FULL_TRUST_N).
    calibrated = raw + weight * (bucket_actual - raw).
    """
    if not table:
        return raw_prob
    rec = table.get(_bucket_key(float(raw_prob)))
    if not rec or rec.get("n", 0) <= 0:
        return raw_prob
    actual = float(rec["actual"])
    n = int(rec["n"])
    weight = min(1.0, n / FULL_TRUST_N)
    calibrated = raw_prob + weight * (actual - raw_prob)
    return max(0.0, min(1.0, calibrated))


def _in_pytest() -> bool:
    """True только во время прогона тестов (pytest), но НЕ в боевых воркфлоу.
    ВАЖНО: нельзя использовать os.getenv('CI') — GitHub Actions ставит CI=true
    во ВСЕХ воркфлоу, включая боевой скан, что отключило бы калибровку в проде."""
    import sys, os
    return "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST") is not None


def load_table() -> dict:
    """Читает сохранённую таблицу калибровки (мягкий fail-safe).

    В тестах (pytest) файл НЕ читаем — иначе внешний артефакт на диске делает
    тесты evaluate/scan недетерминированными. Тесты калибровки передают таблицу
    явно. В БОЮ (в т.ч. в Actions) читаем нормально."""
    if _in_pytest():
        return {}
    try:
        if CALIB_TABLE.exists():
            return json.loads(CALIB_TABLE.read_text())
    except Exception:
        pass
    return {}


def save_table(table: dict) -> None:
    """Сохраняет таблицу калибровки.

    В тестах (pytest) НЕ пишем: тест v5_weekly_status с фейковыми резолвами
    однажды перезаписал БОЕВУЮ таблицу (5 корзин, n=38) мусорным артефактом —
    та же дыра, что была в load_table, только на записи. Защита симметричная."""
    if _in_pytest():
        return
    try:
        CALIB_TABLE.write_text(json.dumps(table, ensure_ascii=False, indent=0))
    except Exception:
        pass
