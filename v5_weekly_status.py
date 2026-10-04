"""v5_weekly_status — воскресный статус живой стратегии в Telegram.

Заменяет ежедневный легаси-STATS похороненной whale-copy (852 ставки, WR 44%,
ROI −4% — финальные цифры зафиксированы в README, повторять их каналу незачем).

Отвечает на три вопроса оператора без сетевых вызовов:
  1. Что открыто и сколько подтверждено ончейном (журнал v5).
  2. Как копится калибровочная выборка (всего оценок / короткий горизонт ≤45д
     — именно short-срез даст Brier-вердикт).
  3. Сколько дней до чекпойнтов календаря (docs/NEXT_STEPS.md).

Глубокий разбор остаётся за verify_journal.py (запускается руками раз в
неделю по инструкции) — этот статус лишь напоминает, что проект жив и копит.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

JOURNAL = Path("event_journal.jsonl")
CALIB = Path("calibration_journal.jsonl")
SHORT_HORIZON_DAYS = 45.0
SHORT_TARGET = 30          # резолвов нужно для ворот вердикта

CHECKPOINTS = [            # из docs/NEXT_STEPS.md
    ("24.06", datetime(2026, 6, 24, tzinfo=timezone.utc), "первый взгляд"),
    ("08.07", datetime(2026, 7, 8, tzinfo=timezone.utc), "контроль"),
    ("22.07", datetime(2026, 7, 22, tzinfo=timezone.utc), "ворота вердикта"),
    # 02.08 сужена YES-зона 50-70% → 50-65% (выше 62c EV отрицателен), счётчик
    # вердикта перезапущен: старые резолвы из широкой зоны несопоставимы.
    ("02.09", datetime(2026, 9, 2, tzinfo=timezone.utc), "промежуточный: узкая зона на реальных ставках"),
    # 04.10 аудит: гейт YES по сырому Grok, поиск, conf ≥ medium — счётчик YES
    # перезапущен. 15.11 — после резолва выборов США 03.11 (крупный кластер
    # YES-ставок), первый взгляд на новую выборку.
    ("15.11", datetime(2026, 11, 15, tzinfo=timezone.utc), "первый взгляд: YES после перезапуска 04.10"),
]


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


def calibration_buckets(resolved: list) -> list:
    """Калибровка Grok: для каждой корзины его оценки P(YES) — какова РЕАЛЬНАЯ
    частота YES. resolved: список (market_yes, ai_yes, actual_yes).

    Отвечает на гипотезу оператора: если в корзине '0.6-0.8' реальный YES
    заметно выше (напр. 90%), Grok систематически недооценивает фаворитов —
    значит механический NO против них структурно убыточен, ставить надо на YES.
    """
    if not resolved:
        return []
    out = ["📊 Калибровка Grok (его оценка YES → реальный YES):"]
    for lo, hi in [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]:
        bucket = [y for _, a, y in resolved if lo <= a < hi]
        if bucket:
            actual = sum(bucket) / len(bucket) * 100
            mid = (lo + min(hi, 1.0)) / 2 * 100
            flag = ""
            if actual - mid >= 20:
                flag = " ⚠️ недооценка фаворита"
            elif mid - actual >= 20:
                flag = " ⚠️ переоценка"
            out.append(f"  Grok {lo:.1f}-{hi:.1f}: реально YES {actual:.0f}% "
                       f"(n={len(bucket)}){flag}")
    return out


def build_status(journal: List[dict], calib: List[dict],
                 now: datetime) -> str:
    open_rows = [r for r in journal
                 if str(r.get("status", "open")).lower() == "open"]
    onchain_rows = [r for r in open_rows if r.get("fill_source") == "onchain"]
    onchain = len(onchain_rows)

    # экспозиция по категориям — ТОЛЬКО реальные ончейн-позиции (04.07: бумага
    # надувала цифру до $562 через midpoint-заглушку и читалась как деньги).
    exp_line = ""
    try:
        import category_exposure as cx
        exp = cx.exposure_by_category(onchain_rows)
        if exp:
            exp_line = cx.format_exposure(
                exp, counts=cx.exposure_counts(onchain_rows))
    except Exception:
        pass

    n_calib = len(calib)
    n_short = sum(1 for r in calib
                  if r.get("horizon_days") is not None
                  and float(r["horizon_days"]) <= SHORT_HORIZON_DAYS)

    cp_bits = []
    for label, dt, name in CHECKPOINTS:
        d = (dt - now).days
        if d >= 0:
            cp_bits.append(f"{label} {name} (через {d}д)")
    cp_line = " · ".join(cp_bits) if cp_bits else "все чекпойнты пройдены"

    lines = [
        "📋 Polymarket v5 · недельный статус",
        f"Открыто (ончейн): {onchain} позиций",
    ]
    if exp_line:
        lines.append(exp_line)
    lines += [
        "",
        f"Калибровка: {n_calib} оценок Grok · короткий горизонт ≤45д: {n_short}",
        f"Цель ворот вердикта: ≥{SHORT_TARGET} коротких РЕЗОЛВОВ "
        f"(точный счёт — verify_journal.py)",
        f"Календарь: {cp_line}",
    ]
    return "\n".join(lines)


def build_kpi_block(journal: List[dict], calib: List[dict],
                    resolve_fn=None) -> str:
    """KPI успешности. Контракт честности: каждая цифра — с размером выборки
    рядом; ниже порогов мощности — явное «рано судить», не проценты.

    resolve_fn(cid) -> True (NO выиграл) / False / None. None-резолвер =
    сеть недоступна -> KPI пропускается, статус деградирует в активность.
    """
    if resolve_fn is None:
        return ""

    # ── Scorecard по реальным ставкам журнала ──
    wins = losses = 0
    pnl = staked = 0.0
    oc_wins = oc_losses = 0
    oc_pnl = oc_staked = 0.0
    for r in journal:
        if str(r.get("status", "open")).lower() != "open":
            continue
        cid = r.get("condition_id", "")
        try:
            entry = float(r.get("entry_price_actual") or r.get("no_price") or 0)
        except (TypeError, ValueError):
            entry = 0.0
        if not cid or entry <= 0:
            continue
        won = resolve_fn(cid)
        if won is None:
            continue
        try:
            stake = float(r.get("stake_actual") or 0) or 40.0
        except (TypeError, ValueError):
            stake = 40.0
        onchain = r.get("fill_source") == "onchain"
        staked += stake
        delta = stake * (1.0 / entry - 1.0) if won else -stake
        pnl += delta
        if won:
            wins += 1
        else:
            losses += 1
        if onchain:
            oc_staked += stake
            oc_pnl += delta
            if won:
                oc_wins += 1
            else:
                oc_losses += 1

    lines = []
    decided = wins + losses
    if decided:
        wr = wins / decided * 100
        roi = pnl / staked * 100 if staked else 0.0
        # Это метрика КАЧЕСТВА ПРЕДСКАЗАНИЙ (бумага+ончейн) — валидационная
        # выборка стратегии, не деньги. Реальный денежный результат — отдельной
        # строкой ниже (только ончейн). Смешивать нельзя: 04.07 $562 «экспозиции»
        # уже приняли за реальный риск.
        line = (f"Точность сигналов (модельная выборка): резолвов {decided} · "
                f"WR {wr:.0f}% · ROI {roi:+.0f}%")
        oc_n = oc_wins + oc_losses
        if oc_n:
            oc_wr = oc_wins / oc_n * 100
            oc_roi = oc_pnl / oc_staked * 100 if oc_staked else 0.0
            line += (f"\n     💰 реальные деньги (ончейн, n={oc_n}): "
                     f"WR {oc_wr:.0f}%, ROI {oc_roi:+.0f}%")
        lines.append(line)
    else:
        lines.append("Точность сигналов: резолвов пока 0 — ставки зреют")

    # ── Brier-прогресс (главный вопрос проекта) ──
    short = [r for r in calib
             if r.get("horizon_days") is not None
             and float(r["horizon_days"]) <= SHORT_HORIZON_DAYS
             and r.get("market_yes_price") is not None
             and r.get("ai_yes_estimate") is not None]
    import calibration_map as _cm
    short = _cm.first_per_market(short)   # n = рынки, а не переоценки
    resolved = []
    for r in short:
        won = resolve_fn(r.get("condition_id", ""))
        if won is not None:
            # won=True значит NO выиграл -> исход YES = 0
            resolved.append((float(r["market_yes_price"]),
                             float(r["ai_yes_estimate"]),
                             0.0 if won else 1.0))
    n = len(resolved)
    if n < 10:
        lines.append(f"Brier (короткие): {n}/{SHORT_TARGET} резолвов — рано судить")
    else:
        b_mkt = sum((m - y) ** 2 for m, _, y in resolved) / n
        b_ai = sum((a - y) ** 2 for _, a, y in resolved) / n
        delta = b_mkt - b_ai      # >0 = Grok точнее рынка
        leader = "Grok точнее" if delta > 0 else "рынок точнее"
        tag = "ВЕРДИКТ" if n >= SHORT_TARGET else "предварительно"
        lines.append(f"Brier (короткие, n={n}/{SHORT_TARGET}, {tag}): "
                     f"{leader}, Δ={delta:+.3f} "
                     f"(рынок {b_mkt:.3f} vs Grok {b_ai:.3f})")
        # калибровка по корзинам — проверка гипотезы про недооценку фаворитов
        for cl in calibration_buckets(resolved):
            lines.append(cl)
        # Пересчитываем и СОХРАНЯЕМ таблицу калибровки — сканер применит её как
        # посткалибровку. Таблица копится: чем больше резолвов, тем крепче поправка.
        try:
            import calibration_map as cm
            table = cm.build_calibration_table(resolved)
            cm.save_table(table)
            lines.append(f"  (таблица калибровки обновлена: {len(table)} корзин, "
                         f"n={n})")
        except Exception as e:
            lines.append(f"  (таблица калибровки не обновлена: {e})")
    return "\n".join(lines)


def main() -> None:
    now = datetime.now(timezone.utc)
    journal = _load_jsonl(JOURNAL)
    calib = _load_jsonl(CALIB)
    msg = build_status(journal, calib, now)
    # KPI успешности — с сетевым резолвером; при сбое деградируем молча
    try:
        from verify_journal import _resolve_no_outcome
        _cache: dict = {}

        def _resolve(cid: str):
            if cid not in _cache:
                _cache[cid] = _resolve_no_outcome(cid)
            return _cache[cid]

        kpi = build_kpi_block(journal, calib, resolve_fn=_resolve)
        if kpi:
            msg += "\n\n" + kpi
        from verify_journal import _flush_resolution_cache
        _flush_resolution_cache()   # недельный прогон наполняет resolutions.json
    except Exception as e:  # noqa: BLE001
        print(f"KPI block skipped: {e}")
    print(msg)
    try:
        import requests
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "disable_notification": True},
                timeout=10).raise_for_status()
            print("sent to telegram")
    except Exception as e:  # noqa: BLE001
        print(f"telegram send failed: {e}")


if __name__ == "__main__":
    main()
