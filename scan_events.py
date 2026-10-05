"""
scan_events.py — runner for the v5 event-market scanner (manual-bet mode A).

Flow:
  1. Fetch active markets via collector.get_active_markets / priority.
  2. event_scanner.scan() applies gates + Grok mispricing check.
  3. Each candidate → Telegram alert + append to event_journal.jsonl.
  4. Dedup against already-alerted condition_ids (state in event_seen.json).

No bets are placed. The journal records market price, AI estimate and entry so
the edge can be validated against real outcomes after ~6-8 weeks.

Run:  python scan_events.py
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
import collector
import event_scanner as es
import ai_cache
from ai_context import estimate_probability

JOURNAL = Path("event_journal.jsonl")   # one JSON line per alerted candidate
CALIB = Path("calibration_journal.jsonl")  # one line per AI estimate (paid-for data)
SEEN = Path("event_seen.json")          # condition_ids already alerted
AI_CACHE = Path("ai_cache.json")        # cached Grok estimates per condition_id
MARKET_FETCH_LIMIT = 1000                # deeper slice — more mid-liquidity events
MAX_AI_CALLS = 40                        # cap Grok calls per run (cost control)


def _load_ai_cache() -> dict:
    if AI_CACHE.exists():
        try:
            d = json.loads(AI_CACHE.read_text())
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _save_ai_cache(store: dict) -> None:
    try:
        AI_CACHE.write_text(json.dumps(store, ensure_ascii=False, sort_keys=True))
    except Exception as e:
        print(f"  ⚠️ ai_cache save failed: {e}")


# Re-alert when a previously-seen market's edge has grown by at least this much.
# A market we passed on at 12pp but that's now 25pp is a genuinely better setup,
# not a duplicate — suppressing it forever loses real signal.
REALERT_EDGE_GROWTH = 0.10


def _normalize_seen(raw) -> dict:
    """Accept either the legacy flat list of cids or the new dict form.

    Legacy entries get last_edge=None so the next scan alerts once (capturing
    their edge), instead of being silently suppressed forever.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {cid: {"last_edge": None, "alerted_at": None, "resolved": False}
                for cid in raw}
    return {}


def _load_seen() -> dict:
    if SEEN.exists():
        try:
            return _normalize_seen(json.loads(SEEN.read_text()))
        except Exception:
            return {}
    return {}


def _save_seen(seen: dict) -> None:
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, sort_keys=True, indent=0))


HELD_LONG_SKIP_DAYS = 30    # уже открытую позицию с резолвом дальше этого не
                            # гоняем через дорогой поиск — edge на длинном тезисе
                            # за сутки не меняется, докуп решается движением цены


def _should_skip_pre_ai(cid: str, seen: dict, held_cids: set = None,
                        hours_to_resolve: float = None) -> bool:
    """Skip BEFORE spending an AI call.

    Always skip resolved markets (dead). Also skip a market we ALREADY HOLD
    whose resolution is far away (> HELD_LONG_SKIP_DAYS): re-estimating a
    long-horizon open position via expensive search is near-useless — the edge
    barely moves day-to-day, and the add/exit decision comes from price moves
    (daily status, no AI). Near-resolution held positions are NOT skipped (the
    endgame matters), and brand-new candidates are NOT skipped (we must decide
    whether to enter).
    """
    entry = seen.get(cid)
    if entry and entry.get("resolved"):
        return True
    if (held_cids is not None and cid in held_cids
            and hours_to_resolve is not None
            and hours_to_resolve > HELD_LONG_SKIP_DAYS * 24):
        return True
    return False


def _should_alert(cid: str, current_edge: float, seen: dict) -> bool:
    """Post-AI: alert if new, or if edge grew materially since last alert.

    Legacy entries (last_edge None) were already alerted under the old flat-set
    format — re-alerting them would spam duplicates of known positions. They're
    suppressed here; run() records their current edge silently so future growth
    is measured against it.
    """
    entry = seen.get(cid)
    if not entry:
        return True                      # never seen — alert
    last = entry.get("last_edge")
    if last is None:
        return False                     # migrated/legacy — record silently
    return (current_edge - last) >= REALERT_EDGE_GROWTH


