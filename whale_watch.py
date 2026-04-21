"""
Whale Watch — detect large capital flows into markets.

Different from insider detection:
- Insider: one suspicious wallet, one large trade
- Whale Watch: AGGREGATE flow — many trades, same direction, same market

Signal: when $50K+ flows into one side of a market in 20 minutes,
someone (or multiple someones) know something.

Runs on the same trade data as detector — no extra API calls.
"""

from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Optional
import trade_economics


# Thresholds
MIN_FLOW_TOTAL = 20000       # $20K+ total directional flow (was $50K — only caught 1 market)
MIN_IMBALANCE = 0.65         # 65%+ of volume on one side
MIN_UNIQUE_WALLETS = 2       # At least 2 wallets (not just one whale)
MAX_ODDS_THRESHOLD = 0.93    # Skip near-certain markets

# Markets to skip (same as detector)
SKIP_KEYWORDS = [
    '15m', '15 min', '15-min', 'updown', 'up or down',
    'up/down', 'bitcoin up or down', 'eth up or down',
]


def analyze_whale_flows(trades: List[Dict], markets: List[Dict]) -> List[Dict]:
    """
    Aggregate trades by market and detect unusual directional flows.
    
    Args:
        trades: raw trades from collector (all trades, not just large ones)
        markets: market data for context
        
    Returns:
        List of whale signals, sorted by flow size
    """
    print(f"\n[{datetime.now()}] 🐋 WHALE WATCH — Analyzing {len(trades)} trades...", flush=True)
    
    market_lookup = {m.get('conditionId'): m for m in markets if m.get('conditionId')}
    
    # Aggregate trades by market
    flows = defaultdict(lambda: {
        "yes_volume": 0,
        "no_volume": 0,
        "yes_trades": 0,
        "no_trades": 0,
        "wallets_yes": set(),
        "wallets_no": set(),
        "max_single_trade": 0,
        "trades": [],
    })
    
    skipped = 0
    for trade in trades:
        try:
            condition_id = trade.get("conditionId", "")
            if not condition_id:
                continue
            
            # Skip HFT/noise markets
            title = trade.get("title", "").lower()
            if any(kw in title for kw in SKIP_KEYWORDS):
                skipped += 1
                continue
            
            # Skip SELL trades (closing positions)
            if trade.get("side") == "SELL":
                continue
            
            size = float(trade.get("size", 0))
            price = float(trade.get("price", 0))
            outcome = trade.get("outcome", "Yes")
            wallet = trade.get("proxyWallet", "")
            
            if not (0 < price < 1) or size <= 0:
                continue
            
            # Determine YES/NO using trade_economics
            outcome_lower = str(outcome).lower()
            if outcome_lower in ("no", "under"):
                econ_outcome = "No"
            elif outcome_lower in ("yes", "over"):
                econ_outcome = "Yes"
            else:
                # Team name — detect from title
                market = market_lookup.get(condition_id, {})
                market_title = market.get("question", "")
                try:
                    from notifier import _is_second_in_vs_title
                    econ_outcome = "No" if _is_second_in_vs_title(outcome, market_title) else "Yes"
                except:
                    econ_outcome = "Yes"
            
            econ = trade_economics.calculate(size, price, econ_outcome)
            cost = econ.cost
            
            if cost <= 0:
                continue
            
            flow = flows[condition_id]
            if econ.is_no:
                flow["no_volume"] += cost
                flow["no_trades"] += 1
                flow["wallets_no"].add(wallet)
            else:
                flow["yes_volume"] += cost
                flow["yes_trades"] += 1
                flow["wallets_yes"].add(wallet)
            
            flow["max_single_trade"] = max(flow["max_single_trade"], cost)
            
        except Exception:
            continue
    
    # Detect whale signals
    signals = []
    
    for condition_id, flow in flows.items():
        total = flow["yes_volume"] + flow["no_volume"]
        if total < MIN_FLOW_TOTAL:
            continue
        
        # Directional imbalance
        if total > 0:
            yes_pct = flow["yes_volume"] / total
            no_pct = flow["no_volume"] / total
        else:
            continue
        
        # Dominant direction
        if yes_pct >= MIN_IMBALANCE:
            dominant = "YES"
            dominant_volume = flow["yes_volume"]
            dominant_pct = yes_pct
            dominant_wallets = flow["wallets_yes"]
            dominant_trades = flow["yes_trades"]
        elif no_pct >= MIN_IMBALANCE:
            dominant = "NO"
            dominant_volume = flow["no_volume"]
            dominant_pct = no_pct
            dominant_wallets = flow["wallets_no"]
            dominant_trades = flow["no_trades"]
        else:
            continue  # No strong imbalance
        
        # Need at least 2 wallets (one whale is already caught by detector)
        if len(dominant_wallets) < MIN_UNIQUE_WALLETS:
            continue
        
        # Get market data
        market = market_lookup.get(condition_id, {})
        market_question = market.get("question", "")
        
        # Fallback: fetch from Gamma API if market not in local lookup
        if not market_question or market_question == "Unknown market":
            try:
                import requests as req
                import time as t
                t.sleep(0.5)
                resp = req.get("https://gamma-api.polymarket.com/markets", 
                              params={"condition_id": condition_id, "limit": 1}, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if data and len(data) > 0:
                        market = data[0]
                        market_question = market.get("question", "")
            except Exception:
                pass
        
        # Get current market price (YES price)
        yes_price = None
        try:
            outcomes = market.get("outcomes", [])
            prices = market.get("outcomePrices", [])
            if isinstance(outcomes, str):
                import json
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                import json
                prices = json.loads(prices)
            if prices:
                yes_price = float(prices[0])
        except:
            pass
        
        # Skip near-certain markets
        if yes_price and (yes_price > MAX_ODDS_THRESHOLD or yes_price < (1 - MAX_ODDS_THRESHOLD)):
            continue
        
        # Strength score (scaled for $20K+ threshold)
        strength = 0
        strength += min(40, int(dominant_volume / 2000))  # Up to 40 for volume ($80K+)
        strength += min(20, len(dominant_wallets) * 5)     # Up to 20 for wallet diversity
        strength += int(dominant_pct * 30)                  # Up to 30 for imbalance
        if flow["max_single_trade"] > 5000:
            strength += 10                                  # Bonus for big single trade
        strength = min(100, strength)
        
        signal = {
            "type": "WHALE_FLOW",
            "condition_id": condition_id,
            "market": market_question or f"Unknown (cid: {condition_id[:20]})",
            "market_slug": market.get("slug", ""),
            "event_slug": market.get("eventSlug", ""),
            "dominant_side": dominant,
            "dominant_volume": round(dominant_volume, 0),
            "total_volume": round(total, 0),
            "imbalance_pct": round(dominant_pct * 100, 1),
            "unique_wallets": len(dominant_wallets),
            "trade_count": dominant_trades,
            "max_single_trade": round(flow["max_single_trade"], 0),
            "yes_price": yes_price,
            "strength": strength,
            "timestamp": datetime.now().isoformat(),
        }
        signals.append(signal)
    
    # Sort by volume
    signals.sort(key=lambda s: s["dominant_volume"], reverse=True)
    
    print(f"[{datetime.now()}] 🐋 Whale signals: {len(signals)} (from {len(flows)} active markets, {skipped} HFT skipped)", flush=True)
    
    for s in signals[:5]:
        print(f"  🐋 ${s['dominant_volume']:,.0f} {s['dominant_side']} ({s['imbalance_pct']:.0f}%) | {s['unique_wallets']} wallets | {s['market'][:55]}", flush=True)
    
    return signals


def format_whale_alert(signal: Dict) -> str:
    """Format whale signal for Telegram."""
    side = signal["dominant_side"]
    vol = signal["dominant_volume"]
    total = signal["total_volume"]
    imbalance = signal["imbalance_pct"]
    wallets = signal["unique_wallets"]
    trades = signal["trade_count"]
    strength = signal["strength"]
    market = signal["market"]
    yes_price = signal.get("yes_price")
    max_trade = signal["max_single_trade"]
    
    # Emoji by strength
    if strength >= 75:
        emoji = "🔴"
        label = "STRONG"
    elif strength >= 50:
        emoji = "🟡"
        label = "MODERATE"
    else:
        emoji = "⚪"
        label = "WEAK"
    
    msg = f"🐋 WHALE FLOW — {label}\n\n"
    msg += f"MARKET\n{market}\n\n"
    msg += f"FLOW\n"
    msg += f"${vol:,.0f} → {side} ({imbalance:.0f}% directional)\n"
    msg += f"Total volume: ${total:,.0f} in 20min\n"
    msg += f"Biggest single trade: ${max_trade:,.0f}\n\n"
    msg += f"PARTICIPANTS\n"
    msg += f"{wallets} unique wallets · {trades} trades\n\n"
    
    if yes_price:
        msg += f"MARKET PRICE\n"
        msg += f"YES: {yes_price*100:.0f}¢ | NO: {(1-yes_price)*100:.0f}¢\n\n"
    
    msg += f"STRENGTH: {emoji} {strength}/100"
    
    # Link
    slug = signal.get("event_slug") or signal.get("market_slug", "")
    if slug:
        msg += f"\n🔗 https://polymarket.com/event/{slug}"
    
    return msg
