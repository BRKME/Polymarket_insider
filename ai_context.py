"""
AI Context Layer v6 — Grok only (GPT removed).

Grok: grok-4.3 (xAI, X Search + Web Search)
GPT removed: 33% WR, contradicts own data, hallucinations.

Grok searches Twitter + Web before answering = real-time facts.
"""

import os
import re
import logging
from typing import Optional
from datetime import datetime

XAI_API_KEY = os.getenv("XAI_API_KEY", "")

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# MARKET TYPE DETECTION
# ══════════════════════════════════════════════════════════

def detect_market_type(title: str) -> str:
    t = title.lower()
    sports = [
        'nba', 'nfl', 'mlb', 'nhl', 'wnba', 'ncaa', 'epl', 'mls',
        'euroleague', 'ufc', 'tennis', 'golf', ' vs ', ' vs.',
        'champions league', 'la liga', 'serie a', 'bundesliga',
        'premier league', 'world cup', 'cricket',
    ]
    politics = [
        'president', 'election', 'vote', 'senate', 'congress',
        'governor', 'prime minister', 'parliament', 'party',
        'nomination', 'impeach', 'minister',
    ]
    crypto = [
        'bitcoin', 'ethereum', 'btc', 'eth', 'solana', 'crypto',
        'token', 'defi', 'nft', 'fdv', 'airdrop',
    ]
    geo = [
        'war', 'strike', 'invasion', 'ceasefire', 'sanctions',
        'tariff', 'iran', 'russia', 'ukraine', 'china', 'taiwan',
        'nato', 'military',
    ]
    if any(kw in t for kw in sports):
        return "sports"
    if any(kw in t for kw in politics):
        return "politics"
    if any(kw in t for kw in crypto):
        return "crypto"
    if any(kw in t for kw in geo):
        return "geopolitics"
    return "other"


# ══════════════════════════════════════════════════════════
# TYPE-SPECIFIC PROMPTS
# ══════════════════════════════════════════════════════════

SYSTEM = """Ты независимый спортивный/новостной аналитик.
Топ-трейдер сделал ставку. Дай ОБЪЕКТИВНЫЙ анализ рынка.

ВАЖНО: Ты анализируешь РЫНОК, а не подтверждаешь ставку.
Не ищи подтверждение позиции трейдера. Ищи ПРАВДУ.

АБСОЛЮТНЫЙ ЗАПРЕТ:
- НИКОГДА не выдумывай статистику, счета матчей или рекорды
- Если не нашёл конкретный факт — НЕ ПИШИ его
- Каждый факт должен быть найден через поиск

ПРАВИЛА:
1. Анализируй ОБЕИХ участников (не только того на кого ставка)
2. H2h между ними — ключевой фактор
3. Для Over/Under: реальные счета последних игр
4. Факты ЗА ставку трейдера → COPY
5. Факты ПРОТИВ ставки → SKIP
6. NO_DATA — только если событие реально неизвестно
7. Для NBA, MLB, NFL, NHL, EPL, теннис — данные ВСЕГДА есть
8. При сомнениях → SKIP

ЯЗЫК: ТОЛЬКО РУССКИЙ.

ФОРМАТ (СТРОГО):
Строка 1: ✅ COPY или ❌ SKIP
Строка 2: • первый ключевой факт (конкретно, с цифрами)
Строка 3: • второй ключевой факт (конкретно, с цифрами)

ПРИМЕР:
✅ COPY
• Brann выиграли 3 из 5, Sarpsborg проиграли 4 из 5 выездных
• H2h: Brann ведут 4-1 в последних 5 домашних встречах

НЕ ПИШИ длинные предложения. Только буллиты. Максимум 2 строки после вердикта."""