def _should_alert_thesis(cid: str, thesis_key: str,
                         current_edge: float, seen: dict) -> bool:
    """Межпрогонная память тезисов (SpaceX-кейс 10.06).

    Внутрипрогонный дедуп не ловит вариант того же тезиса («above $2T» после
    вчерашнего «above $2.2T»), пришедший другим прогоном с другим cid. Правило
    то же, что для ре-алертов: новый cid ИЗВЕСТНОГО тезиса алертится только
    если edge вырос на REALERT_EDGE_GROWTH относительно лучшего из seen.
    """
    if not thesis_key:
        return True
    best = None
    for other_cid, entry in seen.items():
        if other_cid == cid:
            continue
        if entry.get("thesis_key") != thesis_key:
            continue
        le = entry.get("last_edge")
        if le is not None and (best is None or le > best):
            best = le
    if best is None:
        return True
    return (current_edge - best) >= REALERT_EDGE_GROWTH


def _prune_seen(seen: dict, resolved_cids: set) -> dict:
    """Drop resolved markets from state so the file doesn't grow without bound."""
    return {cid: v for cid, v in seen.items() if cid not in resolved_cids}


def _load_journal_rows() -> list:
    """Read journal rows (for prune-by-resolved). Empty if no journal yet."""
    return _load_jsonl_rows(JOURNAL)


def _load_jsonl_rows(path: Path) -> list:
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


def _journal_row(candidate: es.Candidate, re_alert: bool = False) -> dict:
    """Build a journal row. Re-alerts (edge grew on a known market) are the
    SAME position seen again — their rows carry status='re_alert' so exposure,
    fill-matching and mark-to-market never double-count them."""
    row = candidate.to_alert()
    row["alerted_at"] = datetime.now(timezone.utc).isoformat()
    _side = str(getattr(candidate, "side", "NO")).upper()
    row["side"] = _side
    row["bet_side"] = _side          # реальная сторона (NO осн., YES средняя зона)
    row["stake_plan"] = "manual $15-70 flat"
    row["status"] = "re_alert" if re_alert else "open"
    # Thesis category — powers the exposure-by-category visibility. The limit
    # itself is an operator rule; we just make the number visible.
    try:
        import category_exposure as cx
        row["category"] = cx.classify(candidate.question,
                                      slug=getattr(candidate, "event_slug", None))
    except Exception:
        row["category"] = "other"
    # horizon in days from alert to resolution — lets verify_journal split the
    # Brier by horizon so the verdict on short bets doesn't wait for 2027.
    try:
        end = datetime.fromisoformat(str(candidate.end_date).replace("Z", "+00:00"))
        row["horizon_days"] = round(
            (end - datetime.now(timezone.utc)).total_seconds() / 86400, 1)
    except Exception:
        row["horizon_days"] = None
    # Filled by hand after placing the bet — verification prefers these over the
    # alert-time price/stake so it scores what was actually traded, not theory.
    row["entry_price_actual"] = None
    row["stake_actual"] = None
    return row


def _append_journal(candidate: es.Candidate, re_alert: bool = False) -> None:
    with JOURNAL.open("a") as f:
        f.write(json.dumps(_journal_row(candidate, re_alert),
                           ensure_ascii=False) + "\n")


