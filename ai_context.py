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
A top trader or suspected insider just placed a bet. You must give a binary recommendation: COPY or SKIP.

CRITICAL CONTEXT:
- This person is betting because they believe they have an edge (insider info, sharp analysis, or proven track record).
- Your job is NOT to judge if the odds look right. Your job is to CHECK if real-world facts SUPPORT the trader's bet.
- If the facts support the bet → COPY. If facts contradict the bet → SKIP.

EXAMPLE:
- Trader bets Oh My God @ 18% (underdog). You search and find Oh My God has 75% h2h win rate vs opponent.
  → Facts SUPPORT the bet → ✅ COPY
- Trader bets Team X @ 60%. You search and find Team X lost 8 of last 10.
  → Facts CONTRADICT the bet → ❌ SKIP

IMPORTANT:
- The odds shown (e.g. "@ 48%") mean the trader PAID 48¢ per share. If that team wins, share pays $1.
- Low odds (10-30%) = underdog bet. This is OFTEN the smart money play. Don't skip just because odds are low.
- You have web search — USE IT to check current form, standings, recent results, injuries, polls.
- NEVER say "no information available". Always search for team/player form, standings, recent W-L record.
  Even if this specific match has no coverage, the teams have recent history you can find.
- If you truly find zero relevant information after searching, reply: NO_DATA

FORMAT (STRICT):
- PLAIN TEXT ONLY. No markdown, no headers, no links, no bullet points.
- Line 1: ✅ COPY or ❌ SKIP — followed by ONE sentence explaining WHY (the key reason)
- Then 1-2 sentences with supporting facts, stats, sources
- Cite source in parentheses if found
- End with a clear conclusion — do NOT leave sentences unfinished
- If search returns nothing useful, reply: NO_DATA

EXAMPLE FORMAT:
✅ COPY — Medjedovic has beaten two seeded players this week and is in strong form.
He defeated Borges 7-6, 6-2 and de Miñaur 6-4, 6-3 to reach the semifinals. (cadenaser.com) Rublev has struggled on clay this season with a 3-4 record. Value at 65%."""

PROMPTS = {
    "sports": """Market: "{title}"
The trader is betting on: {outcome} at {odds:.0f}% odds (paid {odds:.0f}¢, wins $1 if {outcome} wins)
Bet size: ${amount:,.0f}

Search for {outcome}'s recent form, W-L record, h2h vs opponent, and injuries. Do the facts support this bet on {outcome}?""",

    "politics": """Market: "{title}"
The trader is betting on: {outcome} at {odds:.0f}% odds (paid {odds:.0f}¢, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for latest polls, news, and expert analysis. Do the facts support betting on {outcome}?""",

    "crypto": """Market: "{title}"
The trader is betting on: {outcome} at {odds:.0f}% odds (paid {odds:.0f}¢, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for recent price action, news, and sentiment. Do the facts support betting on {outcome}?""",

    "geopolitics": """Market: "{title}"
The trader is betting on: {outcome} at {odds:.0f}% odds (paid {odds:.0f}¢, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for latest diplomatic developments and expert analysis. Do the facts support betting on {outcome}?""",

    "other": """Market: "{title}"
The trader is betting on: {outcome} at {odds:.0f}% odds (paid {odds:.0f}¢, wins $1 if correct)
Bet size: ${amount:,.0f}

Search for any relevant recent information. Do the facts support betting on {outcome}?""",
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
            max_tokens=350,
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

        if len(text) > 500:
            # Cut at last sentence boundary
            cut = text[:500].rfind('.')
            if cut > 200:
                text = text[:cut+1]
            else:
                text = text[:497] + "..."

        logger.info(f"  AI [{market_type}]: {text[:80]}")
        return text

    except Exception as e:
        logger.warning(f"AI context failed: {e}")
        return None
