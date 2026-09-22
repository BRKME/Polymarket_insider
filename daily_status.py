"""daily_status.py — дневной информационный статус Polymarket (раз в день).

Позиции НЕ бинарны: токены NO/YES торгуются непрерывно, их можно продать
(зафиксировать прибыль/убыток) или докупить, не дожидаясь резолва. Статус
показывает живую P&L-картину с МЯГКИМИ подсказками действий (наблюдения к
решению оператора, не приказы).

Формат: шапка (открытых позиций · суммарный нереал. P&L · в плюсе/минусе) +
детали ТОЛЬКО по двигавшимся заметно (≥ MOVE_THRESHOLD_PCT) или близким к
резолву. Переиспользует position_pnl/decide_exit из mark_to_market.
"""
from __future__ import annotations

from typing import Callable, List, Optional

import mark_to_market as mtm
from config import EXIT_PARTIAL_PRICE, EXIT_STOP_EDGE

MOVE_THRESHOLD_PCT = 15.0    # цена сдвинулась на столько — позиция «двигалась»
NEAR_RESOLUTION_DAYS = 7     # ближе этого к резолву — флаг
ADD_DRAWDOWN_PCT = -15.0     # просадка глубже — кандидат на докуп (если тезис цел)
THESIS_INTACT_AI_YES = 0.40  # AI всё ещё считает YES маловероятным -> тезис цел
TAKE_PROFIT_RET_PCT = 40.0   # рост NO на столько -> подсказка зафиксировать
                             # (информационно, мягче порога авто-выхода 0.80¢)
GARBAGE_DROP_PCT = -85.0     # падение глубже -> подозрение на мусор API (пустой
                             # ордербук отдаёт ~1¢), не реальная котировка


def smart_truncate(text: str, max_len: int) -> str:
    """Обрезка по границе слова с многоточием — не рвёт слова посередине."""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rstrip()
    # откатываемся до последнего пробела, чтобы не резать слово
    if " " in cut:
        cut = cut[:cut.rfind(" ")].rstrip()
    return cut + "…"


def is_plausible_price(entry: float, current: float) -> bool:
    """Отбраковка мусорных цен Data API.

    Пустой/неликвидный ордербук часто отдаёт ~1¢, что парсер принимает за
    реальную котировку и рисует −98%. Если цена обвалилась глубже
    GARBAGE_DROP_PCT от входа — это подозрение на мусор, не котировка.
    Резолв (легитимный 0/1) обрабатывается отдельно по статусу, не здесь.
    """
    if entry <= 0 or current <= 0:
        return False
    ret_pct = (current / entry - 1.0) * 100.0
    return ret_pct > GARBAGE_DROP_PCT