def _append_calibration(row: dict) -> None:
    """One line per AI estimate — including rejected markets.

    We pay for every Grok call regardless of whether the market clears EDGE_MIN.
    Logging *all* of them turns the rejects into free calibration data: later we
    can score Grok's P(YES) against real outcomes (Brier) and, crucially, compare
    it to the market price's own Brier on the same markets. That comparison is
    the single question this whole strategy rests on — does Grok beat the crowd?
    The bet journal alone can't answer it (it's the selected, biased tail).
    """
    with CALIB.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_logging_estimator(markets: list):
    """Wrap estimate_probability so every call is journalled for calibration.

    `markets` is the gated slice we're about to evaluate. We index it by question
    text so the wrapper can attach the market's price/liquidity/condition_id to
    each logged estimate, even for markets that later get rejected on edge.
    """
    by_q = {}
    for m in markets:
        q = m.get("question", "") or m.get("title", "")
        if q:
            by_q[q] = m
    run_ts = datetime.now(timezone.utc).isoformat()

    # Per-condition_id cache so a 6h re-scan of the same slow market doesn't pay
    # for a fresh Grok call. Invalidated by age or a >5pp YES-price move.
    cache_store = _load_ai_cache()

    def _yes_for(question: str):
        m = by_q.get(question, {})
        parsed = es._parse_prices(m) if m else None
        return parsed[0] if parsed else None

    def _cid_for(question: str):
        return by_q.get(question, {}).get("conditionId", "")

    # Двухэтапная оценка: дешёвый скрин без поиска -> дорогой поиск ($5/1000)
    # только на рынки с сильным скрин-edge. Кэш стоит ПЕРЕД этим, так что
    # повторный скан слоумувинг-рынка не платит ни за один этап.
    import yes_strategy as ys

    def _yes_confirm(yes_price, cheap_prob):
        return ys.yes_gate(yes_price) and cheap_prob > ys.GROK_YES_THRESHOLD

    two_stage = es.make_two_stage_estimator(
        estimate_probability, yes_price_for=_yes_for, screen_edge_min=es.EDGE_MIN,
        yes_confirm=_yes_confirm)

    cached_real = ai_cache.make_cached_estimator(
        two_stage, cache_store, cid_for=_cid_for, yes_for=_yes_for)

    def _estimator(question: str, description: str = None, end_date: str = None):
        cid = _cid_for(question)
        was_cached = cid in cache_store and ai_cache.is_valid(
            cache_store.get(cid, {}), __import__("time").time(),
            _yes_for(question) or -1)
        est = cached_real(question, description, end_date)
        m = by_q.get(question, {})
        parsed = es._parse_prices(m) if m else None
        yes_price = round(parsed[0], 4) if parsed else None
        no_price = round(parsed[1], 4) if parsed else None
        ai_yes = est.get("prob") if est else None
        edge = (round(yes_price - ai_yes, 4)
                if (yes_price is not None and ai_yes is not None) else None)
        # horizon to resolution — lets the Brier verdict split short vs long so
        # short-horizon bets give an early read without waiting on 2027 resolves.
        horizon_days = None
        try:
            from datetime import datetime as _dt
            _end = _dt.fromisoformat(str(m.get("endDate", "")).replace("Z", "+00:00"))
            horizon_days = round(
                (_end - _dt.now(timezone.utc)).total_seconds() / 86400, 1)
        except Exception:
            pass
        # Only log calibration on a REAL Grok call — a cache hit is the same
        # estimate and would double-count in the Brier sample.
        if not was_cached:
          try:
            _append_calibration({
                "estimated_at": run_ts,
                "condition_id": m.get("conditionId", ""),
                "question": question,
                "market_yes_price": yes_price,   # the crowd's P(YES) — baseline
                "no_price": no_price,
                "ai_yes_estimate": ai_yes,       # Grok's P(YES) — what we score
                "ai_conf": est.get("conf") if est else None,
                # с поиском или из памяти — чтобы на чекпойнте сравнить Brier
                "searched": est.get("searched") if est else None,
                "edge": edge,
                "liquidity": round(float(m.get("liquidity", 0) or 0), 2),
                "end_date": str(m.get("endDate", "")),
                "horizon_days": horizon_days,
                # Will this market produce a NO bet? (mirrors scanner thresholds)
                "would_bet": bool(
                    est and ai_yes is not None and edge is not None
                    and edge >= es.EDGE_MIN
                    and es._CONF_RANK.get(est.get("conf", "low"), 0)
                        >= es._CONF_RANK[es.MIN_CONFIDENCE]
                ),
            })
          except Exception as e:
            print(f"  ⚠️ calibration log failed: {e}")
        return est

    # expose the cache store so run() can persist it after the scan
    _estimator._cache_store = cache_store
    return _estimator


def _market_url(c: es.Candidate) -> str:
    # /event/<eventSlug> is the only slug the site reliably resolves. The market
    # slug carries a numeric id tail that 404s and can't be safely trimmed
    # (the date looks like the same tail), so we don't guess — if there's no
    # event slug, link to a site search by the question, which always resolves.
    if c.event_slug:
        return f"https://polymarket.com/event/{c.event_slug}"
    from urllib.parse import quote_plus
    return f"https://polymarket.com/markets?_q={quote_plus(c.question)}"


LONG_LOCK_DAYS = 120   # дольше — флаг «длинная заморозка капитала»
LONG_WINDOW_WARN_DAYS = 90   # дольше — предупреждение о риске окна для NO-ставки


