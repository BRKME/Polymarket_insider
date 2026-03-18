# Polymarket Insider — Telegram Notifier
# Formats and sends insider/top-trader alerts

# Debug flag — set to False in production to reduce log noise
DEBUG_CALCULATIONS = False

import requests
from openai import OpenAI
import openai
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY
from typing import Dict, Optional
from functools import lru_cache
import hashlib

def determine_position(trade_data, odds):
    """Determine YES/NO position from trade data"""
    if trade_data:
        outcome = trade_data.get('outcome')
        if outcome:
            outcome_lower = str(outcome).lower()
            if 'yes' in outcome_lower:
                return 'YES'
            if 'no' in outcome_lower:
                return 'NO'
    
    # Fallback
    return '~YES' if odds > 0.5 else '~NO'

def format_trade_info(alert):
    """Format trade information with correct profit calculation"""
    if DEBUG_CALCULATIONS:
        print(f"[DEBUG] format_trade_info() called")
    
    analysis = alert["analysis"]
    trade_data = alert.get("trade_data", {})
    
    # BUG FIX: analysis['odds'] is EFFECTIVE odds (already flipped for NO).
    # We need the raw YES price for calculations.
    # raw_price = YES token price from API (always).
    # odds = effective probability the trader is betting on.
    yes_price = analysis.get('raw_price', analysis['odds'])
    no_price = 1 - yes_price
    amount = analysis['amount']
    
    position = determine_position(trade_data, analysis['odds'])
    is_estimated = position.startswith('~')
    
    if 'YES' in position:
        implied_prob = yes_price * 100
        tokens_bought = amount / yes_price if yes_price > 0 else 0
        payout_if_win = tokens_bought * 1.0
        potential_profit = payout_if_win - amount
        position_display = f"YES @ {yes_price*100:.1f}¢"
    else:
        implied_prob = no_price * 100
        tokens_bought = amount / no_price if no_price > 0 else 0
        payout_if_win = tokens_bought * 1.0
        potential_profit = payout_if_win - amount
        position_display = f"NO @ {no_price*100:.1f}¢"
        
        # DEBUG: Print calculation details
        if DEBUG_CALCULATIONS:
            print(f"[DEBUG] NO POSITION CALCULATION:")
            print(f"  YES price (odds): {yes_price:.4f} ({yes_price*100:.1f}¢)")
            print(f"  NO price: {no_price:.4f} ({no_price*100:.1f}¢)")
            print(f"  Amount: ${amount:,.0f}")
            print(f"  Tokens bought: {tokens_bought:,.0f}")
            print(f"  Potential profit: ${potential_profit:,.0f}")
            print(f"  Position display: {position_display}")
    
    if is_estimated:
        position_display += " ⚠️"
    
    # Calculate ROI
    roi_percent = (potential_profit / amount * 100) if amount > 0 else 0
    roi_multiplier = roi_percent / 100
    
    # Format ROI display
    if roi_multiplier < 0.1:
        roi_display = f"{roi_multiplier:.2f}x"  # 0.04x for small ROI
    elif roi_multiplier < 100:
        roi_display = f"{roi_multiplier:.1f}x"  # 5.7x for medium ROI
    else:
        roi_display = f"{roi_multiplier:.0f}x"  # 200x for large ROI
    
    return {
        'position': position_display,
        'implied_prob': f"{implied_prob:.1f}%",
        'profit': f"${potential_profit:,.0f}",
        'roi_percent': roi_percent,
        'roi_display': roi_display,
        'is_estimated': is_estimated,
        'amount': f"${amount:,.0f}",
        'tokens': f"{tokens_bought:,.0f}"
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
    sport_prefixes = {
        'nba-': 'nba', 'nfl-': 'nfl', 'mlb-': 'mlb', 'nhl-': 'nhl',
        'epl-': 'epl', 'mls-': 'mls', 'ncaa-': 'ncaa', 'wnba-': 'wnba',
        'cs2-': 'esports', 'dota-': 'esports', 'lol-': 'esports',
        'elc-': 'efl-championship', 'ufc-': 'mma', 'f1-': 'f1',
        'tennis-': 'tennis', 'golf-': 'golf'
    }
    
    for prefix, league in sport_prefixes.items():
        if slug.startswith(prefix) or event_slug.startswith(prefix):
            # Sports URL format
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
    username = trader.get('username', '') or f"Trader #{rank}"
    profit = trader.get('profit', 0)
    volume = trader.get('volume', 0)
    
    # Trade details
    size = float(trade.get('size', 0))
    price = float(trade.get('price', 0))
    outcome = trade.get('outcome', 'Yes')
    
    if outcome.lower() == 'no':
        amount = size * (1 - price)
        position = f"NO @ {(1-price)*100:.0f}%"
    else:
        amount = size * price
        position = f"YES @ {price*100:.0f}%"
    
    # Get market name from trade data (title field, not nested market)
    market = trade.get('title', '') or alert.get('market', '')
    if not market or market == 'Unknown market':
        market = trade.get('slug', '') or 'Unknown'
    
    wallet = alert.get('wallet', '')
    wallet_short = f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 12 else wallet
    
    # Build correct URL
    url = build_polymarket_url(trade, alert)
    
    # Determine verdict based on profit and rank (win_rate not available from API)
    if profit >= 1000000:
        verdict = "🟢 STRONG COPY"
        verdict_note = f"Elite trader (${profit/1000000:.1f}M lifetime profit)"
    elif profit >= 100000:
        verdict = "🟡 CONSIDER"
        verdict_note = f"Solid track record (${profit/1000:.0f}K profit)"
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
Wallet: {wallet_short}

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
    yes_price = analysis.get('odds', 0.5)
    no_price = 1 - yes_price
    
    # Build URL
    url = build_polymarket_url(trade_data, alert)
    
    # === Combined Signal Analysis ===
    combined = alert.get('combined_signal', {})
    mispricing = alert.get('mispricing', {})
    irrationality = alert.get('irrationality', {})
    
    signal_type = combined.get('signal_type', 'INSIDER_ONLY')
    edge_percent = float(mispricing.get('edge_percent', 0))
    
    # Determine which side stats favor
    if edge_percent > 0:
        ev_direction = "NO"
        edge_note = f"YES overpriced +{edge_percent:.1f}% → FAVORS NO"
    elif edge_percent < 0:
        ev_direction = "YES"
        edge_note = f"NO overpriced {edge_percent:.1f}% → FAVORS YES"
    else:
        ev_direction = None
        edge_note = "No clear edge detected"
    
    # === Build Message ===
    message = f"""👁️ INSIDER ACTIVITY

MARKET SIGNAL
{market}
Odds: YES {yes_price*100:.0f}% | NO {no_price*100:.0f}%
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
    
    # Check for conflict
    position_side = 'YES' if 'YES' in trade_info['position'] else 'NO'
    has_conflict = ev_direction and position_side != ev_direction
    
    if has_conflict:
        verdict = "⚠️ MODEL CONFLICT"
        verdict_note = f"Whale bets {position_side}, math says {ev_direction}"
    elif stance == "HIGH_CONVICTION":
        verdict = "🟢 ACTION"
        verdict_note = f"Strong signal, consider {ev_direction or position_side} position"
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
