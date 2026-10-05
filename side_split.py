"""Раздельный вердикт YES vs NO по РЕАЛЬНЫМ позициям (event_journal).

Зачем отдельно от verify_journal: тот считает Brier по калибровочному журналу,
где только сырые оценки Grok и НЕТ сторон. Стороны (NO осн. / YES средняя зона)
живут в event_journal. Вопрос, на который отвечает этот отчёт:

  NO получил вердикт на n=278: рынок точнее, Δ=-0.073 → алерты выключены.
  А набирает ли YES средней зоны СВОЙ edge, или повторяет судьбу NO?

Исходы тянутся живьём (CLOB через verify_journal._resolve_no_outcome), поэтому
запуск — в Actions, где сеть доступна.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional, Tuple

JOURNAL = Path("event_journal.jsonl")
MIN_VERDICT_N = 30          # те же ворота, что были у NO


def split_by_side(rows: list) -> Tuple[list, list]:
    """Делит записи на YES- и NO-позиции. Отсутствие поля = NO (исторически)."""
    import calibration_map as cm
    yes, no = [], []
    import manual_positions
    _manual = manual_positions.load()
    # ручные позиции оператора — не сигналы системы, в статистику не входят
    rows = [r for r in rows or [] if not manual_positions.is_manual(r, _manual)]
    # re_alert — тот же рынок: одна позиция на condition_id; строки без id
    # (старые/ручные) не схлопываем — склеивать их не по чему
    rows = cm.first_per_market(rows) + [r for r in rows
                                        if not r.get("condition_id")]
    for r in rows:
        if str(r.get("side", "NO")).upper() == "YES":
            yes.append(r)
        else:
            no.append(r)
    return yes, no


# Счётчик YES-выборки перезапущен 04.10.2026 (гейт по сырому Grok, поиск,
# conf ≥ medium — политика §1). Резолвы до и после — разные выборки.
YES_SAMPLE_SINCE = "2026-10-04"


def since_restart(rows: list) -> list:
    """Строки, алертнутые не раньше перезапуска счётчика YES-выборки."""
    return [r for r in rows or []
            if str(r.get("alerted_at") or "")[:10] >= YES_SAMPLE_SINCE]


def side_stats(rows: list) -> dict:
    """WR и n по резолвнувшимся позициям одной стороны (won: True/False/None)."""
    resolved = [r for r in rows or [] if r.get("won") is not None]
    n = len(resolved)
    if n == 0:
        return {"n": 0, "wr": None, "wins": 0}
    wins = sum(1 for r in resolved if r.get("won"))
    return {"n": n, "wr": wins / n, "wins": wins}


def brier_for_side(rows: list) -> Optional[dict]:
    """Brier стороны против рынка на её резолвнувшихся позициях.

    Для каждой позиции: исход YES (1/0), оценка Grok (ai_yes_estimate) и цена
    рынка (market_yes_price). Считаем, кто ближе к правде.
    """
    pairs = []
    for r in rows or []:
        if r.get("won") is None:
            continue
        side = str(r.get("side", "NO")).upper()
        # исход в терминах YES: наша ставка выиграла -> YES случился, если мы
        # ставили YES; если ставили NO и выиграли -> YES НЕ случился
        won = bool(r.get("won"))
        actual_yes = 1.0 if (won == (side == "YES")) else 0.0
        ai = r.get("ai_yes_estimate")
        mkt = r.get("market_yes_price")
        if ai is None or mkt is None:
            continue
        pairs.append((float(mkt), float(ai), actual_yes))
    if not pairs:
        return None
    n = len(pairs)
    b_mkt = sum((m - a) ** 2 for m, _, a in pairs) / n
    b_ai = sum((g - a) ** 2 for _, g, a in pairs) / n
    return {"n": n, "brier_market": b_mkt, "brier_grok": b_ai,
            "delta": b_mkt - b_ai}


def _load_journal() -> list:
    try:
        return [json.loads(l) for l in JOURNAL.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def _fill_outcomes(rows: list) -> list:
    """Дотягивает исходы живьём через CLOB (только в Actions, где есть сеть)."""
    try:
        from verify_journal import _resolve_no_outcome
    except Exception as e:
        print(f"  резолвер недоступен: {e}")
        return rows
    filled = 0
    for r in rows:
        if r.get("won") is not None:
            continue
        cid = r.get("condition_id", "")
        if not cid:
            continue
        no_won = _resolve_no_outcome(cid)
        if no_won is None:
            continue
        side = str(r.get("side", "NO")).upper()
        r["won"] = (not no_won) if side == "YES" else no_won
        filled += 1
    print(f"  дотянуто исходов: {filled}")
    return rows


def report() -> None:
    rows = _load_journal()
    print(f"Раздельный вердикт по сторонам · записей в журнале: {len(rows)}")
    rows = _fill_outcomes(rows)
    yes, no = split_by_side(rows)
    print(f"  YES-позиций: {len(yes)} · NO-позиций: {len(no)}\n")

    for name, rs in ((f"YES с {YES_SAMPLE_SINCE} (текущая выборка — по ней вердикт)",
                      since_restart(yes)),
                     ("YES вся история (смешанные выборки, НЕ для вердикта)", yes),
                     ("NO (осн., алерты выключены)", no)):
        st = side_stats(rs)
        print(f"=== {name} ===")
        if st["n"] == 0:
            print("  резолвов нет — судить рано\n")
            continue
        print(f"  резолвов: {st['n']} · выиграло {st['wins']} · WR {st['wr']*100:.0f}%")
        b = brier_for_side(rs)
        if b:
            leader = "Grok точнее" if b["delta"] > 0 else "рынок точнее"
            gate = ("ВЕРДИКТ" if b["n"] >= MIN_VERDICT_N
                    else f"рано ({b['n']}/{MIN_VERDICT_N})")
            print(f"  Brier [{gate}]: {leader}, Δ={b['delta']:+.3f} "
                  f"(рынок {b['brier_market']:.3f} vs Grok {b['brier_grok']:.3f})")
        print()

    print("Читать так: Δ>+0.01 на n≥30 — у стороны есть измеренный edge.")
    print("NO уже получил вердикт на калибровочной выборке (Δ=-0.073) — выключен.")


if __name__ == "__main__":
    report()
