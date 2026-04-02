"""
AI Context Layer — One-line smart context for trade alerts.

Uses GPT-4o-mini to add actionable context:
- Sports: recent form, H2H, injuries, standings
- Politics: polls, recent developments
- Crypto: on-chain data, sentiment shift
- Other: key facts that explain why this bet is interesting

Cost: ~$0.002 per call. Only called for alerts that pass all filters.
"""

import logging
from typing import Optional
from openai import OpenAI
from config import OPENAI_API_KEY

logger = logging.getLogger(__name__)


def generate_trade_context(
    market_title: str,
    outcome: str,
    odds_pct: float,
    trader_rank: int = 0,
    amount: float = 0,
) -> Optional[str]:
    """
    Generate one-line context for a trade alert.
    
    Returns a short string like:
      "Milano 3rd in Euroleague, Paris 12th. Milano won last 4 H2H."
    
    Returns None on any error (caller shows alert without context).
    """
    if not OPENAI_API_KEY:
        return None

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = f"""You are a sports/prediction market analyst. 
A top-{trader_rank} trader just bet ${amount:,.0f} on "{outcome}" at {odds_pct:.0f}% in this market:

"{market_title}"

Give me ONE line (max 20 words) of factual context that helps evaluate this bet.
Focus on: recent form, standings, H2H record, key injuries, or polls — whatever is most relevant.
If you don't have reliable data, say "No recent data available".

Do NOT give opinions like "good bet" or "risky". Just facts.
Do NOT repeat the market title or the trader's position.

One line:"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=60,
            temperature=0.3,
        )

        text = response.choices[0].message.content.strip()
        # Clean up
        text = text.strip('"').strip("'").strip("—").strip("-").strip()
        
        if not text or len(text) < 5:
            return None
        
        # Truncate if too long
        if len(text) > 120:
            text = text[:117] + "..."

        return text

    except Exception as e:
        logger.warning(f"AI context failed: {e}")
        return None
