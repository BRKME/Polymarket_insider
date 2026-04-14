"""
AI Context Layer v4 — Web-search-powered trade analysis.

Uses gpt-4o-mini-search-preview: GPT searches the web before answering.
For sports: checks current form, standings, injuries.
For politics: checks latest polls, news.

Cost: ~$0.025 per search call + token costs. Only for alerts passing all filters.
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
- You have web search — USE IT to check current form, standings, recent results, injuries.

FORMAT RULES (STRICT):
- PLAIN TEXT ONLY. No markdown, no headers, no links, no bullet points.
- Start with ✅ COPY or ❌ SKIP on the first line
- Then 1-2 sentences with specific factual reasons (max 40 words)
- For sports: mention recent W-L record, standings position, or key injuries
- If search returns nothing useful, reply: NO_DATA"""

PROMPTS = {
    "sports": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if {outcome} wins)
Bet size: ${amount:,.0f}

Search for current form, recent results, standings, and injuries for both teams. Then decide: COPY or SKIP?""",

    "politics": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for latest polls, news developments, and expert analysis. Then decide: COPY or SKIP?""",

    "crypto": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for recent price action, news, and market sentiment. Then decide: COPY or SKIP?""",

    "geopolitics": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for latest diplomatic developments, news, and expert analysis. Then decide: COPY or SKIP?""",

    "other": """Market: "{title}"
Trader bet: {outcome} at {odds:.0f}% (paid {odds:.0f}¢ per share, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for any relevant recent information. Then decide: COPY or SKIP?""",
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
            model="gpt-4o-mini-search-preview",
            web_search_options={
                "search_context_size": "low",  # minimize cost
            },
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
        )

        text = response.choices[0].message.content.strip()
        text = text.strip('"').strip("'").strip()
        
        # Clean markdown and URL junk from search model output
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)  # [text](url) → text
        text = re.sub(r'#{1,3}\s*', '', text)                   # ## headers → plain
        text = re.sub(r'https?://\S+', '', text)                # raw URLs
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)          # **bold** → plain
        text = re.sub(r'\n{2,}', '\n', text)                    # multi newlines
        text = re.sub(r'^\s*[-•]\s*', '', text, flags=re.MULTILINE)  # bullet points
        text = text.strip()

        if not text or "NO_DATA" in text or len(text) < 8:
            logger.info(f"  AI context: NO_DATA for '{market_title[:50]}'")
            return None

        if len(text) > 250:
            text = text[:247] + "..."

        logger.info(f"  AI [{market_type}]: {text[:80]}")
        return text

    except Exception as e:
        logger.warning(f"AI context failed: {e}")
        return None
