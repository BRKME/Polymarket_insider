"""
AI Context Layer v3 — Factual context for trade alerts.

GPT-4o-mini only (no web search — DDG blocked in GitHub Actions).
Asks for GENERAL KNOWLEDGE context, not real-time stats.

What GPT can reliably provide:
- Sports: team tier (contender vs rebuilding), conference, general strength
- Politics: candidate background, party dynamics
- Crypto: project description, market cap tier
- Geopolitics: situation background, key actors

What it CANNOT provide (and shouldn't try):
- Today's injury report, exact standings, live scores
- Current poll numbers, exact vote counts
- Real-time prices

Cost: ~$0.002 per call. Only for alerts passing all filters.
"""

import re
import logging
from typing import Optional

from openai import OpenAI
from config import OPENAI_API_KEY

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

SYSTEM = """You are an independent analyst for a prediction market copy-trading bot.
A top trader just placed a bet. You must give a binary recommendation: COPY or SKIP.

IMPORTANT:
- The odds shown (e.g. "Thunder @ 69%") mean the trader PAID 69¢ per share of Thunder.
  If Thunder wins, each share pays $1. So 69% = the market price for that outcome.
- Do NOT confuse sides. If "Magic @ 81%" — Magic IS the favorite at 81%, not the underdog.
- Your job: decide if copying this specific bet is smart.

Rules:
- Start with ✅ COPY or ❌ SKIP
- Then give 1-2 specific reasons in max 25 words
- Consider: are the odds fair? Is this team/outcome likely? Is the market efficient?
- Do NOT invent stats, W-L records, scores, or injury reports
- If you don't know enough about the teams/topic, reply: NO_DATA"""

PROMPTS = {
    "sports": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if {outcome} wins)
Bet size: ${amount:,.0f}

Should I copy this bet? Consider team strength, home/away, conference tier.""",

    "politics": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Should I copy this bet? Consider political landscape, incumbent dynamics, precedent.""",

    "crypto": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Should I copy this bet? Consider project fundamentals, market conditions, volatility.""",

    "geopolitics": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Should I copy this bet? Consider diplomatic trajectory, escalation risk, historical precedent.""",

    "other": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Should I copy this bet?""",
}


# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════

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
    if not market_title or not OPENAI_API_KEY:
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
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=80,
            temperature=0.3,
        )

        text = response.choices[0].message.content.strip()
        text = text.strip('"').strip("'").strip()

        if not text or "NO_DATA" in text or len(text) < 8:
            logger.info(f"  AI context: NO_DATA for '{market_title[:50]}'")
            return None

        if len(text) > 150:
            text = text[:147] + "..."

        logger.info(f"  AI [{market_type}]: {text[:80]}")
        return text

    except Exception as e:
        logger.warning(f"AI context failed: {e}")
        return None
