"""
event_scanner.py — direct event-market scanner (v5 strategy).

Replaces the whale-copy approach. Instead of reacting to large trades (which
skew toward sports, where we have no edge), we scan event markets directly and
flag the only region with a validated, theory-backed edge:

    EVENT (not sport) + NO side underpriced + market overprices YES.

Edge source: optimism/longshot bias on binary event markets. Crowds overpay for
"YES, it will happen", so NO is systematically underpriced. Confirmed in our data
(NO on events: WR ~81%). Sport has no such bias and is excluded.

Pipeline:
  1. Fetch active markets (collector.get_active_markets / priority).
  2. Gate: event-only, binary, NO in [0.10, 0.50], min liquidity, time window.
  3. AI estimates true P(YES). Trade only if market overprices YES by >= EDGE_MIN.
  4. Emit candidate with explicit "market X% vs estimate Y%" reasoning.

This module is pure logic + Gamma reads. AI call is injected so it can be tested
offline. No keys handled here.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable

from config import (
    ODDS_FILTER_MIN, ODDS_FILTER_MAX,
)

# ── Strategy constants (frozen; see changelog) ─────────────────────────────
NO_ODDS_MIN = 0.07          # skip NO cheaper than this (resolution-risk longshots)
NO_ODDS_MAX = 0.60          # widened from 0.50 to capture more event mispricing
CORE_NO_MIN = 0.10          # validated "core" band (for journal tagging)
CORE_NO_MAX = 0.50
# Liquidity floor is derived from the largest manual stake (config), so our bet
# stays a small slice of the book and we don't move the price on entry/exit.
# Falls back to a safe constant if config isn't importable in a test context.
try:
    from config import MIN_LIQUIDITY_EVENT as MIN_LIQUIDITY
except Exception:
    MIN_LIQUIDITY = 5_000   # $ — thin markets have unreliable prices & bad fills
MIN_HOURS_TO_RESOLVE = 12   # avoid HFT/last-minute markets (lower bound only)
MAX_DAYS_TO_RESOLVE = None  # no upper cap — event markets resolve months out
EDGE_MIN = 0.15             # required gap: market P(YES) - AI P(YES) >= 15pp
# Suspicious-edge guard: in an honest single market, NO price ≈ P(NO). If the
# AI claims a huge mispricing (edge >= SUSPICIOUS_EDGE) yet the market still
# prices NO near 50/50, the market disagrees with us hard — usually because it's
# a LINKED/grouped market ("what happens first") where our single-event logic
# doesn't apply. Flag, don't trust.
SUSPICIOUS_EDGE = 0.35
SUSPICIOUS_NO_LOW = 0.40    # NO price band where a huge edge is implausible
SUSPICIOUS_NO_HIGH = 0.60
MIN_CONFIDENCE = "medium"   # ignore low-confidence AI estimates (too noisy to trade)
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}

# Sport / HFT exclusion — these are the streams where we have NO edge.
#
# Split into two classes because a naive substring match misfires:
#   "match" matched "rematch"; "atp"/"epl" can hide inside ordinary words.
#   ("beat" was dropped entirely — too ambiguous: "earnings beat", "heat beat".) Whole-word markers are matched
#   with \b boundaries; markers carrying punctuation/spaces (which \w boundaries
#   don't handle) stay as substring checks.
SPORT_WORD_MARKERS = [
    "handicap", "moneyline", "match", "friendly",
    "nba", "nfl", "nhl", "mlb", "wta", "atp", "fifa", "ucl", "epl",
    "bundesliga",
]
SPORT_SUBSTR_MARKERS = [
    " vs ", " vs. ", "vs the", "win on", "la liga", "serie a",
    "-1.5", "+1.5", "-2.5", "+2.5",
]
HFT_WORD_MARKERS = ["updown"]
HFT_SUBSTR_MARKERS = ["15m", "15 min", "up or down", "this hour", "next hour"]

_SPORT_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in SPORT_WORD_MARKERS) + r")\b")
_HFT_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in HFT_WORD_MARKERS) + r")\b")


@dataclass
class Candidate:
    question: str
    condition_id: str
    market_yes_price: float     # market-implied P(YES)
    no_price: float             # price of the NO token (our entry)
    ai_yes_estimate: float      # AI's independent P(YES)
    edge: float                 # market_yes_price - ai_yes_estimate
    liquidity: float
    end_date: str
    reasoning: str
    slug: str = ""               # market slug (fallback)
    event_slug: str = ""         # event slug — this is what /event/<slug> needs
    band: str = "core"          # "core" (0.10-0.50, validated) or "extended"
    suspicious: bool = False    # likely a linked/grouped market — treat with care
    ai_conf: str = "low"        # Grok's stated confidence (low/medium/high)
    had_rules: bool = False     # дошли ли правила резолва до Grok (диагностика
                                # Starmer-бага: судил ли он по правилам или вслепую)
    rules_excerpt: str = ""     # первые ~200 симв реальных правил — чтобы разбор
                                # постфактум был фактическим, а не по флагу
    side: str = "NO"            # сторона ставки: "NO" (осн.) или "YES" (новая
                                # стратегия средней зоны, валидируется отдельно)
    ai_yes_raw: Optional[float] = None  # сырая оценка Grok ДО калибровки — по ней
                                # YES-гейт судит о согласии (см. scan_yes)

    def to_alert(self) -> Dict:
        return asdict(self)


def _is_sport_or_hft(question: str) -> bool:
    q = f" {question.lower()} "
    if _SPORT_WORD_RE.search(q) or _HFT_WORD_RE.search(q):
        return True
    if any(m in q for m in SPORT_SUBSTR_MARKERS):
        return True
    if any(m in q for m in HFT_SUBSTR_MARKERS):
        return True
    return False


def _parse_prices(market: Dict) -> Optional[tuple]:
    """Return (yes_price, no_price) for a binary Yes/No market, else None."""
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    # Gamma returns these as JSON strings sometimes.
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except Exception: return None
    if isinstance(prices, str):
        try: prices = json.loads(prices)
        except Exception: return None
    if not outcomes or not prices or len(outcomes) != 2 or len(prices) != 2:
        return None
    labels = [str(o).strip().lower() for o in outcomes]
    if set(labels) != {"yes", "no"}:
        return None  # named / multi-outcome market — no YES/NO bias to exploit
    try:
        pmap = {labels[i]: float(prices[i]) for i in range(2)}
    except Exception:
        return None
    yes, no = pmap.get("yes", 0.0), pmap.get("no", 0.0)
    if not (0 < yes < 1 and 0 < no < 1):
        return None
    return yes, no


def _hours_to_resolve(market: Dict) -> Optional[float]:
    ed = market.get("endDate") or market.get("end_date")
    if not ed:
        return None
    try:
        end = datetime.fromisoformat(str(ed).replace("Z", "+00:00"))
        return (end - datetime.now(timezone.utc)).total_seconds() / 3600
    except Exception:
        return None


def passes_gate(market: Dict) -> Optional[tuple]:
    """
    Structural gate (no AI yet). Returns (yes_price, no_price, liq, hours) if the
    market is a tradeable event-NO candidate, else None.
    """
    q = market.get("question", "") or market.get("title", "")
    if not q or _is_sport_or_hft(q):
        return None

    parsed = _parse_prices(market)
    if not parsed:
        return None
    yes_price, no_price = parsed

    # NO must be in the underpriced band.
    if not (NO_ODDS_MIN <= no_price < NO_ODDS_MAX):
        return None

    liq = float(market.get("liquidity", 0) or market.get("volume", 0) or 0)
    if liq < MIN_LIQUIDITY:
        return None

    hrs = _hours_to_resolve(market)
    if hrs is None or hrs < MIN_HOURS_TO_RESOLVE:
        return None
    if MAX_DAYS_TO_RESOLVE is not None and hrs > MAX_DAYS_TO_RESOLVE * 24:
        return None

    return yes_price, no_price, liq, hrs


def ensure_description(market: Dict, fetch_fn=None) -> str:
    """Гарантирует непустое описание (правила резолва).

    Баг 21.06: Gamma /markets в списочном режиме отдаёт description пустым, и
    Grok оценивал Starmer без правил — не видел, что ОБЪЯВЛЕНИЕ об уходе даёт
    YES. Если описание пусто, дотягиваем по conditionId (fetch_fn), мягкий
    fail-safe: при отказе возвращаем пустую строку, не роняя скан.
    """
    desc = (market.get("description") or "").strip()
    if desc:
        return desc
    if fetch_fn is None:
        return ""
    try:
        cid = market.get("conditionId") or market.get("condition_id") or ""
        return (fetch_fn(cid) or "").strip()
    except Exception:
        return ""


def _fetch_market_description(condition_id: str) -> str:
    """Полные правила резолва по conditionId из Gamma API (мягкий fail-safe).

    /markets в списочном режиме отдаёт description пустым; пробуем несколько
    известных способов запроса по одному рынку — Gamma принимает разные
    параметры в разных версиях. Берём первый непустой description.

    В CI (тестах) НЕ ходим в сеть — иначе живой Gamma делает тесты
    недетерминированными (для биткоин-рынка вернёт правила с '50-50', split-гейт
    сработает, evaluate вернёт None и мок-тесты падают). Тесты, которым нужно
    описание, передают его в самом market-дикте.
    """
    if not condition_id:
        return ""
    # Только в тестах (pytest) не ходим в сеть. НЕ по CI — GitHub ставит CI=true
    # и в боевом скане, что отключило бы дотягивание правил и split-детектор.
    import sys, os
    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return ""
    try:
        import requests
        from config import GAMMA_API_URL
        attempts = [
            {"condition_ids": condition_id},
            {"condition_id": condition_id},
            {"clob_token_ids": condition_id},
        ]
        for params in attempts:
            try:
                r = requests.get(f"{GAMMA_API_URL}/markets",
                                 params=params, timeout=15)
                if r.status_code != 200:
                    continue
                data = r.json()
                rec = None
                if isinstance(data, list) and data:
                    rec = data[0]
                elif isinstance(data, dict):
                    rec = data
                if rec:
                    desc = (rec.get("description") or "").strip()
                    if desc:
                        return desc
            except Exception:
                continue
    except Exception:
        return ""
    return ""


import re as _re_grp

# Паттерны рынков-лестниц/групповых: исход — одна из НЕСКОЛЬКИХ взаимоисключающих
# стадий/вариантов, а не бинарное да/нет. NO на таком рынке выигрывает по многим
# путям — это меняет смысл ставки, оператор должен читать правила.
_GROUPED_PATTERNS = [
    r"\bround of \d+\b",            # Round of 32/16 — конкретная стадия вылета
    r"\bstage of elimination\b",
    r"\b(quarter|semi)\s*-?\s*final",
    r"\breach the \w+final",
    r"\bmake the (quarter|semi|final)",
    r"\bwinner of\b",
    r"\bwho will win\b",
    r"\beliminated in the\b",
    r"\bgroup stage\b",
    r"\bwhich (team|player|party|candidate) (will )?win",
]
_GROUPED_RE = _re_grp.compile("|".join(_GROUPED_PATTERNS), _re_grp.IGNORECASE)


def looks_grouped(question: str) -> bool:
    """True, если вопрос похож на групповой/лестничный рынок (исход = одна из
    нескольких стадий/вариантов), а не бинарное да/нет. Такие помечаем
    suspicious: NO выигрывает по многим путям, смысл ставки меняется."""
    if not question:
        return False
    return bool(_GROUPED_RE.search(question))


_SPLIT_RES_RE = _re_grp.compile(
    r"(50\s*[-/]\s*50)|(resolve[sd]?\s+(to\s+)?a?\s*tie)|(split\s+the\s+pot)",
    _re_grp.IGNORECASE,
)


def has_split_resolution(rules: Optional[str]) -> bool:
    """True, если правила содержат пункт «резолв 50-50 / ничья при недостижении»
    (напр. 'if neither occurs by [date], resolve 50-50').

    Такой рынок структурно мёртв для edge: если сравниваемое событие за
    горизонтом резолва, исход предопределён в ничью, и цена 50¢ корректна —
    NO/YES не дают преимущества. Биткоин-кейс ('$1M before GTA VI', GTA в ноябре,
    резолв 31 июля) — ровно это. Гейтим ДО Grok, не тратим оценку и не выдаём
    как edge."""
    if not rules:
        return False
    return bool(_SPLIT_RES_RE.search(rules))


def evaluate(market: Dict, ai_estimate_fn: Callable[[str], Optional[dict]]) -> Optional[Candidate]:
    """
    Full evaluation: structural gate + AI mispricing check.
    `ai_estimate_fn(question)` returns {"prob": 0..1, "conf": str, "why": str} or None.
    Injected for testability.
    """
    gated = passes_gate(market)
    if not gated:
        return None
    yes_price, no_price, liq, _ = gated

    q = market.get("question", "") or market.get("title", "")
    description = ensure_description(market, fetch_fn=_fetch_market_description)
    end_date = market.get("endDate", "") or market.get("end_date", "") or ""

    # Гейт ДО Grok: рынок с пунктом «50-50 при недостижении» структурно мёртв
    # для edge (если событие за горизонтом резолва — гарантированная ничья, цена
    # 50¢ корректна). Биткоин-$1M-before-GTA — ровно этот случай. Не тратим
    # оценку и не выдаём как edge.
    if has_split_resolution(description):
        return None

    # Pass resolution context when the estimator accepts it; fall back to the
    # legacy single-arg signature so injected test stubs keep working.
    try:
        est = ai_estimate_fn(q, description, end_date)
    except TypeError:
        est = ai_estimate_fn(q)
    if not est or est.get("prob") is None:
        return None

    # Skip low-confidence estimates — they're too noisy to bet on.
    if _CONF_RANK.get(est.get("conf", "low"), 0) < _CONF_RANK[MIN_CONFIDENCE]:
        return None

    ai_yes_raw = float(est["prob"])
    if not (0 <= ai_yes_raw <= 1):
        return None

    # Посткалибровка: пересчитываем сырую оценку Grok по фактической таблице
    # корзин (вердикт показал систематический промах Grok). Таблица копится на
    # резолвах; где данных мало — поправка слабая (усадка к сырой оценке).
    try:
        import calibration_map as cm
        ai_yes = cm.calibrate(ai_yes_raw, cm.load_table())
    except Exception:
        ai_yes = ai_yes_raw

    edge = yes_price - ai_yes          # market overprices YES by this much
    if edge < EDGE_MIN:
        return None                    # not enough mispricing — skip

    suspicious = (
        (edge >= SUSPICIOUS_EDGE
         and SUSPICIOUS_NO_LOW <= no_price <= SUSPICIOUS_NO_HIGH)
        or looks_grouped(q)        # групповой/лестничный рынок по тексту вопроса
    )

    # reasoning carries ONLY Grok's why — presentation (numbers, warnings) is
    # the alert formatter's job. Keeping data and format separate stops the
    # alert from restating the same numbers three times.
    reasoning = est.get("why", "") or ""
    # The site opens markets at /event/<eventSlug>. Market.slug often carries a
    # numeric conditionId tail that 404s. Prefer the parent event's slug.
    ev_slug = ""
    events = market.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        ev_slug = events[0].get("slug", "") or ""
    if not ev_slug:
        ev_slug = market.get("eventSlug", "") or ""

    return Candidate(
        question=q,
        condition_id=market.get("conditionId", ""),
        market_yes_price=round(yes_price, 4),
        no_price=round(no_price, 4),
        ai_yes_estimate=round(ai_yes, 4),
        edge=round(edge, 4),
        liquidity=round(liq, 2),
        end_date=str(market.get("endDate", "")),
        reasoning=reasoning,
        slug=market.get("slug", ""),
        event_slug=ev_slug,
        band=("core" if CORE_NO_MIN <= no_price < CORE_NO_MAX else "extended"),
        suspicious=suspicious,
        ai_conf=str(est.get("conf", "low")),
        had_rules=bool(description and description.strip()),
        rules_excerpt=(description or "").strip()[:200],
    )


def _thesis_key(question: str) -> str:
    """Collapse near-identical questions (same event, different date) to one key.

    'US x Iran peace deal by August 31' and '...by October 31' share a thesis;
    betting both isn't diversification, it's one doubled position. We strip
    dates/months/years and keep the stable head of the question.
    """
    import re as _re
    q = question.lower()
    q = _re.sub(r'\b(by|before|on|until)\b.*$', '', q)          # drop "by <date>" tail
    q = _re.sub(r'\b\d{4}\b', '', q)                            # years
    q = _re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b', '', q)
    q = _re.sub(r'[^a-z0-9 ]', ' ', q)
    q = _re.sub(r'\s+', ' ', q).strip()
    return ' '.join(q.split()[:6])      # stable head


def make_two_stage_estimator(underlying, yes_price_for, screen_edge_min=0.10,
                             yes_confirm=None):
    """Двухэтапная оценка для экономии на дорогом поиске Grok ($5/1000 вызовов).

    Этап 1: дешёвый вызов БЕЗ поиска (use_search=False) — только токены.
    Если структурный edge (market_yes - cheap_est_yes) ниже screen_edge_min,
    рынок неинтересен — дорогой поиск НЕ запускаем.
    Этап 2: только на прошедших скрин — повторный вызов С поиском, его
    результат и идёт в ставку (поиск даёт актуальные факты).

    underlying(question, desc, end, use_search=bool) -> est|None
    yes_price_for(question) -> текущая YES-цена рынка (для скрин-edge)
    yes_confirm(yes_price, cheap_prob) -> bool — YES-кандидат (Grok согласен с
        рынком в YES-зоне) тоже идёт на поиск: NO-разрыв у него ~0, и без этого
        Grok судил выборы по памяти («кандидаты не определены», 04.10.2026).
    """
    def _estimator(question: str, description: str = None, end_date: str = None):
        def _call(use_search):
            try:
                return underlying(question, description, end_date, use_search=use_search)
            except TypeError:
                try:
                    return underlying(question, description, end_date)
                except TypeError:
                    return underlying(question)
        cheap = _call(False)
        if not cheap or cheap.get("prob") is None:
            return None
        yes = yes_price_for(question)
        if yes is None:
            return None
        screen_edge = float(yes) - float(cheap["prob"])
        is_yes_candidate = bool(yes_confirm and yes_confirm(float(yes), float(cheap["prob"])))
        if screen_edge < screen_edge_min and not is_yes_candidate:
            return cheap            # слабый edge — отдаём дешёвую оценку, поиск пропущен
        confirmed = _call(True)
        return confirmed or cheap
    return _estimator


def _horizon_score(edge: float, hours_to_resolve: Optional[float]) -> float:
    """Качество ступени тезиса = edge × краткосрочность.

    Один тезис на разных сроках ('Starmer by June' / 'by December') — это не
    дубли, а лестница с разным риском. Короткое окно надёжнее для NO на
    маловероятном-в-моменте событии (за 9 дней мало что случится), длинное —
    рискованнее (окно может поймать само событие). Балансируем: предпочитаем
    срок, где и переоценка хорошая, и окно узкое. Чистая краткосрочность взяла
    бы срок с нулевым edge; чистый edge — самый длинный (рискованный) срок.
    """
    if edge is None or edge <= 0:
        return 0.0
    if not hours_to_resolve or hours_to_resolve <= 0:
        return 0.0
    days = hours_to_resolve / 24.0
    # краткосрочность: 1/(1+days/30) — мягко падает с горизонтом, не обнуляя
    recency = 1.0 / (1.0 + days / 30.0)
    return edge * recency


def scan_yes(markets: List[Dict], ai_estimate_fn: Callable[[str], Optional[dict]]) -> List[Candidate]:
    """YES-стратегия средней зоны (разворот после провала NO).

    Ставим YES там, где рыночная цена YES в 50-70% И Grok согласен по
    направлению (склонён к YES). Плывём ПО рынку, не против. Отдельная выборка,
    не наследует метрики NO. Использует ту же посткалибровку оценки Grok.
    """
    import yes_strategy as ys
    out: List[Candidate] = []
    seen_thesis: Dict[str, bool] = {}
    for m in markets:
        q = m.get("question", "") or m.get("title", "")
        if not q or _is_sport_or_hft(q):
            continue
        parsed = _parse_prices(m)
        if not parsed:
            continue
        yes_price, no_price = parsed
        if not ys.yes_gate(yes_price):
            continue
        liq = float(m.get("liquidity", 0) or m.get("volume", 0) or 0)
        if liq < MIN_LIQUIDITY:
            continue
        hrs = _hours_to_resolve(m)
        if hrs is None or hrs < MIN_HOURS_TO_RESOLVE:
            continue
        k = _thesis_key(q)
        if k in seen_thesis:
            continue
        # AI-оценка (с посткалибровкой, как в NO-ветке)
        description = ensure_description(m, fetch_fn=_fetch_market_description)
        end_date = m.get("endDate", "") or m.get("end_date", "") or ""
        if has_split_resolution(description):
            continue
        try:
            est = ai_estimate_fn(q, description, end_date)
        except TypeError:
            est = ai_estimate_fn(q)
        if not est or est.get("prob") is None:
            continue
        grok_yes_raw = float(est["prob"])
        try:
            import calibration_map as cm
            grok_yes = cm.calibrate(grok_yes_raw, cm.load_table())
        except Exception:
            grok_yes = grok_yes_raw
        # Согласие по направлению — мнение САМОГО Grok, поэтому гейт и edge
        # считаются по сырой оценке. Калибровка заменяет число средним корзины,
        # а таблица немонотонна (сырые 0.0-0.4 → 0.85-0.90): по ней Grok «против»
        # выдавался за «согласие по YES» (Maine: 4% → 69%, 12 из 82 алертов до
        # 04.10.2026). Откалиброванное число остаётся в ai_yes_estimate — для
        # Brier и сопоставимости с прежними строками журнала.
        edge = ys.yes_edge(yes_price, grok_yes_raw)
        if edge is None:
            continue
        seen_thesis[k] = True
        out.append(Candidate(
            question=q,
            condition_id=m.get("conditionId", ""),
            market_yes_price=yes_price,
            no_price=no_price,
            ai_yes_estimate=round(grok_yes, 4),
            ai_yes_raw=round(grok_yes_raw, 4),
            edge=round(edge, 4),
            liquidity=liq,
            end_date=end_date,
            reasoning=str(est.get("why", "")),
            event_slug=(m.get("events", [{}]) or [{}])[0].get("slug", "") if m.get("events") else "",
            suspicious=looks_grouped(q),
            ai_conf=str(est.get("conf", "low")),
            had_rules=bool(description and description.strip()),
            rules_excerpt=(description or "").strip()[:200],
            side="YES",
        ))
    out.sort(key=lambda c: (c.suspicious, -c.edge))
    return out


def scan(markets: List[Dict], ai_estimate_fn: Callable[[str], Optional[dict]]) -> List[Candidate]:
    """Run the full pipeline, dedup theses BEFORE AI, return ranked candidates.

    Экономия: схлопываем date-варианты одного тезиса ДО вызова AI (поиск-режим
    Grok дорог — $5/1000 вызовов). Из каждого тезиса берём одного представителя
    с лучшей структурной привлекательностью (NO ближе к центру полосы — выше
    шанс реального edge), остальные в LLM не идут.
    """
    # 1. Структурный гейт (без AI) + группировка по тезису-лестнице
    gated: Dict[str, tuple] = {}        # thesis_key -> (market, no_price, score)
    for m in markets:
        g = passes_gate(m)
        if not g:
            continue
        _, no_price, _, _ = g
        q = m.get("question", "") or m.get("title", "")
        k = _thesis_key(q)
        # Представитель ступени-лестницы: edge × краткосрочность. До AI реального
        # edge нет, поэтому structural-прокси: чем дешевле NO (ниже в полосе),
        # тем больше потенциал переоценки. × краткосрочность (узкое окно надёжнее
        # для NO). Это формализует ручной выбор оператора «короткий срок лучше».
        struct_edge = max(0.0, NO_ODDS_MAX - no_price)   # дешевле NO -> больше прокси-edge
        score = _horizon_score(struct_edge, _hours_to_resolve(m))
        if k not in gated or score > gated[k][2]:
            gated[k] = (m, no_price, score)

    # 2. AI-оценка ТОЛЬКО по одному представителю на тезис
    out = []
    for m, _, _ in gated.values():
        try:
            c = evaluate(m, ai_estimate_fn)
            if c:
                out.append(c)
        except Exception:
            continue

    # Clean (non-suspicious) candidates first, then by edge.
    out.sort(key=lambda c: (c.suspicious, -c.edge))
    return out