DROP_ALERT_PP = 0.10        # падение цены YES за сутки, после которого — метка
DROP_ALERT_HORIZON_H = 72   # ...если до события не больше трёх суток


def _price_ago(cid: str, calib_rows: list, now: datetime,
               lo_h: float = 18, hi_h: float = 36) -> Optional[float]:
    """Цена YES рынка примерно сутки назад — из калибровочного журнала (он
    пишет market_yes_price на каждую оценку). Берём последнюю запись в окне
    [now−hi_h, now−lo_h]; сканы идут ~2 раза в сутки, окно их ловит."""
    lo = (now - timedelta(hours=hi_h)).isoformat()
    hi = (now - timedelta(hours=lo_h)).isoformat()
    best = None
    for r in calib_rows or []:
        if r.get("condition_id") != cid or r.get("market_yes_price") is None:
            continue
        ts = str(r.get("estimated_at") or "")
        if lo <= ts <= hi and (best is None or ts >= best[0]):
            best = (ts, float(r["market_yes_price"]))
    return best[1] if best else None


def _drop_line(c: es.Candidate) -> str:
    """Метка: цена YES резко упала за сутки, а событие вот-вот. Бразилия 04.10:
    Flávio 74→57¢, Lula 73→61¢ за ночь перед голосованием — рынок узнал новое,
    а зона 50-65% подала это как недооценку. Данных мало (5 случаев, 2 из них
    выиграли) — поэтому метка, а не запрет."""
    ago = getattr(c, "price_24h_ago", None)
    if ago is None:
        return ""
    drop = float(ago) - float(c.market_yes_price)
    hrs = es._hours_to_resolve({"endDate": c.end_date})
    if drop < DROP_ALERT_PP or hrs is None or hrs > DROP_ALERT_HORIZON_H:
        return ""
    return (f"⚠️ Цена упала на {drop*100:.0f}пп за сутки ({ago*100:.0f}→"
            f"{c.market_yes_price*100:.0f}¢), а до события ≤3д — рынок, вероятно, "
            f"знает новое. Не «дёшево», а «что-то случилось»")


def _event_key(slug: Optional[str]) -> str:
    """Ключ события: первые три слова event_slug. Связывает исходы одного
    события на разных рынках (brazil-presidential-election-first-round-
    winner / -2nd-place / -3rd-place), но не разные штаты/гонки."""
    parts = [p for p in str(slug or "").lower().split("-") if p]
    return "-".join(parts[:3]) if len(parts) >= 3 else ""