PROMPTS = {
    "sports": """Рынок: "{title}"
Трейдер ставит на: {outcome} по {odds:.0f}%
Сумма: ${amount:,.0f}

Дай ОБЪЕКТИВНЫЙ анализ. Кто фаворит? Найди:
1. Последние 5-10 матчей ОБЕИХ команд/игроков
2. H2h между ними (последние 3-5 встреч)
3. Травмы ключевых игроков
Для Over/Under: счёт последних 3 игр между этими командами.
Потом сравни с позицией трейдера — поддерживают ли факты ЕГО ставку.""",

    "politics": """Рынок: "{title}"
Трейдер ставит на: {outcome} по {odds:.0f}%
Сумма: ${amount:,.0f}

Дай ОБЪЕКТИВНЫЙ анализ. Найди последние опросы, новости, экспертные оценки.
Что говорят факты? Потом сравни с позицией трейдера.""",

    "crypto": """Рынок: "{title}"
Трейдер ставит на: {outcome} по {odds:.0f}%
Сумма: ${amount:,.0f}

Дай ОБЪЕКТИВНЫЙ анализ. Найди последние движения цены, новости, настроения рынка.
Что говорят факты? Потом сравни с позицией трейдера.""",

    "geopolitics": """Рынок: "{title}"
Трейдер ставит на: {outcome} по {odds:.0f}%
Сумма: ${amount:,.0f}

Дай ОБЪЕКТИВНЫЙ анализ. Найди последние дипломатические события, новости, экспертный анализ.
Что говорят факты? Потом сравни с позицией трейдера.""",

    "other": """Рынок: "{title}"
Трейдер ставит на: {outcome} по {odds:.0f}%
Сумма: ${amount:,.0f}

Дай ОБЪЕКТИВНЫЙ анализ. Найди релевантную информацию.
Что говорят факты? Потом сравни с позицией трейдера.""",
}


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

def _clean_ai_response(text: str) -> str:
    """Clean markdown, URLs, and junk from AI response."""
    text = text.strip('"').strip("'").strip()
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)  # [text](url) → text
    # Broken citation footnotes Grok leaves behind: "[1](", "[[2]](", "[3]"
    text = re.sub(r'\[+\d+\]+\(+', '', text)                # [1](  [[2]](  → removed
    text = re.sub(r'\[+\d+\]+', '', text)                   # [1]  [[2]]    → removed
    text = re.sub(r'#{1,3}\s*', '', text)                   # ## headers → plain
    text = re.sub(r'https?://\S+', '', text)                # raw URLs
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)          # **bold** → plain
    # Facts often arrive glued: ")Teichmann" or ". Earlier" with no break.
    # Put each sentence-fact on its own bullet line for readability.
    text = re.sub(r'\)\s*(?=[A-ZА-Я])', ')\n', text)        # ...6-2)Teichmann → newline
    text = re.sub(r'\.\s+(?=[A-ZА-Я])', '.\n', text)        # end-of-fact → newline
    text = re.sub(r'\s+([.,;])', r'\1', text)               # drop space before punct
    text = re.sub(r'\n{2,}', '\n', text)                    # multi newlines
    text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.MULTILINE)  # strip existing bullets
    # Re-add a clean bullet to each non-empty line
    lines = [l.strip().rstrip('.') for l in text.split('\n') if l.strip()]
    return '\n'.join(f"• {l}" for l in lines)


