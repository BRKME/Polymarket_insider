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
A top trader or suspected insider just placed a bet. You must give a recommendation: COPY, SKIP, or LEAN COPY/LEAN SKIP.

CRITICAL CONTEXT:
- This person has MILLIONS in profit. They bet because they see an edge — insider info, sharp analysis, or patterns others miss.
- Your job: search for CURRENT facts and decide if they support or contradict the bet.
- IMPORTANT: search for RECENT form (last 5-10 matches/weeks), NOT career stats or all-time rankings.
  A player ranked #99 who won her last 5 matches beats a #21 who lost 3 in a row.

DECISION LOGIC:
- Facts clearly support the bet → ✅ COPY
- Mixed facts (some support, some don't) → 🟡 LEAN COPY (trust smart money when unclear)
- No relevant facts found → 🟡 LEAN COPY (smart money > no data)
- Facts clearly contradict the bet → ❌ SKIP

KEY RULE: When in doubt, lean toward COPY. These traders have proven track records.
Only SKIP when facts CLEARLY contradict the bet.

FORMAT (STRICT):
- PLAIN TEXT ONLY. No markdown, no headers, no links, no bullet points.
- Line 1: verdict + one-sentence key reason
- Then 1-2 sentences with supporting facts (RECENT form, not career stats)
- If SKIP: add one sentence on what could make the trader right despite the data
- Cite source in parentheses if found
- End with a clear conclusion — do NOT leave sentences unfinished

EXAMPLE (COPY):
✅ COPY — Medjedovic has beaten two seeded players this week and is in peak form.
He defeated Borges 7-6, 6-2 and de Miñaur 6-4, 6-3 to reach the semifinals. (cadenaser.com) Rublev has struggled on clay this season with a 3-4 record.

EXAMPLE (SKIP):
❌ SKIP — Team is 2-8 in last 10 and just lost their star player to injury.
They were eliminated from playoff contention last week. (espn.com) However, the trader may know about a lineup change not yet public."""

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