def _related_line(c: es.Candidate, batch: list, open_rows: list) -> str:
    """Связанные исходы: другие алерты этого прогона и открытые позиции на то
    же событие. Бразилия 04.10: Lula 1-й, Flávio 2-й, Renan 3-й — одна ставка
    «порядок из опросов сохранится», пришедшая тремя сигналами."""
    key = _event_key(getattr(c, "event_slug", ""))
    if not key:
        return ""
    n_batch = sum(1 for o in batch or []
                  if o is not c and o.condition_id != c.condition_id
                  and _event_key(getattr(o, "event_slug", "")) == key)
    n_open = sum(1 for r in open_rows or []
                 if str(r.get("status", "open")).lower() == "open"
                 and float(r.get("stake_actual") or 0) > 0
                 and r.get("condition_id") != c.condition_id
                 and _event_key(r.get("event_slug") or r.get("slug")) == key)
    if not n_batch and not n_open:
        return ""
    bits = []
    if n_batch:
        bits.append(f"ещё {n_batch} {_plural(n_batch, 'алерт', 'алерта', 'алертов')} сейчас")
    if n_open:
        bits.append(f"{n_open} {_plural(n_open, 'открытая позиция', 'открытые позиции', 'открытых позиций')}")
    return ("🔗 Связано: " + " и ".join(bits) + " на то же событие — исходы "
            "зависят друг от друга, это одна ставка, а не несколько")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _format_alert(c: es.Candidate, related_line: str = "") -> str:
    """v2 (UX-фидбек оператора): за 10 секунд должно быть ясно ЧТО делать.

    Строка 1: вопрос. Строка 2: действие глаголом — «Купить NO ~38¢», размер,
    дата резолва, флаг длинной заморозки. Дальше одна рамка вероятностей
    (рынок vs Grok → переоценка), довод по-русски без двойных маркеров,
    ликвидность и экспозиция категории («уже открыто» — не путать с размером).
    Стрелка «→» используется ровно один раз — у вывода переоценки.
    """
    if c.suspicious:
        fire = "⚠️"
    elif c.edge >= 0.25 and getattr(c, "side", "NO") != "YES":
        # у YES разрыв Grok−рынок силы сигнала не означает (политика §2):
        # Бразилия 04.10 — три 🔥 с разрывом 27-43пп, все проиграли
        fire = "🔥"
    else:
        fire = "✅"
    url = _market_url(c)

    # дата + заморозка. endDate рынка — нижняя граница резолва, не дата выплаты:
    # подсчёт голосов, второй тур (Джорджия), споры оракула отодвигают её.
    end_h = "?"
    lock = ""
    try:
        _end = datetime.fromisoformat(str(c.end_date).replace("Z", "+00:00"))
        end_h = _end.strftime("%d.%m.%Y")
        d = int((_end - datetime.now(timezone.utc)).total_seconds() // 86400)
        if d > LONG_LOCK_DAYS:
            lock = f" · ⏳{d}д заморозки"
        elif d >= 0:
            end_h += f" ({d}д)"
    except Exception:
        pass

    # ставка: фиксированная (решение оператора 04.10.2026 — $30 на всё)
    size_txt = ""
    try:
        from config import OPERATOR_STAKE
        size_txt = f" · ставка ${OPERATOR_STAKE:.0f}"
        if getattr(c, "side", "NO") == "YES":
            import yes_strategy as ys
            if ys.above_breakeven(c.market_yes_price):
                size_txt += (f" · ⚠️ дороже безубытка "
                             f"({ys.YES_ASSUMED_WR*100:.0f}¢ при WR "
                             f"{ys.YES_ASSUMED_WR*100:.0f}%)")
    except Exception:
        pass

    # экспозиция категории — доллары и число ставок (без банка и капа)
    cat_line = ""
    try:
        import category_exposure as cx
        cat = cx.classify(c.question, slug=getattr(c, "event_slug", None))
        rows = _load_journal_rows()
        cur = cx.exposure_by_category(rows).get(cat, 0.0)
        n = cx.exposure_counts(rows).get(cat, 0)
        cat_line = f" · в {cat} уже открыто ${cur:.0f} ({n} ставок)"
    except Exception:
        pass

    # довод: без ведущих маркеров Grok ("• ", "- ")
    why = (c.reasoning or "").strip().lstrip("•-– ").strip()

    liq_k = f"${c.liquidity/1000:.0f}k" if c.liquidity >= 1000 else f"${c.liquidity:.0f}"

    # YES-ветка: новая стратегия средней зоны — своё действие и пометки
    if getattr(c, "side", "NO") == "YES":
        import yes_strategy as _ys
        yes_cents = f"{c.market_yes_price*100:.0f}¢"
        # Показываем то, что сказал Grok (по этому числу и судится согласие),
        # а не константу корзины калибровки.
        _raw = getattr(c, "ai_yes_raw", None)
        grok_shown = _raw if _raw is not None else c.ai_yes_estimate
        lines = [
            f"{fire} {c.question}",
            f"Купить YES ~{yes_cents}{size_txt} · резолв ≥{end_h}{lock}",
            "",
            f"Рынок YES: {c.market_yes_price*100:.0f}% · "
            f"Grok: {grok_shown*100:.0f}% ({c.ai_conf}) — согласие по YES",
            f"🧪 Новая стратегия средней зоны ({_ys.YES_MIN*100:.0f}-"
            f"{_ys.YES_MAX*100:.0f}%) — ещё НЕ валидирована, "
            "решай сам, копим выборку",
        ]
        if why:
            lines.append(f"Почему: {why}")
        if c.suspicious:
            lines.append("⚠️ Похоже на связанный/групповой рынок — читай правила")
        drop = _drop_line(c)
        if drop:
            lines.append(drop)
        if related_line:
            lines.append(related_line)
        lines.append(f"Ликв. {liq_k}{cat_line}")
        lines.append("")
        lines.append("Чек: свежая цена · правила резолва · лимит категории")
        if c.event_slug:
            lines.append(f"🔗 https://polymarket.com/event/{c.event_slug}")
        return "\n".join(lines)

    cents = f"{c.no_price*100:.0f}¢"
    lines = [
        f"{fire} {c.question}",
        f"Купить NO ~{cents}{size_txt} · резолв ≥{end_h}{lock}",
        "",
        f"Рынок верит в YES: {c.market_yes_price*100:.0f}% · "
        f"Grok: {c.ai_yes_estimate*100:.0f}% ({c.ai_conf}) → "
        f"переоценка {c.edge*100:.0f}пп",
    ]
    if why:
        lines.append(f"Почему: {why}")
    # Риск длинного окна: NO выигрывает, только если событие НЕ случится за срок.
    # Чем длиннее окно, тем выше шанс, что оно поймает само событие — оператор
    # отметил, что для «он точно уйдёт, вопрос когда» короткий срок безопаснее.
    try:
        _end2 = datetime.fromisoformat(str(c.end_date).replace("Z", "+00:00"))
        _d2 = int((_end2 - datetime.now(timezone.utc)).total_seconds() // 86400)
        if _d2 > LONG_WINDOW_WARN_DAYS:
            lines.append(
                f"⚠️ Длинное окно ({_d2}д): NO проигрывает, если событие случится "
                f"в срок. На том же тезисе ищи более короткий срок — NO надёжнее.")
    except Exception:
        pass
    if c.suspicious:
        lines.append("⚠️ Похоже на связанный/групповой рынок — читай правила резолва")
    lines.append(f"Ликв. {liq_k}{cat_line}")
    lines.append("")
    lines.append("Чек: свежая цена · правила резолва · лимит категории")
    if url:
        lines.append(f"🔗 {url}")
    return "\n".join(lines)


# ── ВЕРДИКТ 02.08.2026 ────────────────────────────────────────────────────────
# Предзарегистрированные ворота сработали на n=278 (в 9× больше требуемых 30):
# Brier рынка 0.141 vs Grok 0.214, Δ=-0.073 — рынок точнее. Калибровка честно
# помогла (Grok 0.293→0.214, разрыв сократился вдвое), но НЕ обогнала рынок.
# Деньги подтверждают: сигналы WR 14%/ROI -71%, реальные ончейн ROI -100%.
# Экономика добивает: банк ~$200 против $20-50/мес за xAI — окупаемость требует
# 10-25% в месяц, чего нет даже у топ-китов лидерборда.
# Решение: NO-алерты ВЫКЛЮЧЕНЫ. Журналирование остаётся — данные копятся даром.
# YES средней зоны пока живёт: у неё своё основание (в корзине 0.6-0.8 реальный
# YES 86% — Grok недооценивает фаворитов именно там), но всего 13 резолвов;
# ждём раздельного вердикта.
NO_STRATEGY_ALERTS = False


def should_alert_side(side) -> bool:
    """Слать ли алерт по этой стороне. NO выключен вердиктом, YES — жив."""
    s = str(side or "NO").upper()
    if s == "YES":
        return True
    return NO_STRATEGY_ALERTS


def _send(msg: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  ⚠️ Telegram creds missing — printing instead:\n", msg)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "disable_web_page_preview": False},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  ❌ send failed: {e}")
        return False


def run() -> None:
    print(f"[{datetime.now()}] event scan start")
    seen = _load_seen()
    # Backfill: записи seen до введения тезис-памяти не несут thesis_key —
    # восстанавливаем из вопросов журнала по cid, иначе память не покрывает
    # уже открытые позиции (а SpaceX-кейс именно про них).
    try:
        q_by_cid = {r.get("condition_id"): r.get("question", "")
                    for r in _load_journal_rows()}
        for cid, entry in seen.items():
            if not entry.get("thesis_key") and q_by_cid.get(cid):
                entry["thesis_key"] = es._thesis_key(q_by_cid[cid])
    except Exception:
        pass
    # Prune resolved markets from seen state so the file doesn't grow forever.
    # A position closed in the journal is resolved/exited — its cid is dead.
    try:
        resolved = {r.get("condition_id", "") for r in _load_journal_rows()
                    if str(r.get("status", "open")).lower() == "closed"}
        if resolved:
            before = len(seen)
            seen = _prune_seen(seen, resolved)
            if len(seen) < before:
                print(f"  pruned {before - len(seen)} resolved cids from seen")
    except Exception:
        pass

    markets = collector.get_active_markets(limit=MARKET_FETCH_LIMIT)
    print(f"  fetched {len(markets)} active markets")

    # Show the raw field shapes of the first market once — lets us spot when
    # live Gamma returns prices/liquidity in a different format than expected.
    if markets:
        m0 = markets[0]
        print("  --- sample market fields ---")
        for k in ("question", "outcomes", "outcomePrices", "liquidity",
                  "volume", "endDate", "conditionId", "slug", "eventSlug"):
            print(f"    {k}: {type(m0.get(k)).__name__} = {repr(m0.get(k))[:70]}")

    # Cheap structural gate first (no AI). Track WHY each market fails so a
    # zero-candidate run is debuggable from the log alone.
    funnel = {"sport_or_hft": 0, "not_binary_yesno": 0, "no_out_of_band": 0,
              "low_liquidity": 0, "bad_time": 0, "already_seen": 0, "PASS": 0}
    # Уже открытые позиции (в журнале, не закрытые) — для пропуска дорогой
    # переоценки длинных held-ставок.
    held_cids = {r.get("condition_id", "") for r in _load_journal_rows()
                 if str(r.get("status", "open")).lower() != "closed"}

    no_band_count = 0
    gated = []
    for m in markets:
        cid = m.get("conditionId", "")
        _hrs = es._hours_to_resolve(m)
        if _should_skip_pre_ai(cid, seen, held_cids=held_cids,
                               hours_to_resolve=_hrs):
            funnel["already_seen"] += 1
            continue
        q = m.get("question", "") or m.get("title", "")
        if not q or es._is_sport_or_hft(q):
            funnel["sport_or_hft"] += 1
            continue
        parsed = es._parse_prices(m)
        if not parsed:
            funnel["not_binary_yesno"] += 1
            continue
        _, no_price = parsed
        if not (es.NO_ODDS_MIN <= no_price < es.NO_ODDS_MAX):
            funnel["no_out_of_band"] += 1
            continue
        no_band_count += 1
        liq = float(m.get("liquidity", 0) or m.get("volume", 0) or 0)
        if liq < es.MIN_LIQUIDITY:
            funnel["low_liquidity"] += 1
            continue
        hrs = es._hours_to_resolve(m)
        too_late = (es.MAX_DAYS_TO_RESOLVE is not None
                    and hrs is not None and hrs > es.MAX_DAYS_TO_RESOLVE * 24)
        if hrs is None or hrs < es.MIN_HOURS_TO_RESOLVE or too_late:
            funnel["bad_time"] += 1
            continue
        funnel["PASS"] += 1
        gated.append(m)

    print(f"  --- gate funnel ---")
    for k, v in funnel.items():
        print(f"    {k}: {v}")
    print(f"  {len(gated)} passed structural gate (event + NO {es.NO_ODDS_MIN}-{es.NO_ODDS_MAX} + liquid)")

    # The AI-call cap slices the FRONT of this list, so order matters: with raw
    # Gamma order the best candidates can fall past the cap and never get an AI
    # estimate. Prioritise (1) NO inside the validated core band 0.10-0.50, then
    # (2) NO nearest the centre of that band (where the event-bias edge is
    # cleanest), then (3) deeper liquidity (better fills, more reliable price).
    def _priority(m):
        parsed = es._parse_prices(m) or (0.0, 0.0)
        no_price = parsed[1]
        in_core = es.CORE_NO_MIN <= no_price < es.CORE_NO_MAX
        core_centre = (es.CORE_NO_MIN + es.CORE_NO_MAX) / 2
        liq = float(m.get("liquidity", 0) or m.get("volume", 0) or 0)
        # sort key: core first (False<True so negate), then closeness to centre,
        # then more liquidity first (negate).
        return (not in_core, abs(no_price - core_centre), -liq)

    gated.sort(key=_priority)
    gated = gated[:MAX_AI_CALLS]  # cost cap (now applied to the best slice)

    # Wrap the estimator so every Grok call (rejects included) lands in the
    # calibration journal — see _append_calibration for why this matters.
    logging_estimator = _make_logging_estimator(gated)
    # Счётчик отказов: если ВСЕ оценки провалились (типично при балансе xAI $0),
    # сканер не должен молчать — иначе «нет кандидатов» неотличимо от «API мёртв».
    _est_stats = {"calls": 0, "fails": 0}
    _inner = logging_estimator
    def logging_estimator(q, description=None, end_date=None):  # noqa: F811
        _est_stats["calls"] += 1
        r = _inner(q, description, end_date)
        if not r or r.get("prob") is None:
            _est_stats["fails"] += 1
        return r
    candidates = es.scan(gated, logging_estimator)
    print(f"  {len(candidates)} candidates after AI mispricing check")

    # YES-стратегия средней зоны (параллельно, боевой режим). Использует те же
    # прогатенные рынки и тот же логирующий эстиматор (калибровка внутри).
    try:
        yes_candidates = es.scan_yes(gated, logging_estimator)
        print(f"  {len(yes_candidates)} YES-candidates (mid-zone strategy)")
        candidates = list(candidates) + list(yes_candidates)
    except Exception as e:
        print(f"  scan_yes failed: {e}")

    # Алерт при полном отказе AI (нет кредитов / API down): честно сообщить,
    # а не выдать тишину за «нет интересных рынков».
    if _est_stats["calls"] >= 3 and _est_stats["fails"] == _est_stats["calls"]:
        try:
            import requests as _req
            from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                _req.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID,
                          "text": ("⚠️ Polymarket-сканер: Grok недоступен — все "
                                   f"{_est_stats['calls']} оценок не прошли. Вероятно, "
                                   "кончились кредиты xAI (баланс $0). Оценка рынков не "
                                   "работает, пока не пополнишь. Это не «нет сигналов» — "
                                   "это отказ API.")},
                    timeout=10)
        except Exception as e:
            print(f"  no-credit alert failed: {e}")

    # Цена ~сутки назад — для метки резкого падения перед событием (и в журнал,
    # чтобы проверить метку на данных).
    try:
        _calib_rows = _load_jsonl_rows(CALIB)
        _now_dt = datetime.now(timezone.utc)
        for c in candidates:
            if getattr(c, "side", "NO") == "YES":
                c.price_24h_ago = _price_ago(c.condition_id, _calib_rows, _now_dt)
    except Exception as e:
        print(f"  price-24h lookup failed: {e}")

    sent = 0
    suppressed = 0
    outgoing = []
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        cid = c.condition_id
        thesis = es._thesis_key(c.question)
        if not _should_alert(cid, c.edge, seen):
            # Migrated legacy entries (last_edge None) get their baseline edge
            # recorded SILENTLY here. Ordinary suppressions must NOT update
            # last_edge — otherwise a slow per-run creep ratchets the baseline
            # up and the 10pp re-alert threshold can never accumulate.
            if cid in seen and seen[cid].get("last_edge") is None:
                seen[cid]["last_edge"] = c.edge
                seen[cid]["thesis_key"] = thesis
            suppressed += 1
            continue
        if not _should_alert_thesis(cid, thesis, c.edge, seen):
            # вариант уже известного тезиса (другой порог/дата того же события)
            # — фиксируем молча, не дублируем по смыслу
            seen[cid] = {"last_edge": c.edge, "alerted_at": None,
                         "resolved": False, "thesis_key": thesis}
            suppressed += 1
            continue
        is_re_alert = cid in seen   # known market whose edge grew — same position
        # Вердикт 02.08: NO-алерты выключены (ворота сработали). Кандидата
        # по-прежнему журналируем — выборка копится бесплатно, но не зовём
        # оператора в ставку, которая измеренно не имеет edge.
        if should_alert_side(getattr(c, "side", "NO")):
            outgoing.append(c)      # шлём после цикла — нужен весь пакет для связей
        else:
            suppressed += 1
        _append_journal(c, re_alert=is_re_alert)
        seen[cid] = {"last_edge": c.edge, "alerted_at": now, "resolved": False,
                     "thesis_key": thesis}

    try:
        _open_rows = _load_journal_rows()
    except Exception:
        _open_rows = []
    for c in outgoing:
        try:
            rel = _related_line(c, outgoing, _open_rows)
        except Exception:
            rel = ""
        if _send(_format_alert(c, related_line=rel)):
            sent += 1

    _save_seen(seen)
    # Persist the AI estimate cache for the next run.
    if hasattr(logging_estimator, "_cache_store"):
        _save_ai_cache(logging_estimator._cache_store)
    print(f"[{datetime.now()}] done — {sent} alerts sent, "
          f"{suppressed} suppressed (seen), journal updated")


if __name__ == "__main__":
    run()
