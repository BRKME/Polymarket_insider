# Polymarket Insider — Telegram Notifier
# Formats and sends insider/top-trader alerts

# Debug flag — set to False in production to reduce log noise
DEBUG_CALCULATIONS = False

import requests
import re
from openai import OpenAI
import openai
import trade_economics
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY
from typing import Dict, Optional
from functools import lru_cache
import hashlib


def extract_market_subject(market_title: str) -> Optional[str]:
    """
    Extract subject from market title for clearer YES/NO display.
    
    Examples:
    - "Will FC Barcelona win on 2026-03-22?" → "FC Barcelona"
    - "Will Trump win the election?" → "Trump"
    - "Will Bitcoin reach $100K?" → "Bitcoin reach $100K"
    - "Lakers vs Celtics" → None (not a Will X? format)
    - "Will there be no change in Fed rates?" → None (filler word)
    """
    if not market_title:
        return None
    
    # Filler words that aren't useful as subjects
    FILLER_SUBJECTS = {
        'there', 'it', 'this', 'that', 'the', 'a', 'an', 'any',
        'someone', 'anyone', 'everybody', 'nobody',
    }
    
    # Pattern: "Will X win/happen/reach/etc?"
    patterns = [
        r'^Will\s+(.+?)\s+win\b',  # "Will X win..."
        r'^Will\s+(.+?)\s+be\s+',   # "Will X be..."
        r'^Will\s+(.+?)\s+reach\b', # "Will X reach..."
        r'^Will\s+(.+?)\s+pass\b',  # "Will X pass..."
        r'^Will\s+(.+?)\s+happen',  # "Will X happen..."
        r'^Will\s+(.+?)\?',         # Generic "Will X?"
    ]
    
    for pattern in patterns:
        match = re.search(pattern, market_title, re.IGNORECASE)
        if match:
            subject = match.group(1).strip()
            # Clean up trailing words like "on 2026-03-22"
            subject = re.sub(r'\s+on\s+\d{4}-\d{2}-\d{2}.*$', '', subject)
            subject = re.sub(r'\s+in\s+\d{4}.*$', '', subject)
            
            if not subject:
                continue
            # Reject filler words (exact or leading)
            first_word = subject.split()[0].lower() if subject.split() else ''
            if subject.lower() in FILLER_SUBJECTS or first_word in FILLER_SUBJECTS:
                continue
            # Cap length — long subjects make UI unreadable
            if len(subject) > 35:
                continue
            return subject
    
    return None


