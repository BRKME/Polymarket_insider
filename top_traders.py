"""
Top Traders Module — Copy trades from Polymarket leaderboard winners.

Data source: https://polymarket.com/leaderboard
API endpoint: https://gamma-api.polymarket.com/leaderboard

Strategy: Track top 50 profitable wallets, alert on their new positions.
"""

import requests
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from config import GAMMA_API_URL, REQUEST_DELAY

# Minimum criteria for tracking
MIN_PROFIT_ALL_TIME = 50000      # $50K lifetime profit
MIN_WIN_RATE = 0.55              # 55% win rate
MIN_VOLUME = 100000              # $100K total volume
MAX_LEADERBOARD_RANK = 50        # Top 50 only

# Cache for leaderboard data
_leaderboard_cache: Dict = {}
_cache_timestamp: Optional[datetime] = None
CACHE_TTL_MINUTES = 60


def fetch_leaderboard(limit: int = 50) -> List[Dict]:
    """
    Fetch Polymarket leaderboard.
    Returns list of top traders with their stats.
    """
    global _leaderboard_cache, _cache_timestamp
    
    # Check cache
    if _cache_timestamp and (datetime.now() - _cache_timestamp).seconds < CACHE_TTL_MINUTES * 60:
        if _leaderboard_cache:
            return _leaderboard_cache.get('traders', [])
    
    url = f"{GAMMA_API_URL}/leaderboard"
    params = {
        "limit": limit,
        "window": "all"  # all-time stats
    }
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        traders = []
        
        for idx, entry in enumerate(data, start=1):
            trader = {
                'rank': idx,
                'address': entry.get('address', ''),
                'username': entry.get('username', ''),
                'profit': float(entry.get('pnl', 0) or 0),
                'volume': float(entry.get('volume', 0) or 0),
                'positions': int(entry.get('positionsWon', 0) or 0) + int(entry.get('positionsLost', 0) or 0),
                'positions_won': int(entry.get('positionsWon', 0) or 0),
                'positions_lost': int(entry.get('positionsLost', 0) or 0),
            }
            
            total = trader['positions_won'] + trader['positions_lost']
            trader['win_rate'] = trader['positions_won'] / total if total > 0 else 0
            
            traders.append(trader)
        
        # Update cache
        _leaderboard_cache = {'traders': traders}
        _cache_timestamp = datetime.now()
        
        print(f"[{datetime.now()}] ✓ Fetched {len(traders)} leaderboard entries")
        return traders
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching leaderboard: {e}")
        return []


def get_tracked_wallets() -> Dict[str, Dict]:
    """
    Get wallets worth tracking based on criteria.
    Returns dict: address -> trader info
    """
    traders = fetch_leaderboard(limit=MAX_LEADERBOARD_RANK)
    tracked = {}
    
    for trader in traders:
        # Apply filters
        if trader['profit'] < MIN_PROFIT_ALL_TIME:
            continue
        if trader['win_rate'] < MIN_WIN_RATE:
            continue
        if trader['volume'] < MIN_VOLUME:
            continue
        
        tracked[trader['address'].lower()] = trader
    
    print(f"[{datetime.now()}] Tracking {len(tracked)} top traders (of {len(traders)} checked)")
    return tracked


def is_top_trader(wallet_address: str) -> Optional[Dict]:
    """
    Check if wallet belongs to a tracked top trader.
    Returns trader info if yes, None otherwise.
    """
    tracked = get_tracked_wallets()
    return tracked.get(wallet_address.lower())


def fetch_trader_recent_positions(address: str, hours: int = 24) -> List[Dict]:
    """
    Fetch recent positions for a specific trader.
    Used to detect new bets from tracked wallets.
    """
    url = f"{GAMMA_API_URL}/positions"
    params = {
        "user": address,
        "limit": 50,
        "sortBy": "createdAt",
        "sortDirection": "desc"
    }
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        positions = response.json()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        recent = []
        for pos in positions:
            created = pos.get('createdAt')
            if created:
                try:
                    created_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                    if created_dt > cutoff:
                        recent.append(pos)
                except:
                    pass
        
        return recent
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching positions for {address[:10]}...: {e}")
        return []