def filter_open_positions(positions: List[dict]) -> List[dict]:
    """Только РЕАЛЬНО открытые позиции. /positions отдаёт всю историю кошелька,
    включая зарезолвленные (currentValue≈0 → фантомные −100%) и пыль. Открытая
    позиция: ненулевой размер, не redeemable (не завершённая), есть стоимость.
    """
    out = []
    for p in positions:
        try:
            size = float(p.get("size") or 0)
            cur_val = float(p.get("currentValue") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue                      # пыль/закрытая
        if p.get("redeemable") is True:
            continue                      # зарезолвлена — к выплате, не открыта
        if cur_val <= 0:
            continue                      # нулевая стоимость = фактически мёртвая
        out.append(p)
    return out


def resolved_positions(positions: List[dict], now=None) -> List[dict]:
    """Завершившиеся позиции: реализованный результат, а не открытый риск.

    Выигрыш: redeemable=True (токены к выплате). Проигрыш: size>0,
    currentValue≈0 И endDate в прошлом — прошедший endDate отличает
    реальный резолв в ноль от мусорной котировки неликвидного ордербука
    (кейс 14.07: −$45 по трём футбольным ставкам молча выпали из учёта).
    """
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    out = []
    for p in positions:
        try:
            size = float(p.get("size") or 0)
            cur_val = float(p.get("currentValue") or 0)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        if p.get("redeemable") is True:
            out.append(p)                 # выигрыш к выплате
            continue
        if cur_val > 0:
            continue                      # открыта — не сюда
        end_raw = p.get("endDate")
        if not end_raw:
            continue                      # без даты нулевая цена = мусор API
        try:
            end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if end.tzinfo is None:            # см. коммент в realized_block: смесь
            end = end.replace(tzinfo=timezone.utc)   # naive/aware роняет сравнение
        if end <= now:
            out.append(p)                 # рынок завершён, токены обнулились
    return out


def realized_block(resolved: List[dict], now=None,
                   window_days: int = 7) -> Optional[str]:
    """Блок «Реализовано»: итог завершившихся за окно + строки по позициям.

    None, если за окно ничего не завершилось (блок не раздувает статус).
    """
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    recent = []
    for p in resolved:
        try:
            end = datetime.fromisoformat(
                str(p.get("endDate")).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # Polymarket отдаёт endDate и с 'Z' (aware), и без (naive). Смесь роняла
        # sorted()/сравнение с cutoff: TypeError offset-naive vs offset-aware
        # (полевой баг 03-05.08.2026, статус падал 3 дня). Приводим всё к UTC.
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end >= cutoff:
            recent.append((end, p))
    if not recent:
        return None
    total = 0.0
    lines = []
    for end, p in sorted(recent, key=lambda x: x[0], reverse=True):
        invested = float(p.get("initialValue") or 0)
        pnl = float(p.get("cashPnl") if p.get("cashPnl") is not None
                    else -invested)
        total += pnl
        marker = "🟢" if pnl >= 0 else "🔴"
        q = smart_truncate(p.get("title") or "", 44)
        lines.append(f"{marker} {q}  ${pnl:+.0f}")
    head = (f"\n🏁 Реализовано за {window_days}д: ${total:+.0f} "
            f"({len(recent)} позиц.)")
    return "\n".join([head] + lines)


def pending_payout_block(positions: List[dict]) -> Optional[str]:
    """Выигранные рынки, выплата по которым ещё не забрана (redeemable=True).

    Polymarket считает их в Portfolio — они стоят ~$1 за долю. Бот их отбрасывал
    как «не открытые», из-за чего его сумма была меньше реальной, а картина
    асимметричной: проигрыши показывались в «Реализовано», выигрыши исчезали
    (расхождение 04.09.2026: бот $140 против ~$200 у оператора).
    """
    pend = []
    for p in positions or []:
        if p.get("redeemable") is not True:
            continue
        try:
            v = float(p.get("currentValue") or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            pend.append((v, str(p.get("title", ""))))
    if not pend:
        return None
    total = sum(v for v, _ in pend)
    lines = [f"\n🏆 К выплате (выиграно, не забрано): ${total:.0f} "
             f"({len(pend)} поз.)"]
    for v, t in sorted(pend, reverse=True)[:3]:
        lines.append(f"  🟢 {smart_truncate(t, 42)}  ${v:.0f}")
    return "\n".join(lines)


def build_status_from_wallet(positions: List[dict], journal: List[dict],
                             cash_value: Optional[float] = None,
                             now=None) -> str:
    """Статус от РЕАЛЬНЫХ позиций кошелька (/positions API) — источник истины.

    positions: формат Polymarket /positions (conditionId, title, size,
    avgPrice, curPrice, currentValue, cashPnl). journal: алерты бота, нужны
    только чтобы обогатить AI-тезисом (ai_yes_estimate/horizon) по condition_id.

    Решает три дефекта журнал-подхода разом: наличные, устаревшая база P&L,
    недосчёт позиций — потому что считаем по факту кошелька, а не по алертам.
    """
    jmap = {r.get("condition_id"): r for r in journal}

    realized = realized_block(resolved_positions(positions, now=now), now=now)
    pending = pending_payout_block(positions)
    positions = filter_open_positions(positions)
    n = len(positions)
    total_invested = total_current = total_pnl = 0.0
    in_profit = in_loss = 0
    detail_lines: List[tuple] = []

    for p in positions:
        invested = float(p.get("initialValue") or 0)
        current = float(p.get("currentValue") or 0)
        pnl = position_pnl_value(p)   # см. функцию: cashPnl для открытой врёт
        total_invested += invested
        total_current += current
        total_pnl += pnl
        if pnl >= 0:
            in_profit += 1
        else:
            in_loss += 1

        ret_pct = (pnl / invested * 100.0) if invested > 0 else 0.0
        moved = abs(ret_pct) >= MOVE_THRESHOLD_PCT
        jr = jmap.get(p.get("conditionId"), {})
        hd = jr.get("horizon_days")
        near = hd is not None and 0 <= hd <= NEAR_RESOLUTION_DAYS
        if moved or near:
            q = smart_truncate(p.get("title") or "", 44)
            marker = "🟢" if pnl >= 0 else "🔴"
            avg = float(p.get("avgPrice") or 0)
            cur = float(p.get("curPrice") or 0)
            hint = None
            if avg > 0 and cur > 0:
                hint = position_action_hint(avg, cur, jr.get("ai_yes_estimate"), hd,
                                            side=str(jr.get("side", "NO")))
            # Только то, что требует решения: строка без подсказки повторяет
            # числа из шапки и не говорит, что делать (правка 15.08.2026).
            if not hint:
                continue
            line = f"{marker} {q}\n  {ret_pct:+.0f}% (${pnl:+.0f})"
            line += f"\n  → {hint}"
            detail_lines.append((abs(ret_pct), line))

    pnl_pct = (total_pnl / total_invested * 100.0) if total_invested > 0 else 0.0
    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    cash_txt = total_txt = ""
    if cash_value is not None:
        total_bal = total_current + cash_value
        total_txt = (f"\n💼 Баланс: ${total_bal:.0f} "
                     f"(позиции ${total_current:.0f} + наличные ${cash_value:.0f})")
    else:
        total_txt = f"\n💼 Позиции: ${total_current:.0f} (наличные не учтены)"

    header = (f"📊 Polymarket — дневной статус\n"
              f"{pnl_emoji} P&L ${total_pnl:+.0f} от ${total_invested:.0f} "
              f"({pnl_pct:+.0f}%)\n"
              f"Позиций: {n} · в плюсе {in_profit} / в минусе {in_loss}"
              f"{total_txt}")

    parts = [header]
    if pending:
        parts.append(pending)
    if realized:
        parts.append(realized)
    has_hint = any("→" in line for _, line in detail_lines)
    if detail_lines:
        parts.append("\nТребуют решения:")
        ranked = sorted(detail_lines, key=lambda x: x[0], reverse=True)
        for _, line in ranked[:12]:       # топ-12, чтобы сообщение не раздувалось
            parts.append(line)
        if len(ranked) > 12:
            parts.append(f"…и ещё {len(ranked) - 12} (показаны крупнейшие движения)")
    else:
        parts.append("\nЗаметных движений нет — позиции зреют.")
    if has_hint:
        parts.append("\n<i>Позиции не бинарны: их можно продать (зафиксировать) "
                     "или докупить при движении цены, не дожидаясь резолва.</i>")
    return "\n".join(parts)


def position_pnl_value(p: dict) -> float:
    """Нереализованный P&L открытой позиции = стоимость − вложено.

    НЕ берём cashPnl: в Polymarket это РЕАЛИЗОВАННЫЙ денежный поток, а для
    открытой позиции он не отражает прибыль. Полевой баг 15.08.2026 — статус
    сам себе противоречил: «P&L −$25 от $86» рядом с «Позиции: $91».
    """
    try:
        return float(p.get("currentValue") or 0) - float(p.get("initialValue") or 0)
    except (TypeError, ValueError):
        return 0.0


def position_action_hint(entry: float, current: float, ai_yes: Optional[float],
                         horizon_days: Optional[float],
                         side: str = "NO") -> Optional[str]:
    """Мягкая подсказка действия по небинарной позиции (или None).

    Порядок: фиксация прибыли → слом тезиса (режь) → докуп на просадке при
    целом тезисе → близость резолва. Иначе тишина.
    """
    if entry <= 0 or current <= 0:
        return None
    ret_pct = (current / entry - 1.0) * 100.0
    lbl = "YES" if str(side).upper() == "YES" else "NO"

    # 1. Прибыль доросла до зоны фиксации (по цене ИЛИ по доходности)
    if current >= EXIT_PARTIAL_PRICE or ret_pct >= TAKE_PROFIT_RET_PCT:
        return (f"{lbl} {entry*100:.0f}¢→{current*100:.0f}¢ (+{ret_pct:.0f}%) — "
                f"можно зафиксировать (продать часть/всё), не ждать резолва")

    # текущий edge = (1 - current_no) - ai_yes — насколько NO ещё недооценён
    cur_edge = None
    if ai_yes is not None:
        cur_edge = ((1 - current) - ai_yes) if lbl == "NO" else (ai_yes - current)

    # 2. Тезис сломан: цена против нас И edge инвертировался
    if ret_pct < ADD_DRAWDOWN_PCT and cur_edge is not None and cur_edge <= EXIT_STOP_EDGE:
        return (f"{lbl} {entry*100:.0f}¢→{current*100:.0f}¢ ({ret_pct:.0f}%) — "
                f"AI пересмотрел YES вверх, тезис под вопросом → рассмотри выход (режь)")

    # 3. Просадка, но тезис цел -> кандидат на докуп
    if ret_pct < ADD_DRAWDOWN_PCT and ai_yes is not None and ai_yes <= THESIS_INTACT_AI_YES:
        return (f"{lbl} {entry*100:.0f}¢→{current*100:.0f}¢ ({ret_pct:.0f}%) — "
                f"просадка, но тезис цел (AI YES {ai_yes*100:.0f}%) → можно докупить дешевле")

    # 4. Близко к резолву
    if horizon_days is not None and 0 <= horizon_days <= NEAR_RESOLUTION_DAYS:
        return (f"{lbl} {current*100:.0f}¢ — резолв через {horizon_days:.0f}д, "
                f"реши: держать до конца или зафиксировать сейчас")

    return None


def _open_rows(journal: List[dict]) -> List[dict]:
    return [r for r in journal
            if str(r.get("status", "open")).lower() == "open"]


def build_daily_status(journal: List[dict],
                       price_fn: Callable[[str], Optional[float]],
                       confirmed_only: bool = True,
                       portfolio_value: Optional[float] = None,
                       cash_value: Optional[float] = None) -> str:
    """Дневной статус: шапка + детали по двигавшимся/близким к резолву.

    confirmed_only=True (по умолчанию): P&L считается ТОЛЬКО по реально
    подтверждённым ончейном позициям (fill_source=onchain + entry_price_actual).
    Журнал — это лог АЛЕРТОВ, не портфель; считать P&L по непокупленным алертам
    = фантомный убыток (баг 15.06: −$114 при банке $120 по 11 алертам).
    """
    rows = _open_rows(journal)
    if confirmed_only:
        rows = [r for r in rows
                if r.get("fill_source") == "onchain"
                and r.get("entry_price_actual")]
    n = len(rows)
    if n == 0:
        return ("📊 Polymarket — дневной статус\n"
                "Подтверждённых ончейном позиций: 0.\n"
                "<i>Журнал содержит алерты, но реальных (ончейн) входов "
                "не зафиксировано — показывать P&L не по чему.</i>")

    total_unreal = 0.0
    total_stake = 0.0
    in_profit = in_loss = 0
    detail_lines: List[str] = []
    priced = 0
    skipped_garbage = 0

    for r in rows:
        cid = r.get("condition_id", "")
        entry = mtm.entry_no_price(r)
        stake = mtm.position_stake(r)
        if entry is None or entry <= 0:
            continue
        current = price_fn(cid)
        if current is None or current <= 0:
            continue                       # цена недоступна — пропускаем тихо
        if not is_plausible_price(entry, current):
            skipped_garbage += 1           # мусорная цена API — не считаем
            continue
        priced += 1
        pnl = mtm.position_pnl(entry, current, stake)
        total_unreal += pnl["unrealised"]
        total_stake += stake
        if pnl["unrealised"] >= 0:
            in_profit += 1
        else:
            in_loss += 1

        moved = abs(pnl["ret_pct"]) >= MOVE_THRESHOLD_PCT
        hd = r.get("horizon_days")
        near = hd is not None and 0 <= hd <= NEAR_RESOLUTION_DAYS
        if moved or near:
            q = smart_truncate(r.get("question") or "", 44)
            hint = position_action_hint(entry, current,
                                        r.get("ai_yes_estimate"), hd,
                                        side=str(r.get("side", "NO")))
            if not hint:
                continue          # только то, что требует решения — см. выше
            marker = "🟢" if pnl["unrealised"] >= 0 else "🔴"
            line = (f"{marker} {q}\n"
                    f"  {pnl['ret_pct']:+.0f}% (${pnl['unrealised']:+.0f})")
            line += f"\n  → {hint}"
            detail_lines.append((abs(pnl["ret_pct"]), line))

    pnl_pct = (total_unreal / total_stake * 100.0) if total_stake > 0 else 0.0
    pnl_emoji = "🟢" if total_unreal >= 0 else "🔴"
    coverage = "" if priced == n else f" ({priced}/{n} оценено)"
    header = (f"📊 Polymarket — дневной статус\n"
              f"{pnl_emoji} P&L ${total_unreal:+.0f} от ${total_stake:.0f} "
              f"({pnl_pct:+.0f}%)\n"
              f"Позиций: {n}{coverage} · в плюсе {in_profit} / в минусе {in_loss}")
    if skipped_garbage:
        header += f"\n⚠️ {skipped_garbage} с подозрительной ценой API — пропущены"
    if portfolio_value is not None:
        # Polymarket разделяет Portfolio (стоимость позиций) и Cash (свободный
        # USDC). Раньше бот показывал только позиции и звал их «кошелёк целиком»
        # — неверно. Теперь честно: позиции отдельно, наличные отдельно.
        if cash_value is not None:
            total = portfolio_value + cash_value
            header += (f"\n💼 Баланс: ${total:.0f} "
                       f"(позиции ${portfolio_value:.0f} + наличные ${cash_value:.0f})")
        else:
            header += (f"\n💼 Позиции: ${portfolio_value:.0f} "
                       f"(наличные USDC не учтены — нет источника)")

    parts = [header]
    has_hint = any("→" in line for _, line in detail_lines)
    if detail_lines:
        parts.append("\nТребуют решения:")
        # сортировка по величине движения — важное выше
        for _, line in sorted(detail_lines, key=lambda x: x[0], reverse=True):
            parts.append(line)
    else:
        parts.append("\nЗаметных движений нет — позиции зреют.")
    # дисклеймер про небинарность — только когда есть подсказка действия
    if has_hint:
        parts.append("\n<i>Позиции не бинарны: их можно продать (зафиксировать) "
                     "или докупить при движении цены, не дожидаясь резолва.</i>")
    return "\n".join(parts)


def _fetch_portfolio_value() -> Optional[float]:
    """Стоимость позиций через Polymarket Data API /value (fail-safe).

    Это Portfolio (только позиции), НЕ весь кошелёк — наличные USDC отдельно
    (см. _fetch_cash_value). None при недоступности.
    """
    try:
        import requests
        from fill_matcher import DATA_API, PROXY_WALLET
        r = requests.get(f"{DATA_API}/value",
                         params={"user": PROXY_WALLET}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list):
            return sum(float(x.get("value", 0)) for x in data) or None
        if isinstance(data, dict):
            return float(data.get("value", 0)) or None
    except Exception:
        return None
    return None


# На Polygon ДВА USDC: нативный (Polymarket использует его) и bridged USDC.e.
# Раньше брал только USDC.e -> наличные читались как 0. Проверяем оба.
_USDC_TOKENS = {
    "native": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    "bridged": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
}
_POLYGON_RPCS = [
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
]


def _erc20_balance(rpc: str, token: str, holder_padded: str) -> Optional[float]:
    import requests
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
               "params": [{"to": token, "data": "0x70a08231" + holder_padded},
                          "latest"]}
    r = requests.post(rpc, json=payload, timeout=12)
    if r.status_code != 200:
        return None
    result = r.json().get("result")
    if not result or result == "0x":
        return None
    return int(result, 16) / 1e6      # USDC = 6 знаков


def _fetch_cash_value() -> Optional[float]:
    """Свободный USDC (Cash) — баланс на адресе через Polygon RPC.

    Проверяет оба USDC-контракта (нативный + bridged) и несколько RPC с
    фолбэком: наличные могут лежать в любом из токенов, а отдельные RPC
    бывают недоступны. None только если ВСЕ комбинации не дали ответа.
    """
    try:
        from fill_matcher import PROXY_WALLET
        holder = PROXY_WALLET.lower().replace("0x", "").rjust(64, "0")
        for rpc in _POLYGON_RPCS:
            total = 0.0
            got_any = False
            for token in _USDC_TOKENS.values():
                try:
                    bal = _erc20_balance(rpc, token, holder)
                except Exception:
                    bal = None
                if bal is not None:
                    total += bal
                    got_any = True
            if got_any:
                return total            # этот RPC ответил — берём сумму обоих токенов
        return None                     # ни один RPC не ответил
    except Exception:
        return None


def _fetch_positions() -> Optional[List[dict]]:
    """Все реальные позиции кошелька через Polymarket Data API /positions.

    Источник истины — кошелёк, не журнал алертов. None при недоступности
    (тогда main деградирует на старый журнал-путь).
    """
    try:
        import requests
        from fill_matcher import DATA_API, PROXY_WALLET
        # /positions отдаёт 100 записей по умолчанию (max 500) и включает всю
        # историю кошелька. Без пагинации хвост молча отрезается по мере роста
        # истории — берём страницами до конца.
        out: List[dict] = []
        for page in range(10):
            r = requests.get(f"{DATA_API}/positions",
                             params={"user": PROXY_WALLET, "sizeThreshold": 1,
                                     "limit": 500, "offset": page * 500},
                             timeout=20)
            if r.status_code != 200:
                return out or None
            batch = r.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 500:
                break
        return out or None
    except Exception:
        return None


def main() -> None:
    import json
    from pathlib import Path
    journal_path = Path("event_journal.jsonl")
    journal = []
    if journal_path.exists():
        for line in journal_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    journal.append(json.loads(line))
                except Exception:
                    continue

    cash_value = _fetch_cash_value()
    positions = _fetch_positions()
    print(f"[diag] positions={len(positions) if positions is not None else None} "
          f"cash={cash_value} wallet={__import__('fill_matcher').PROXY_WALLET[:10]}…")

    if positions is not None:
        # источник истины — реальный кошелёк
        msg = build_status_from_wallet(positions, journal, cash_value=cash_value)
    else:
        # фолбэк: /positions недоступен — старый журнал-путь (только ончейн)
        def price_fn(cid: str) -> Optional[float]:
            try:
                return mtm.current_no_price(cid, mtm._default_fetch)
            except Exception:
                return None
        msg = build_daily_status(journal, price_fn,
                                 portfolio_value=_fetch_portfolio_value(),
                                 cash_value=cash_value)
    print(msg)
    try:
        import requests
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "HTML", "disable_notification": True},
                timeout=10).raise_for_status()
            print("sent")
    except Exception as e:  # noqa: BLE001
        print(f"telegram send failed: {e}")


if __name__ == "__main__":
    main()
