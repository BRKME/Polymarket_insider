"""Копи-монитор — финальное звено спортивного копи-трейда.

Для каждого кита из ЖИВОГО списка (trusted_whales.json) смотрит свежие сделки
через /activity. Копирует только: BUY (не SELL), спорт (детектор из whale_scout),
свежий вход (цена ≤3¢ от китовой — иначе edge уже съеден), новое (дедуп по
хешу/условию, чтобы не слать один вход дважды).

Не автоторговля — шлёт АЛЕРТ «кит X (эфф 49%) вошёл в X по цене Y, ещё свежо».
Решение о ставке — за оператором.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from whale_scoring import entry_still_fresh, COPY_MAX_SLIPPAGE

# детектор спорта переиспользуем из whale_scout
try:
    from whale_scout import _title_is_sport
except Exception:
    def _title_is_sport(title: str) -> bool:
        return False

SEEN_FILE = Path("copy_seen.json")

# Сделка старше этого окна не копируется: в спорте цена уезжает за часы, а
# /activity отдаёт историю — без окна первый прогон (или потерянный seen-файл)
# зальёт алертами старые входы. Дедуп это не ловит, окно — ловит.
MAX_ACT_AGE_H = 12


def act_is_recent(act: dict, now_ts: float) -> bool:
    """True, если сделке не больше MAX_ACT_AGE_H часов. Нет timestamp —
    fail-closed (возраст неизвестен = считаем старой)."""
    ts = act.get("timestamp")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return False
    return (now_ts - ts) <= MAX_ACT_AGE_H * 3600


def is_fresh_sport_buy(act: dict, current_price: float) -> bool:
    """True, если сделка кита — свежая спортивная покупка YES, годная к копированию.

    Outcome обязателен и должен быть YES: /activity содержит покупки и YES-, и
    NO-токенов, а алерт и вся стратегия — YES-центричны. Кит, купивший NO, — это
    сигнал в ПРОТИВОПОЛОЖНУЮ сторону. Нет поля outcome — fail-closed: для
    ставочного сигнала неизвестное направление хуже пропуска."""
    if not act:
        return False
    if str(act.get("outcome", "")).strip().upper() != "YES":
        return False                       # NO-токен или направление неизвестно
    if str(act.get("side", "")).upper() != "BUY":
        return False                       # копируем только входы, не выходы
    if str(act.get("type", "TRADE")).upper() not in ("TRADE", "BUY", ""):
        return False
    if not _title_is_sport(act.get("title", "")):
        return False                       # только спорт
    entry = act.get("price")
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return False
    return entry_still_fresh(entry, current_price)


def copy_signal(whale_name: str, whale_eff: float, act: dict,
                current_price: float) -> Optional[str]:
    """Строит копи-алерт, если вход годен; иначе None."""
    if not is_fresh_sport_buy(act, current_price):
        return None
    title = act.get("title", "")[:70]
    entry = float(act.get("price"))
    drift = (current_price - entry) * 100
    lines = [
        f"🐋 Копи-сигнал (спорт)",
        f"{title}",
        "",
        f"Кит {whale_name} (ROI {whale_eff:.0%}/мес) купил YES по {entry*100:.0f}¢",
        f"Сейчас {current_price*100:.0f}¢ (сдвиг {drift:+.0f}¢, ещё в пределах "
        f"{COPY_MAX_SLIPPAGE*100:.0f}¢)",
        "🧪 Копи-трейд по лидерборду — новая стратегия, решай сам",
    ]
    slug = act.get("eventSlug") or act.get("slug") or ""
    if slug:
        lines.append(f"🔗 https://polymarket.com/event/{slug}")
    return "\n".join(lines)


def _load_seen() -> set:
    try:
        if SEEN_FILE.exists():
            return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        pass
    return set()


def _save_seen(seen: set) -> None:
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False))
    except Exception:
        pass


def _act_key(whale: str, act: dict) -> str:
    return f"{whale}:{act.get('conditionId','')}:{act.get('side','')}"


def _live_yes_price(condition_id: str):
    """Живая цена YES по conditionId (Gamma). None при любом сбое — тогда
    сигнал НЕ строится: сфабрикованная цена хуже пропущенного алерта."""
    try:
        import resolution_tracker as rt
        import event_scanner as es
        m = rt.fetch_market_by_condition_id(condition_id)
        if not m:
            return None
        parsed = es._parse_prices(m)
        return parsed[0] if parsed else None
    except Exception:
        return None


def process_whale_acts(whale_name: str, whale_eff: float, acts: list,
                       price_fn, send_fn, seen: set, new_seen: set,
                       now_ts: float) -> int:
    """Обрабатывает активность одного кита, шлёт годные копи-алерты.

    Гейты по порядку дешевизны: дедуп -> направление/сторона/спорт (без сети)
    -> возраст сделки -> ЖИВАЯ цена (сеть, обязательна). Возвращает число
    отправленных алертов. price_fn/send_fn/now_ts инжектируются для тестов.
    """
    alerts = 0
    for act in acts:
        key = _act_key(whale_name, act)
        if key in seen:
            continue
        # дешёвые гейты без сети: направление YES, BUY, спорт (цену пока не
        # проверяем — передаём вход как current, freshness решит live-цена ниже)
        entry = act.get("price")
        try:
            entry = float(entry)
        except (TypeError, ValueError):
            new_seen.add(key)
            continue
        if not is_fresh_sport_buy(act, current_price=entry):
            new_seen.add(key)
            continue
        if not act_is_recent(act, now_ts):
            new_seen.add(key)
            continue
        # живая цена обязательна: без неё гейт свежести (≤3¢) непроверяем,
        # а алерт с cur=entry печатал бы выдуманный «сдвиг +0¢»
        cur = price_fn(act.get("conditionId", ""))
        if cur is None:
            # сеть могла мигнуть — НЕ помечаем виденным, дадим шанс
            # следующему прогону (через 3 ч сделка ещё в окне 12 ч)
            continue
        sig = copy_signal(whale_name, whale_eff, act, float(cur))
        if sig:
            try:
                send_fn(sig)
                alerts += 1
            except Exception as e:
                print(f"  отправка не удалась: {e}")
        new_seen.add(key)
    return alerts


def run() -> None:
    """Проходит по живому списку китов, ищет свежие спортивные входы, шлёт
    копи-алерты (дедуп по виденному). Запуск в Actions."""
    import time as _time
    import trusted_whales as tw
    try:
        import collector
        from scan_events import _send
    except Exception as e:
        print(f"импорт не удался: {e}")
        return

    whales = tw.load_trusted()
    if not whales:
        print("живой список китов пуст — сначала прогони leaderboard scout")
        return
    print(f"копи-монитор: {len(whales)} китов в списке")

    seen = _load_seen()
    new_seen = set(seen)
    alerts = 0
    now_ts = _time.time()

    for w in whales:
        wallet = w.get("wallet")
        name = w.get("name", wallet[:8] if wallet else "?")
        eff = float(w.get("eff", 0))
        if not wallet:
            continue
        try:
            data = collector.get_wallet_activity(wallet, recent=True)
        except Exception as e:
            print(f"  {name}: activity error {e}")
            continue
        alerts += process_whale_acts(
            name, eff, data.get("activities", []),
            price_fn=_live_yes_price, send_fn=_send,
            seen=seen, new_seen=new_seen, now_ts=now_ts)

    _save_seen(new_seen)
    print(f"копи-монитор: отправлено алертов {alerts}, виденных сделок {len(new_seen)}")


if __name__ == "__main__":
    run()