def extract_ou_line(market_title: str) -> Optional[str]:
    """
    Extract O/U line number from market title.
    
    Examples:
    - "Texas Tech vs Alabama: O/U 165.5" → "165.5"
    - "Lakers vs Celtics O/U 220" → "220"
    """
    if not market_title:
        return None
    
    # Pattern: O/U followed by number
    match = re.search(r'O/U\s*([\d.]+)', market_title, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


def _is_second_in_vs_title(outcome_name: str, market_title: str) -> bool:
    """
    Detect if outcome_name is the SECOND participant in an 'X vs Y' title.
    
    Examples:
    - outcome="Jennifer Brady", title="Tomova vs Jennifer Brady" → True
    - outcome="Tomova", title="Tomova vs Jennifer Brady" → False
    - outcome="Lakers", title="Lakers vs Celtics" → False
    - outcome="Celtics", title="Lakers vs Celtics" → True
    """
    if not outcome_name or not market_title:
        return False
    
    # Find "vs" or "vs." split
    vs_match = re.search(r'\bvs\.?\s+', market_title, re.IGNORECASE)
    if not vs_match:
        return False
    
    after_vs = market_title[vs_match.end():].strip()
    before_vs = market_title[:vs_match.start()].strip()
    
    outcome_lower = outcome_name.lower().strip()
    
    # Check if outcome matches second side (after "vs")
    # Use substring match — outcome might be shortened or extended
    if outcome_lower in after_vs.lower():
        return True
    
    # Check last word match (e.g., "Brady" in "Jennifer Brady")
    outcome_last = outcome_lower.split()[-1] if outcome_lower.split() else ""
    after_vs_lower = after_vs.lower()
    if outcome_last and outcome_last in after_vs_lower and outcome_last not in before_vs.lower():
        return True
    
    return False


def determine_position(trade_data, odds):
    """
    Determine position from trade data.
    Returns outcome name (Over/Under, team name, YES/NO).
    """
    if trade_data:
        # Use 'outcome' field - contains the actual position
        # Examples: "Yes", "No", "Over", "Under", "Lakers", etc.
        outcome = trade_data.get('outcome')
        if outcome:
            outcome_str = str(outcome)
            outcome_lower = outcome_str.lower()
            
            # Normalize common values
            if outcome_lower == 'yes':
                return 'YES'
            if outcome_lower == 'no':
                return 'NO'
            
            # Return as-is for Over/Under, team names, etc.
            return outcome_str
    
    # Fallback based on price
    return '~YES' if odds > 0.5 else '~NO'


def is_binary_market(position: str) -> bool:
    """Check if position is binary (YES/NO) vs named (Over/Under, team/player)."""
    pos_clean = position.lstrip('~').upper()
    return pos_clean in ['YES', 'NO']


def format_trade_info(alert):
    """Format trade information using trade_economics as single source of truth."""
    analysis = alert["analysis"]
    trade_data = alert.get("trade_data", {})
    
    # Reconstruct economics from alert data
    size = float(trade_data.get("size", 0))
    raw_price = analysis.get("raw_price", analysis.get("odds", 0.5))
    
    # Get outcome from 'outcome' field (NOT 'name' - that's the username!)
    outcome_str = trade_data.get("outcome", "Yes") or "Yes"
    
    # For economics calculation, we need to know if it's a NO position
    # outcomeIndex is UNRELIABLE for sports — always use title detection for team names
    outcome_lower = str(outcome_str).lower()
    if outcome_lower in ('no', 'under'):
        is_no = True
    elif outcome_lower in ('yes', 'over'):
        is_no = False
    else:
        # Team/player name: detect from title
        market_title = alert.get('market', '') or trade_data.get('title', '')
        is_no = _is_second_in_vs_title(outcome_str, market_title)
    
    if size > 0 and raw_price > 0:
        # Calculate with proper NO detection
        econ = trade_economics.calculate(size, raw_price, "No" if is_no else "Yes")
    else:
        # Fallback for legacy alerts without size
        amount = float(analysis.get("amount", 0))
        econ = trade_economics.TradeEconomics(
            outcome=outcome_str, is_no=outcome_str.lower() == "no",
            raw_price=raw_price, effective_odds=analysis.get("odds", 0.5),
            cost=amount, tokens=0,
            potential_profit=analysis.get("potential_pnl", 0),
            pnl_multiplier=analysis.get("pnl_multiplier", 0),
            roi_percent=analysis.get("pnl_multiplier", 0) * 100,
        )
    
    position = determine_position(trade_data, econ.effective_odds)
    is_estimated = position.startswith('~')
    position_clean = position.lstrip('~')
    position_lower = position_clean.lower()
    
    # Check if it's a binary market (YES/NO) or named (team/player)
    if is_binary_market(position):
        # Binary market - try to make YES/NO clearer
        market_title = alert.get('market', '') or trade_data.get('title', '')
        subject = extract_market_subject(market_title)
        
        if position_clean.upper() == 'YES':
            if subject:
                position_display = f"{subject} ✓ @ {econ.raw_price*100:.1f}¢"
            else:
                position_display = f"YES @ {econ.raw_price*100:.1f}¢"
            implied_prob = econ.raw_price * 100
        else:
            if subject:
                position_display = f"Against {subject} @ {(1 - econ.raw_price)*100:.1f}¢"
            else:
                position_display = f"NO @ {(1 - econ.raw_price)*100:.1f}¢"
            implied_prob = (1 - econ.raw_price) * 100
    elif position_lower in ['over', 'under']:
        # O/U market - add line number if available
        market_title = alert.get('market', '') or trade_data.get('title', '')
        ou_line = extract_ou_line(market_title)
        
        if position_lower == 'under':
            price_display = (1 - econ.raw_price) * 100
        else:
            price_display = econ.raw_price * 100
        implied_prob = price_display
        
        if ou_line:
            position_display = f"{position_clean} {ou_line} @ {price_display:.1f}¢"
        else:
            position_display = f"{position_clean} @ {price_display:.1f}¢"
    else:
        # Sports/event market - show team/player name
        # Use title-based detection (outcomeIndex is unreliable)
        market_title = alert.get('market', '') or trade_data.get('title', '')
        is_second = _is_second_in_vs_title(position_clean, market_title)
        if is_second:
            position_display = f"{position_clean} @ {(1 - econ.raw_price)*100:.1f}¢"
            implied_prob = (1 - econ.raw_price) * 100
        else:
            position_display = f"{position_clean} @ {econ.raw_price*100:.1f}¢"
            implied_prob = econ.raw_price * 100
    
    if is_estimated:
        position_display += " ⚠️"
    
    # Format ROI display
    roi_multiplier = econ.pnl_multiplier
    if roi_multiplier < 0.1:
        roi_display = f"{roi_multiplier:.2f}x"
    elif roi_multiplier < 100:
        roi_display = f"{roi_multiplier:.1f}x"
    else:
        roi_display = f"{roi_multiplier:.0f}x"
    
    return {
        'position': position_display,
        'implied_prob': f"{implied_prob:.1f}%",
        'profit': f"${econ.potential_profit:,.0f}",
        'roi_percent': econ.roi_percent,
        'roi_display': roi_display,
        'is_estimated': is_estimated,
        'amount': f"${econ.cost:,.0f}",
        'tokens': f"{econ.tokens:,.0f}"
    }

def format_wallet_classification(wallet_stats: Optional[Dict]) -> str:
    """Format wallet classification with emoji"""
    if not wallet_stats:
        return "🆕 New Wallet"
    
    classification = wallet_stats.get('classification', 'Unknown')
    insider_score = wallet_stats.get('insider_score', 0)
    
    emoji_map = {
        'Probable Insider': '🔴',
        'Syndicate/Whale': '🟠',
        'Professional': '🟡',
        'Retail': '🟢',
        'New': '🆕'
    }
    
    emoji = emoji_map.get(classification, '⚪')
    return f"{emoji} {classification} (Score: {insider_score:.0f}/100)"

def format_latency_alert(latency: Optional[Dict]) -> str:
    """Format latency information with severity indicators"""
    if not latency or not latency.get('is_pre_event'):
        return ""
    
    minutes = abs(latency['latency_minutes'])
    severity = latency['severity']
    
    severity_emoji = {
        'CRITICAL': '🚨🚨🚨',
        'HIGH': '🚨🚨',
        'MEDIUM': '🚨',
        'LOW': '⏰'
    }
    
    emoji = severity_emoji.get(severity, '⏰')
    
    return f"\n{emoji} PRE-EVENT DETECTED: {minutes:.0f} minutes BEFORE event"

@lru_cache(maxsize=100)
def generate_ai_summary_cached(cache_key: str, market: str, position: str, amount: str, 
                                wallet_info: str, latency_info: str):
    """
    Cached AI summary generation.
    FIX ISSUE #14: Rate limiting with caching.
    FIX ISSUE #12: Improved error handling.
    """
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        
        # Build context
        context = f"Market: {market}\n"
        context += f"Position: {position}\n"
        context += f"Bet Size: {amount}\n"
        
        if wallet_info:
            context += f"Wallet History: {wallet_info}\n"
        
        if latency_info:
            context += f"Timing: {latency_info}\n"
        
        prompt = f"""Analyze this Polymarket trade in ONE concise sentence (max 15 words).

{context}

Focus on the SPECIFIC insight, not generic patterns. Be direct and actionable.

Good examples:
- "Unusual pre-event timing suggests advance knowledge of announcement"
- "Pattern matches previous insider trades from this wallet"
- "Coordinated timing with other large bets indicates organized group"

Bad examples (too generic):
- "Large bet suggests potential insider information"
- "Extreme confidence may indicate knowledge"

Write ONE specific insight (max 15 words):"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
            temperature=0.5
        )
        
        summary = response.choices[0].message.content.strip()
        
        # Remove quotes if AI added them
        summary = summary.strip('"').strip("'")
        
        return summary
        
    except openai.RateLimitError:
        return "⚠️ AI analysis rate limited - high-probability insider signal detected"
    except openai.APIError as e:
        return f"⚠️ AI analysis unavailable (API error)"
    except Exception as e:
        print(f"Error generating AI summary: {e}")
        return "High-probability insider signal detected"

def generate_ai_summary(alert):
    """
    Generate AI analysis with caching.
    FIX ISSUE #14: Cache identical alerts to reduce API costs.
    """
    trade_info = format_trade_info(alert)
    wallet_stats = alert.get('wallet_stats')
    latency = alert.get('latency')
    
    # Build wallet info string
    wallet_info = ""
    if wallet_stats and wallet_stats['total_trades'] >= 1:  # Lowered from 3 to show all wallet history
        wallet_info = f"{wallet_stats['total_trades']} trades, insider score {wallet_stats['insider_score']:.0f}"
    
    # Build latency info string
    latency_info = ""
    if latency and latency.get('is_pre_event'):
        latency_info = f"{latency['latency_minutes']:.0f} minutes BEFORE event"
    
    # Create cache key
    cache_key = hashlib.md5(
        f"{alert['market']}:{trade_info['position']}:{trade_info['amount']}:{wallet_info}:{latency_info}".encode()
    ).hexdigest()
    
    return generate_ai_summary_cached(
        cache_key,
        alert['market'],
        trade_info['position'],
        trade_info['amount'],
        wallet_info,
        latency_info
    )

def send_telegram_alert(alert):
    """
    Send institutional-grade alert to Telegram.
    FIX ISSUE #12: Improved error handling with fallback.
    """
    try:
        message = format_institutional_alert(alert)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
            "parse_mode": "Markdown"
        }
        
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✓ Alert sent successfully")
        return True
        
    except requests.exceptions.HTTPError as e:
        # Markdown parsing failed, try without markdown
        print(f"⚠️  Markdown parsing failed, retrying without formatting: {e}")
        try:
            payload["parse_mode"] = None
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            print(f"✓ Alert sent (without markdown)")
            return True
        except Exception as e2:
            print(f"❌ Alert sending failed completely: {e2}")
            return False
            
    except requests.exceptions.Timeout:
        print(f"❌ Telegram API timeout")
        return False
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Network error sending alert: {e}")
        return False
        
    except Exception as e:
        print(f"❌ Unexpected error sending alert: {e}")
        return False


def build_polymarket_url(trade_data: Dict, alert: Dict = None) -> str:
    """
    Build correct Polymarket URL based on market type.
    Sports: /sports/{league}/{slug}
    Events: /event/{eventSlug}
    """
    # Try to get slug from multiple sources
    slug = ''
    event_slug = ''
    
    if trade_data:
        slug = trade_data.get('slug', '') or trade_data.get('eventSlug', '')
        event_slug = trade_data.get('eventSlug', '') or slug
    
    if alert:
        slug = slug or alert.get('market_slug', '') or alert.get('event_slug', '')
        event_slug = event_slug or alert.get('event_slug', '') or alert.get('market_slug', '') or slug
    
    if not event_slug:
        return "https://polymarket.com"
    
    # Detect sport leagues from slug pattern
    # Simple sports: /sports/{league}/{slug} works directly
    sport_prefixes = {
        'nba-': 'nba', 'nfl-': 'nfl', 'mlb-': 'mlb', 'nhl-': 'nhl',
        'epl-': 'epl', 'mls-': 'mls', 'ncaa-': 'ncaa', 'wnba-': 'wnba',
        'elc-': 'efl-championship', 'ufc-': 'mma', 'f1-': 'f1',
        'tennis-': 'tennis', 'golf-': 'golf'
    }
    
    # Esports have complex URL paths (/sports/league-of-legends/games/week/N/slug)
    # that we can't reconstruct from slug alone — use event format instead
    esports_prefixes = {'cs2-', 'dota-', 'lol-', 'val-', 'rl-'}
    
    for prefix in esports_prefixes:
        if slug.startswith(prefix) or event_slug.startswith(prefix):
            return f"https://polymarket.com/event/{event_slug}"
    
    for prefix, league in sport_prefixes.items():
        if slug.startswith(prefix) or event_slug.startswith(prefix):
            return f"https://polymarket.com/sports/{league}/{event_slug}"
    
    # Default event URL format
    return f"https://polymarket.com/event/{event_slug}"


def format_top_trader_alert(alert: Dict) -> str:
    """
    Format alert for top trader activity.
    New compact format for better UX.
    """
    from datetime import datetime, timezone
    
    trader = alert.get('trader', {})
    trade = alert.get('trade', {})
    
    rank = trader.get('rank', '?')
    raw_username = trader.get('username', '')
    # Some traders have wallet address or wallet+timestamp as "username" — not useful
    if raw_username and not raw_username.startswith('0x') and len(raw_username) < 30:
        username = raw_username
    else:
        username = f"Trader #{rank}"
    profit = trader.get('profit', 0)
    volume = trader.get('volume', 0)
    
    # Trade details
    size = float(trade.get('size', 0))
    price = float(trade.get('price', 0))
    
    # Get outcome name from 'outcome' field (NOT 'name' - that's the username!)
    # For sports: "Over", "Under", team names, etc.
    outcome_name = trade.get('outcome', 'Yes')
    
    # Calculate cost based on outcomeIndex or outcome value
    # outcomeIndex: 0 = first option (usually YES or Team1), 1 = second option (NO or Team2)
    outcome_index = trade.get('outcomeIndex')  # None if missing!
    outcome_lower = str(outcome_name).lower()
    
    # Determine if this is the "second side" (NO equivalent)
    # IMPORTANT: outcomeIndex is unreliable for sports markets —
    # it's a token index, NOT position in "X vs Y" title.
    # Always use title-based detection for team/player names.
    if outcome_lower in ('no', 'under'):
        is_second_side = True
    elif outcome_lower in ('yes', 'over'):
        is_second_side = False
    else:
        # Team/player name: detect from "X vs Y" in title
        market_title = trade.get('title', '') or alert.get('market', '')
        is_second_side = _is_second_in_vs_title(outcome_name, market_title)
    
    if is_second_side:
        amount = size * (1 - price)
        odds_display = f"{(1-price)*100:.0f}%"
    else:
        amount = size * price
        odds_display = f"{price*100:.0f}%"
    
    # Show actual outcome name (team name for sports, YES/NO for binary)
    # For "Will X win?" markets, make YES/NO clearer
    if outcome_lower in ['yes', 'no']:
        # Try to extract subject from market title for clearer display
        market_title = trade.get('title', '') or alert.get('market', '')
        subject = extract_market_subject(market_title)
        
        if subject and outcome_lower == 'no':
            # Show "Against X" instead of just "NO"
            position = f"Against {subject} @ {odds_display}"
        elif subject and outcome_lower == 'yes':
            # Show "X wins" instead of just "YES"  
            position = f"{subject} ✓ @ {odds_display}"
        else:
            position = f"{outcome_name.upper()} @ {odds_display}"
    elif outcome_lower in ['over', 'under']:
        # O/U market - try to add the line number
        market_title = trade.get('title', '') or alert.get('market', '')
        ou_line = extract_ou_line(market_title)
        if ou_line:
            position = f"{outcome_name} {ou_line} @ {odds_display}"
        else:
            position = f"{outcome_name} @ {odds_display}"
    else:
        # Sports/esports market - show team/player name as-is
        position = f"{outcome_name} @ {odds_display}"
    
    # Get market name from trade data (title field, not nested market)
    market = trade.get('title', '') or alert.get('market', '')
    if not market or market == 'Unknown market':
        market = trade.get('slug', '') or 'Unknown'
    
    wallet = alert.get('wallet', '')
    wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
    
    # Build correct URL
    url = build_polymarket_url(trade, alert)
    
    # Determine verdict based on profit, rank, AND bet size
    # A $190 bet from a $16M trader is noise, not a signal
    if amount < 1000:
        verdict = "🔵 MONITOR"
        verdict_note = f"Tiny bet (${amount:,.0f}) — noise, not conviction"
    elif amount < 3000:
        verdict = "🔵 MONITOR"
        verdict_note = f"Small bet (${amount:,.0f}) — low conviction"
    elif profit >= 1000000:
        verdict = "🟢 STRONG COPY"
        verdict_note = f"Elite trader (${profit/1000000:.1f}M profit) · ${amount:,.0f} bet"
    elif profit >= 100000:
        verdict = "🟡 CONSIDER"
        verdict_note = f"Solid trader (${profit/1000:.0f}K profit) · ${amount:,.0f} bet"
    else:
        verdict = "🔵 MONITOR"
        verdict_note = "Track before copying"
    
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    
    message = f"""👑 TOP TRADER SIGNAL

MARKET
{market}

TRADER PROFILE
{username} · Rank #{rank}
P&L: ${profit:,.0f} · Vol: ${volume/1000000:.1f}M

MOVE
{position} · ${amount:,.0f}
Wallet: {wallet_short}"""

    # AI Context (if available)
    ai_context = alert.get('ai_context')
    if ai_context:
        message += f"\n\nCONTEXT\n→ {ai_context}"

    message += f"""

VERDICT: {verdict}
{verdict_note}

🔗 {url}
Polymarket Insiders | {timestamp} UTC"""
    
    return message


def format_institutional_alert(alert):
    """
    Format insider alert with new compact UI.
    Three signal colors: 🟢 HIGH, 🟡 WATCH, 🔴 CONFLICT
    """
    from datetime import datetime, timezone
    
    analysis = alert["analysis"]
    trade_info = format_trade_info(alert)
    trade_data = alert.get("trade_data", {})
    wallet_stats = alert.get('wallet_stats')
    latency = alert.get('latency')
    top_trader = alert.get('top_trader')
    
    # Get market data
    market = alert.get('market', 'Unknown market')
    # Market odds for display — always use raw YES price
    yes_price = analysis.get('raw_price', analysis.get('odds', 0.5))
    no_price = 1 - yes_price
    
    # Build URL
    url = build_polymarket_url(trade_data, alert)
    
    # === Combined Signal Analysis ===
    combined = alert.get('combined_signal', {})
    mispricing = alert.get('mispricing', {})
    irrationality = alert.get('irrationality', {})
    
    signal_type = combined.get('signal_type', 'INSIDER_ONLY')
    edge_percent = float(mispricing.get('edge_percent', 0))
    
    # Determine outcome display names for non-binary markets
    outcome_name = trade_data.get('outcome', 'Yes')
    outcome_lower = str(outcome_name).lower()
    is_binary = outcome_lower in ('yes', 'no')
    
    # For O/U: show "Over/Under" instead of "YES/NO"
    # For sports: show team names
    if is_binary:
        side_a, side_b = "YES", "NO"
    elif outcome_lower in ('over', 'under'):
        side_a, side_b = "Over", "Under"
    else:
        # Sports: detect which side the outcome is on using title
        market_title = alert.get('market', '') or trade_data.get('title', '')
        is_second = _is_second_in_vs_title(str(outcome_name), market_title)
        if is_second:
            side_a = "Opponent"
            side_b = str(outcome_name)
        else:
            side_a = str(outcome_name)
            side_b = "Opponent"
    
    # Determine which side stats favor
    if edge_percent > 0:
        ev_direction = "NO"
        edge_note = f"{side_a} overpriced +{edge_percent:.1f}% → FAVORS {side_b}"
    elif edge_percent < 0:
        ev_direction = "YES"
        edge_note = f"{side_b} overpriced {edge_percent:.1f}% → FAVORS {side_a}"
    else:
        ev_direction = None
        edge_note = "No clear edge detected"
    
    # === Build Message ===
    message = f"""👁️ INSIDER ACTIVITY

MARKET SIGNAL
{market}
Odds: {side_a} {yes_price*100:.0f}% | {side_b} {no_price*100:.0f}%
Edge: {edge_note}"""
    
    # === Insider Move ===
    wallet = alert['wallet']
    wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
    amount = float(analysis.get('amount', 0))
    
    # Wallet profile
    if top_trader:
        profile = f"Top #{top_trader['rank']} trader"
    elif wallet_stats and wallet_stats.get('total_trades', 0) > 0:
        profile = wallet_stats.get('classification', 'Unknown')
    else:
        profile = "new wallet"
    
    message += f"""

INSIDER MOVE
Wallet: {wallet_short} ({profile})
Bet: ${amount:,.0f} {trade_info['position']}"""
    
    # Lead time if available
    if latency and latency.get('is_pre_event'):
        lead_min = int(latency['latency_minutes'])
        if lead_min < 60:
            message += f"\n⏰ {lead_min}m before event"
        elif lead_min < 1440:
            message += f"\n⏰ {lead_min/60:.1f}h before event"
    
    # === Verdict ===
    fa = alert.get('financial_analyst', {})
    stance = fa.get('stance', 'WATCH_ONLY')
    quality = fa.get('signal_quality', 0)
    
    # Determine position side for conflict detection
    # Use normalized_position from detector (handles Over/Under/team names correctly)
    position_side = trade_data.get('normalized_position', combined.get('insider_position', 'YES'))
    # Fallback: parse from display string if normalized_position not available
    if not position_side or position_side not in ('YES', 'NO'):
        position_str = trade_info['position']
        if 'YES' in position_str.upper():
            position_side = 'YES'
        elif 'NO' in position_str.upper():
            position_side = 'NO'
        else:
            # Team/player name: use title detection
            market_title = alert.get('market', '') or trade_data.get('title', '')
            outcome = trade_data.get('outcome', 'Yes')
            position_side = 'NO' if _is_second_in_vs_title(str(outcome), market_title) else 'YES'
    
    has_conflict = ev_direction and position_side != ev_direction
    
    # Get display name for verdict (team name for sports, YES/NO for binary)
    display_position = trade_info['position']
    position_display_name = display_position.split(' @')[0] if ' @' in display_position else str(outcome_name)
    
    # Show which side model favors in human terms
    model_favors_display = side_b if ev_direction == "NO" else side_a
    
    if has_conflict:
        verdict = "⚠️ MODEL CONFLICT"
        verdict_note = f"Insider bets {position_display_name}, model favors {model_favors_display}"
    elif stance == "HIGH_CONVICTION":
        verdict = "🟢 ACTION"
        verdict_note = f"Strong signal on {position_display_name}"
    elif stance == "SELECTIVE":
        verdict = "🟡 WATCH"
        verdict_note = "Monitor for confirmation"
    else:
        verdict = "🔵 WATCH"
        verdict_note = "Low confidence, track only"
    
    # Risk notes
    risks = []
    if not wallet_stats or wallet_stats.get('total_trades', 0) < 3:
        risks.append("New wallet")
    if amount < 5000:
        risks.append("Small bet")
    if edge_percent > 15:
        risks.append(f"Large mispricing ({edge_percent:+.0f}%)")
    
    message += f"""

VERDICT: {verdict}
{verdict_note}"""
    
    if risks:
        message += f"\nRisk: {', '.join(risks[:3])}"
    
    message += f"\nSignal: {quality:.0f}/100"
    
    # === Footer ===
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    
    message += f"""

🔗 {url}
Polymarket Insiders | {timestamp} UTC"""
    
    if trade_info.get('is_estimated'):
        message += f"\n⚠️ Position estimated from odds"
    
    return message


def send_top_trader_alert(alert: Dict) -> bool:
    """Send top trader alert to Telegram."""
    try:
        message = format_top_trader_alert(alert)
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        }
        
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✓ Top trader alert sent")
        return True
        
    except Exception as e:
        print(f"❌ Error sending top trader alert: {e}")
        return False