def generate_trade_context(
    market_title: str,
    outcome: str,
    odds_pct: float,
    trader_rank: int = 0,
    amount: float = 0,
) -> Optional[str]:
    """
    Generate binary COPY/SKIP recommendation.
    Returns None on error or if GPT has no useful context.
    """
    if not market_title or not XAI_API_KEY:
        return None

    market_type = detect_market_type(market_title)

    prompt_template = PROMPTS.get(market_type, PROMPTS["other"])
    prompt = prompt_template.format(
        title=market_title,
        outcome=outcome,
        odds=odds_pct,
        amount=amount,
    )

    try:
        results = {}
        
        # === Call Grok with X Search + Web Search ===
        if XAI_API_KEY:
            try:
                import requests as req
                grok_payload = {
                    "model": "grok-4.3",
                    "input": [
                        {"role": "system", "content": SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    "tools": [
                        {"type": "x_search"},
                        {"type": "web_search"},
                    ],
                }
                print(f"  🔍 Grok: calling grok-4.3 with x_search+web_search...")
                grok_response = req.post(
                    "https://api.x.ai/v1/responses",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {XAI_API_KEY}",
                    },
                    json=grok_payload,
                    timeout=60,  # Search needs more time
                )
                
                if grok_response.status_code == 200:
                    grok_data = grok_response.json()
                    # Debug: show response structure
                    output = grok_data.get("output", [])
                    output_types = [b.get("type") for b in output]
                    print(f"  🔍 Grok response: {len(output)} blocks, types={output_types}")
                    
                    # Extract text from ALL possible block types
                    grok_text_parts = []
                    for block in output:
                        btype = block.get("type")
                        if btype == "message":
                            for content in block.get("content", []):
                                ctype = content.get("type")
                                if ctype in ("output_text", "text"):
                                    grok_text_parts.append(content.get("text", ""))
                        elif btype == "output_text":
                            grok_text_parts.append(block.get("text", ""))
                        elif btype == "text":
                            grok_text_parts.append(block.get("text", ""))
                    
                    grok_text = _clean_ai_response("\n".join(grok_text_parts).strip())
                    
                    if not grok_text:
                        # Fallback: try output_text at top level
                        grok_text = _clean_ai_response(grok_data.get("output_text", ""))
                    
                    if grok_text and len(grok_text) >= 8:
                        results["Grok"] = grok_text
                        print(f"  ✅ Grok: {grok_text[:60]}")
                    else:
                        print(f"  ⚠️ Grok empty after parsing. Raw output keys: {list(grok_data.keys())}")
                        if output:
                            print(f"  ⚠️ First block: {str(output[0])[:200]}")
                else:
                    print(f"  ❌ Grok HTTP {grok_response.status_code}: {grok_response.text[:200]}")
            except Exception as e:
                print(f"  ❌ Grok exception: {e}")
        else:
            print(f"  ⚠️ XAI_API_KEY not set — Grok skipped")
        
        if not results:
            return None
        
        # === Determine consensus ===
        verdicts = {}
        for model, text in results.items():
            upper = text.upper()
            lower = text.lower()
            if "NO_DATA" in upper or "НЕТ ДАННЫХ" in upper:
                verdicts[model] = "NO_DATA"
            elif "данных нет" in lower or "не найдено" in lower:
                verdicts[model] = "NO_DATA"
            elif "LEAN COPY" in upper:
                verdicts[model] = "COPY"
            elif "COPY" in upper and "SKIP" not in upper:
                verdicts[model] = "COPY"
            elif "SKIP" in upper:
                verdicts[model] = "SKIP"
            else:
                verdicts[model] = "UNCLEAR"
        
        # Consensus tag
        real_verdicts = [v for v in verdicts.values() if v in ("COPY", "SKIP")]
        if len(real_verdicts) == 2 and real_verdicts[0] == real_verdicts[1]:
            consensus = real_verdicts[0]
        elif len(real_verdicts) == 1:
            consensus = real_verdicts[0]
        elif len(real_verdicts) == 2:
            consensus = "SPLIT"
        else:
            consensus = "NO_DATA"
        
        # Build combined response
        parts = []
        for model, text in results.items():
            # Truncate each model to ~200 chars
            if len(text) > 200:
                cut = text[:200].rfind('.')
                text = text[:cut+1] if cut > 80 else text[:197] + "..."
            parts.append(f"[{model}] {text}")
        
        combined = "\n".join(parts)
        
        # Add consensus header
        combined = f"[CONSENSUS:{consensus}] {combined}"
        
        logger.info(f"  AI dual [{market_type}]: GPT={verdicts.get('GPT','?')} Grok={verdicts.get('Grok','?')} → {consensus}")
        return combined

    except Exception as e:
        logger.warning(f"AI context failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════
# v5 — Probability estimator for the event-market scanner.
# Asks Grok for an INDEPENDENT P(YES) on a binary event market, with web/X
# search for fresh facts. Used to detect mispricing vs the market price.
# Model-agnostic by design: swap the transport here to change provider.
# ════════════════════════════════════════════════════════════════════════

ESTIMATOR_SYSTEM = """Ты независимый прогнозист-аналитик событийных рынков.
Тебе дают вопрос бинарного рынка (исход YES/NO) и просят оценить ИСТИННУЮ
вероятность исхода YES в процентах (0-100).

КАК РАССУЖДАТЬ (обязательный порядок):
1. Найди свежие факты через web_search/x_search. Не выдумывай.
2. Выпиши факты ЗА исход YES и факты ПРОТИВ. Кратко.
3. Только потом назови число — оно должно быть СЛЕДСТВИЕМ фактов.

ГЛАВНОЕ ПРАВИЛО КАЛИБРОВКИ:
Твоя оценка обязана соответствовать твоим же фактам.
- Если факты ясно указывают, что событие ПРОИЗОЙДЁТ (кандидат уверенно
  лидирует, дедлайн близко и всё идёт к да, тренд сильный) — ставь ВЫСОКУЮ
  вероятность (80-95%). НЕ занижай искусственно.
- Если факты указывают, что НЕ произойдёт — ставь НИЗКУЮ (5-20%).
- Среднюю зону (40-60%) используй ТОЛЬКО при реальной неопределённости, когда
  факты противоречивы или их мало.
ЗАПРЕЩЕНО давать число, противоречащее собственным фактам. Если все твои факты
за YES, а ты ставишь 68% — это ошибка. Перепроверь и исправь.

База vs факты: начни с базовой ставки для класса события, но двигай оценку под
свежие конкретные факты В ПОЛНУЮ СИЛУ. Конкретные данные (опросы, результаты
первого тура, объявленные решения) важнее общей базовой ставки.

Не привязывайся к рыночной цене — её тебе не дают. Оценивай независимо.
Если фактов реально мало — скажи об этом и ставь CONF: low.

ФОРМАТ ОТВЕТА (строго):
FACTS_FOR: <факты за YES, или "нет">
FACTS_AGAINST: <факты против YES, или "нет">
PROB: <число 0-100, согласованное с фактами выше>
CONF: <low|medium|high>
WHY: <1-2 коротких факта — главное обоснование>
"""


def _build_estimator_prompt(question: str, description: str = None,
                            end_date: str = None) -> str:
    """Assemble the estimator user-prompt, injecting resolution context.

    The question title alone hides the traps: grouped/linked markets, exact
    resolution criteria, and the source of truth all live in the description.
    Feeding them in lets Grok judge "P(YES) BY the resolution date UNDER these
    rules" instead of guessing from a headline.
    """
    parts = [f"Вопрос рынка: «{question}»"]
    if description and str(description).strip():
        desc = str(description).strip()
        if len(desc) > 2500:        # правила бывают длинными и критичными
            desc = desc[:2500] + "…"
        parts.append(
            f"\nПРАВИЛА РЕЗОЛВА (читай ПОЛНОСТЬЮ — тут ловушки):\n{desc}\n"
            f"⚠️ Особое внимание edge-условиям: рынок может резолвиться в YES не "
            f"только по основному событию, но и по триггеру вроде «объявление/"
            f"анонс/announcement о событии резолвит YES независимо от даты "
            f"вступления в силу», по групповым/связанным исходам. Оцени P(YES) "
            f"с учётом ВСЕХ путей к YES, а не только очевидного.")
    else:
        parts.append("\n⚠️ ПРАВИЛА РЕЗОЛВА НЕ ПОЛУЧЕНЫ — оценка по заголовку "
                     "ненадёжна. Снизь уверенность (conf не выше medium) и "
                     "учти, что реальное условие резолва может быть шире "
                     "заголовка (триггеры-объявления, групповые исходы).")
    if end_date and str(end_date).strip():
        parts.append(f"\nДата резолва: {str(end_date)[:10]}. Оцени вероятность "
                     f"YES именно К ЭТОЙ ДАТЕ и по правилам выше.")
    parts.append("\nСначала найди свежие факты, потом дай число. "
                 "WHY пиши по-русски, одной фразой, без маркеров списка. "
                 "Ответ строго в требуемом формате.")
    return "\n".join(parts)


# Журнал расхода: одна строка на каждый реальный вызов estimator'а. Без него
# не видно, сколько стоит скан (а счёт xAI общий с другими проектами).
USAGE_LOG = os.getenv("GROK_USAGE_LOG", "grok_usage.jsonl")

# Предохранитель: когда xAI отвечает «кончились кредиты / лимит трат», все
# следующие вызовы в этом запуске заведомо упадут так же — не долбим API.
_BILLING_BLOCKED = False


def _is_billing_block(status_code: int, body: str) -> bool:
    if status_code not in (402, 403):
        return False
    low = (body or "").lower()
    return "credits" in low or "spending limit" in low


def _log_usage(use_search: bool, status_code: int, data: Optional[dict]) -> None:
    try:
        import json
        row = {
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "call": "estimator_search" if use_search else "estimator_screen",
            "status": status_code,
            "usage": (data or {}).get("usage"),
        }
        with open(USAGE_LOG, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  ⚠️ usage log failed: {e}")


def estimate_probability(question: str, description: str = None,
                         end_date: str = None,
                         use_search: bool = True) -> Optional[dict]:
    """
    Estimate independent P(YES) for a binary event-market question.

    `description` and `end_date` are optional resolution context — when present
    they're injected into the prompt so Grok sees the resolution rules (grouped
    markets, exact criteria) rather than inferring from the title.

    use_search: при False — БЕЗ x_search/web_search (только токены, дёшево) для
    первичного скрина; при True — с поиском (дорого, $5/1000 вызовов) для
    финального подтверждения. Двухэтапная схема режет дорогие поиск-вызовы.

    Returns {"prob": float 0..1, "conf": str, "why": str} or None on failure.
    Confidence is surfaced so the scanner can require 'medium'+ before trading.
    """
    global _BILLING_BLOCKED
    if not question or not XAI_API_KEY or _BILLING_BLOCKED:
        return None

    prompt = _build_estimator_prompt(question, description, end_date)

    try:
        import requests as req
        payload = {
            "model": "grok-4.3",
            "input": [
                {"role": "system", "content": ESTIMATOR_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        if use_search:
            payload["tools"] = [{"type": "x_search"}, {"type": "web_search"}]
        resp = req.post(
            "https://api.x.ai/v1/responses",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {XAI_API_KEY}",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            print(f"  ❌ estimator HTTP {resp.status_code}: {resp.text[:160]}")
            if _is_billing_block(resp.status_code, resp.text):
                _BILLING_BLOCKED = True
                print("  ⛔ xAI: кредиты/лимит исчерпаны — остальные оценки "
                      "в этом запуске пропускаем")
            return None

        data = resp.json()
        _log_usage(use_search, resp.status_code, data)
        parts = []
        for block in data.get("output", []):
            bt = block.get("type")
            if bt == "message":
                for c in block.get("content", []):
                    if c.get("type") in ("output_text", "text"):
                        parts.append(c.get("text", ""))
            elif bt in ("output_text", "text"):
                parts.append(block.get("text", ""))
        text = "\n".join(parts).strip() or data.get("output_text", "")
        if not text:
            return None
        return _parse_probability(text)
    except Exception as e:
        print(f"  ❌ estimator error: {e}")
        return None


def _parse_probability(text: str) -> Optional[dict]:
    """Parse 'PROB: 45 / CONF: medium / WHY: ...' from estimator output."""
    prob_m = re.search(r'PROB:\s*([0-9]{1,3}(?:\.[0-9]+)?)', text, re.IGNORECASE)
    if not prob_m:
        # Fallback: first standalone percentage in the text.
        prob_m = re.search(r'\b([0-9]{1,3})\s*%', text)
    if not prob_m:
        return None
    prob = float(prob_m.group(1))
    if prob > 1:
        prob = prob / 100.0
    prob = max(0.0, min(1.0, prob))

    conf_m = re.search(r'CONF:\s*(low|medium|high)', text, re.IGNORECASE)
    conf = conf_m.group(1).lower() if conf_m else "low"

    why_m = re.search(r'WHY:\s*(.+)', text, re.IGNORECASE | re.DOTALL)
    why = _clean_ai_response(why_m.group(1).strip()) if why_m else ""

    return {"prob": round(prob, 4), "conf": conf, "why": why}