def fetch_trader_recent_trades(address: str, minutes_back: int = 60) -> List[Dict]:
    """
    Fetch recent trades for a specific trader.
    Returns list of trades with market info.
    """
    from config import DATA_API_URL
    
    url = f"{DATA_API_URL}/trades"
    params = {
        "maker": address,
        "limit": 50,
        "sortBy": "TIMESTAMP",
        "sortDirection": "DESC"
    }
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code != 200:
            # Try with 'user' param instead
            params = {
                "user": address,
                "limit": 50,
            }
            response = requests.get(url, params=params, timeout=30)
        
        if response.status_code != 200:
            return []
        
        trades = response.json()
        
        # Debug: show raw response for first trader
        if not hasattr(fetch_trader_recent_trades, '_debug_shown'):
            fetch_trader_recent_trades._debug_shown = True
            print(f"[DEBUG] API returned {len(trades)} trades for first trader")
            if trades:
                print(f"[DEBUG] First trade keys: {list(trades[0].keys())[:10]}")
        
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
        cutoff_ts = cutoff.timestamp()
        
        recent = []
        for trade in trades:
            ts = trade.get('timestamp', 0)
            if ts > 10000000000:
                ts = ts // 1000
            
            if ts >= cutoff_ts:
                # Fetch market info if not present
                if not trade.get('market'):
                    condition_id = trade.get('conditionId', '')
                    if condition_id:
                        market_info = fetch_market_info(condition_id)
                        trade['market'] = market_info
                recent.append(trade)
        
        return recent
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error fetching trades for {address[:10]}...: {e}")
        return []


def fetch_market_info(condition_id: str) -> Dict:
    """Fetch market info by condition ID."""
    url = f"{GAMMA_API_URL}/markets/{condition_id}"
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            return response.json()
    except:
        pass
    
    return {'question': 'Unknown market', 'slug': ''}


def detect_top_trader_signals(trades: List[Dict]) -> List[Dict]:
    """
    Scan trades for top trader activity.
    Returns list of alerts for trades from tracked wallets.
    """
    tracked = get_tracked_wallets()
    if not tracked:
        return []
    
    signals = []
    
    for trade in trades:
        wallet = trade.get('proxyWallet', '').lower()
        
        if wallet in tracked:
            trader_info = tracked[wallet]
            
            # Build signal
            signal = {
                'type': 'TOP_TRADER',
                'trade': trade,
                'trader': trader_info,
                'wallet': wallet,
                'rank': trader_info['rank'],
                'profit': trader_info['profit'],
                'win_rate': trader_info['win_rate'],
                'username': trader_info.get('username', '')
            }
            
            signals.append(signal)
            print(f"[{datetime.now()}] 👑 Top trader #{trader_info['rank']} detected: {wallet[:10]}...")
    
    return signals


def format_top_trader_alert(signal: Dict, market: Dict) -> str:
    """
    Format alert message for top trader signal.
    """
    trader = signal['trader']
    trade = signal['trade']
    
    size = float(trade.get('size', 0))
    price = float(trade.get('price', 0))
    outcome = trade.get('outcome', 'Yes')
    
    if outcome.lower() == 'no':
        amount = size * (1 - price)
        position = f"NO @ {(1-price)*100:.0f}%"
    else:
        amount = size * price
        position = f"YES @ {price*100:.0f}%"
    
    username = trader.get('username', '') or f"Wallet #{trader['rank']}"
    
    message = f"""👑 TOP TRADER SIGNAL

📊 MARKET
{market.get('question', 'Unknown market')}

👤 TRADER: {username}
Rank: #{trader['rank']} on leaderboard
Lifetime profit: ${trader['profit']:,.0f}
Win rate: {trader['win_rate']*100:.1f}%
Volume: ${trader['volume']:,.0f}

💰 POSITION
{position}
Size: ${amount:,.0f}

Wallet: {signal['wallet']}

✅ ACTION: Consider copying with 25-40% sizing

🔗 https://polymarket.com/event/{market.get('slug', '')}
📍 Radar | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"""
    
    return message


# Periodic scan function (called from main.py)
def scan_top_traders() -> List[Dict]:
    """
    Main entry point: scan for top trader signals.
    Returns list of formatted alerts ready for Telegram.
    """
    print(f"[{datetime.now()}] Scanning for top trader activity...")
    
    tracked = get_tracked_wallets()
    if not tracked:
        print(f"[{datetime.now()}] No traders meet tracking criteria")
        return []
    
    alerts = []
    
    for address, trader_info in tracked.items():
        positions = fetch_trader_recent_positions(address, hours=24)
        
        for pos in positions:
            # Build alert
            alert = {
                'type': 'TOP_TRADER',
                'trader': trader_info,
                'position': pos,
                'wallet': address
            }
            alerts.append(alert)
    
    print(f"[{datetime.now()}] Found {len(alerts)} top trader positions in last 24h")
    return alerts
