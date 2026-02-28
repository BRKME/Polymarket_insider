"""
Backtest Engine — Empirical Validation of Signal Quality

Purpose: Falsify or validate the hypothesis that our signals generate alpha.

Data collection:
1. Fetch resolved markets from Polymarket API
2. Reconstruct historical signals using current detection logic
3. Calculate actual PnL per signal

Metrics:
- ROI per signal type (ALPHA, CONFLICT, INSIDER_ONLY, TOP_TRADER)
- ROI per feature (wallet_age, pre_event, bet_size, etc.)
- Conditional win rate
- Brier score (probability calibration)
- Log-loss
- Max drawdown
- Return distribution (fat tail check)

Output: Feature importance ranking + calibration curves
"""

import json
import requests
import time
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path
import statistics

from config import GAMMA_API_URL, DATA_API_URL, REQUEST_DELAY


# ══════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════

@dataclass
class ResolvedMarket:
    condition_id: str
    question: str
    slug: str
    outcome: str  # "Yes" or "No"
    end_date: str
    resolved_at: str
    final_yes_price: float
    category: str
    volume: float


@dataclass
class HistoricalTrade:
    trade_hash: str
    wallet: str
    condition_id: str
    timestamp: int
    outcome: str  # position taken: "Yes" or "No"
    price: float
    size: float
    amount: float  # actual cost


@dataclass
class SignalResult:
    signal_type: str
    market_question: str
    condition_id: str
    trade_timestamp: int
    resolution_timestamp: int
    position: str  # "Yes" or "No"
    entry_price: float
    amount: float
    market_outcome: str  # actual result
    pnl: float
    roi: float
    features: Dict  # wallet_age, pre_event, bet_size, etc.
    is_winner: bool


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════

DB_PATH = Path("backtest.db")


def init_backtest_db():
    """Initialize SQLite database for backtest data."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS resolved_markets (
            condition_id TEXT PRIMARY KEY,
            question TEXT,
            slug TEXT,
            outcome TEXT,
            end_date TEXT,
            resolved_at TEXT,
            final_yes_price REAL,
            category TEXT,
            volume REAL,
            fetched_at TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS historical_trades (
            trade_hash TEXT PRIMARY KEY,
            wallet TEXT,
            condition_id TEXT,
            timestamp INTEGER,
            outcome TEXT,
            price REAL,
            size REAL,
            amount REAL,
            fetched_at TEXT,
            FOREIGN KEY (condition_id) REFERENCES resolved_markets(condition_id)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS signal_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT,
            market_question TEXT,
            condition_id TEXT,
            trade_timestamp INTEGER,
            resolution_timestamp INTEGER,
            position TEXT,
            entry_price REAL,
            amount REAL,
            market_outcome TEXT,
            pnl REAL,
            roi REAL,
            features TEXT,
            is_winner INTEGER,
            created_at TEXT
        )
    ''')
    
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_trades_condition 
        ON historical_trades(condition_id)
    ''')
    
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_trades_wallet 
        ON historical_trades(wallet)
    ''')
    
    c.execute('''
        CREATE INDEX IF NOT EXISTS idx_signals_type 
        ON signal_results(signal_type)
    ''')
    
    conn.commit()
    conn.close()
    print(f"[{datetime.now()}] Backtest database initialized: {DB_PATH}")


# ══════════════════════════════════════════════════════════════════
# DATA COLLECTION
# ══════════════════════════════════════════════════════════════════

def fetch_resolved_markets(days_back: int = 90, limit: int = 500) -> List[Dict]:
    """
    Fetch resolved markets from Polymarket API.
    Returns markets that have already resolved with known outcomes.
    """
    url = f"{GAMMA_API_URL}/markets"
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    
    params = {
        "limit": limit,
        "closed": "true",
        "order": "endDate",
        "_sort": "endDate:desc"
    }
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        markets = response.json()
        resolved = []
        
        for m in markets:
            # Skip if no resolution
            if not m.get('resolutionSource'):
                continue
            
            # Determine outcome
            outcomes = m.get('outcomes', [])
            outcome_prices = m.get('outcomePrices', [])
            
            if not outcomes or not outcome_prices:
                continue
            
            # Find winning outcome (price = 1.0 or close to it)
            winning_outcome = None
            for i, price in enumerate(outcome_prices):
                try:
                    if float(price) > 0.95:
                        winning_outcome = outcomes[i] if i < len(outcomes) else None
                        break
                except:
                    continue
            
            if not winning_outcome:
                continue
            
            resolved.append({
                'condition_id': m.get('conditionId', ''),
                'question': m.get('question', ''),
                'slug': m.get('slug', ''),
                'outcome': winning_outcome,
                'end_date': m.get('endDate', ''),
                'resolved_at': m.get('resolutionSource', ''),
                'final_yes_price': float(outcome_prices[0]) if outcome_prices else 0,
                'category': classify_market_category(m.get('question', '')),
                'volume': float(m.get('volume', 0) or 0)
            })
        
        print(f"[{datetime.now()}] Fetched {len(resolved)} resolved markets")
        return resolved
        
    except Exception as e:
        print(f"[{datetime.now()}] Error fetching resolved markets: {e}")
        return []


def classify_market_category(question: str) -> str:
    """Simple category classification for analysis."""
    q = question.lower()
    
    if any(w in q for w in ['trump', 'biden', 'election', 'president', 'congress', 'senate']):
        return 'politics'
    if any(w in q for w in ['war', 'strike', 'military', 'iran', 'russia', 'ukraine', 'china']):
        return 'geopolitics'
    if any(w in q for w in ['bitcoin', 'ethereum', 'crypto', 'btc', 'eth', 'price']):
        return 'crypto'
    if any(w in q for w in ['nba', 'nfl', 'mlb', 'sports', 'game', 'match']):
        return 'sports'
    if any(w in q for w in ['fed', 'rate', 'inflation', 'gdp', 'recession']):
        return 'macro'
    
    return 'other'


def fetch_trades_for_market(condition_id: str, min_amount: float = 1000) -> List[Dict]:
    """
    Fetch historical trades for a specific market.
    Filter by minimum amount to focus on significant trades.
    """
    url = f"{DATA_API_URL}/trades"
    
    all_trades = []
    offset = 0
    limit = 500
    max_pages = 10
    
    for page in range(max_pages):
        params = {
            "conditionId": condition_id,
            "limit": limit,
            "offset": offset,
            "sortBy": "TIMESTAMP",
            "sortDirection": "ASC"
        }
        
        try:
            time.sleep(REQUEST_DELAY)
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            trades = response.json()
            if not trades:
                break
            
            for t in trades:
                size = float(t.get('size', 0))
                price = float(t.get('price', 0))
                outcome = t.get('outcome', 'Yes')
                
                # Calculate actual amount
                if outcome.lower() == 'no':
                    amount = size * (1 - price)
                else:
                    amount = size * price
                
                if amount >= min_amount:
                    all_trades.append({
                        'trade_hash': t.get('transactionHash', ''),
                        'wallet': t.get('proxyWallet', ''),
                        'condition_id': condition_id,
                        'timestamp': t.get('timestamp', 0),
                        'outcome': outcome,
                        'price': price,
                        'size': size,
                        'amount': amount
                    })
            
            if len(trades) < limit:
                break
            
            offset += limit
            
        except Exception as e:
            print(f"[{datetime.now()}] Error fetching trades for {condition_id[:12]}...: {e}")
            break
    
    return all_trades


def save_resolved_market(market: Dict):
    """Save resolved market to database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        INSERT OR REPLACE INTO resolved_markets 
        (condition_id, question, slug, outcome, end_date, resolved_at, 
         final_yes_price, category, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        market['condition_id'],
        market['question'],
        market['slug'],
        market['outcome'],
        market['end_date'],
        market['resolved_at'],
        market['final_yes_price'],
        market['category'],
        market['volume'],
        datetime.now(timezone.utc).isoformat()
    ))
    
    conn.commit()
    conn.close()


def save_historical_trade(trade: Dict):
    """Save historical trade to database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        INSERT OR REPLACE INTO historical_trades
        (trade_hash, wallet, condition_id, timestamp, outcome, price, size, amount, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        trade['trade_hash'],
        trade['wallet'],
        trade['condition_id'],
        trade['timestamp'],
        trade['outcome'],
        trade['price'],
        trade['size'],
        trade['amount'],
        datetime.now(timezone.utc).isoformat()
    ))
    
    conn.commit()
    conn.close()


def collect_backtest_data(days_back: int = 90, min_volume: float = 10000):
    """
    Main data collection function.
    Fetches resolved markets and their trades.
    """
    init_backtest_db()
    
    print(f"[{datetime.now()}] Starting backtest data collection...")
    print(f"[{datetime.now()}] Parameters: days_back={days_back}, min_volume=${min_volume:,.0f}")
    
    # Fetch resolved markets
    markets = fetch_resolved_markets(days_back=days_back)
    
    # Filter by volume
    markets = [m for m in markets if m['volume'] >= min_volume]
    print(f"[{datetime.now()}] {len(markets)} markets with volume >= ${min_volume:,.0f}")
    
    # Save markets and fetch trades
    total_trades = 0
    
    for idx, market in enumerate(markets):
        print(f"[{datetime.now()}] [{idx+1}/{len(markets)}] {market['question'][:60]}...")
        
        save_resolved_market(market)
        
        trades = fetch_trades_for_market(market['condition_id'])
        for trade in trades:
            save_historical_trade(trade)
        
        total_trades += len(trades)
        print(f"  → {len(trades)} significant trades")
        
        # Rate limiting
        time.sleep(0.5)
    
    print(f"[{datetime.now()}] Collection complete: {len(markets)} markets, {total_trades} trades")


# ══════════════════════════════════════════════════════════════════
# SIGNAL RECONSTRUCTION
# ══════════════════════════════════════════════════════════════════

def get_wallet_features_at_time(wallet: str, timestamp: int, conn: sqlite3.Connection) -> Dict:
    """
    Reconstruct wallet features as they would have appeared at trade time.
    """
    c = conn.cursor()
    
    # Count trades before this timestamp
    c.execute('''
        SELECT COUNT(*), MIN(timestamp)
        FROM historical_trades
        WHERE wallet = ? AND timestamp < ?
    ''', (wallet, timestamp))
    
    row = c.fetchone()
    prior_trades = row[0] if row else 0
    first_trade_ts = row[1] if row and row[1] else timestamp
    
    # Wallet age in days
    wallet_age_days = (timestamp - first_trade_ts) / 86400 if first_trade_ts else 0
    
    return {
        'wallet_age_days': wallet_age_days,
        'prior_trades': prior_trades,
        'is_new_wallet': wallet_age_days < 7,
        'is_very_new_wallet': wallet_age_days < 3,
        'is_low_activity': prior_trades < 5
    }


def calculate_pre_event_latency(trade_timestamp: int, end_date_str: str) -> Optional[Dict]:
    """Calculate latency between trade and market resolution."""
    if not end_date_str:
        return None
    
    try:
        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
        trade_time = datetime.fromtimestamp(trade_timestamp, tz=timezone.utc)
        
        latency_seconds = (end_date - trade_time).total_seconds()
        
        if latency_seconds > 0:  # Trade before resolution
            return {
                'latency_seconds': latency_seconds,
                'latency_minutes': latency_seconds / 60,
                'latency_hours': latency_seconds / 3600,
                'is_pre_event': latency_seconds < 86400  # Within 24h
            }
    except:
        pass
    
    return None


def reconstruct_signal_features(trade: Dict, market: Dict, conn: sqlite3.Connection) -> Dict:
    """
    Reconstruct all features that would have been used to score this trade.
    """
    wallet_features = get_wallet_features_at_time(
        trade['wallet'], 
        trade['timestamp'],
        conn
    )
    
    latency = calculate_pre_event_latency(trade['timestamp'], market['end_date'])
    
    # Price/odds features
    price = trade['price']
    outcome = trade['outcome']
    
    if outcome.lower() == 'no':
        effective_odds = 1 - price
    else:
        effective_odds = price
    
    return {
        # Wallet features
        'wallet_age_days': wallet_features['wallet_age_days'],
        'prior_trades': wallet_features['prior_trades'],
        'is_new_wallet': wallet_features['is_new_wallet'],
        'is_very_new_wallet': wallet_features['is_very_new_wallet'],
        'is_low_activity': wallet_features['is_low_activity'],
        
        # Trade features
        'amount': trade['amount'],
        'is_large_bet': trade['amount'] >= 5000,
        'is_very_large_bet': trade['amount'] >= 10000,
        'effective_odds': effective_odds,
        'is_longshot': effective_odds < 0.15,
        'is_contrarian': effective_odds < 0.10,
        
        # Timing features
        'has_pre_event': latency is not None and latency.get('is_pre_event', False),
        'latency_hours': latency['latency_hours'] if latency else None,
        
        # Market features
        'category': market['category'],
        'volume': market['volume']
    }


def classify_signal_type(features: Dict) -> str:
    """
    Classify signal type based on features.
    Simplified version of live detection logic.
    """
    score = 0
    
    # Wallet age scoring
    if features['is_very_new_wallet']:
        score += 40
    elif features['is_new_wallet']:
        score += 20
    
    # Activity scoring
    if features['is_low_activity']:
        score += 10
    
    # Bet size scoring
    if features['is_large_bet']:
        score += 20
    
    # Contrarian scoring
    if features['is_contrarian']:
        score += 25
    
    # Pre-event scoring
    if features['has_pre_event']:
        if features['latency_hours'] and features['latency_hours'] < 1:
            score += 50
        elif features['latency_hours'] and features['latency_hours'] < 4:
            score += 30
        else:
            score += 15
    
    # Determine type
    if score >= 100 and features['is_longshot']:
        return 'ALPHA'
    elif score >= 80:
        return 'INSIDER_CONFIRMED'
    elif score >= 70:
        return 'CONFLICT'
    elif score >= 50:
        return 'INSIDER_ONLY'
    else:
        return 'NO_SIGNAL'


def calculate_trade_pnl(trade: Dict, market_outcome: str) -> Tuple[float, float, bool]:
    """
    Calculate PnL for a trade given market outcome.
    
    Returns: (pnl, roi, is_winner)
    """
    position = trade['outcome']
    amount = trade['amount']
    price = trade['price']
    
    # Determine if trade won
    position_lower = position.lower()
    outcome_lower = market_outcome.lower()
    
    is_winner = position_lower == outcome_lower
    
    if position_lower == 'no':
        effective_price = 1 - price
    else:
        effective_price = price
    
    if is_winner:
        # Win: get $1 per token, paid effective_price per token
        tokens = amount / effective_price
        pnl = tokens - amount  # tokens * $1 - cost
    else:
        # Lose: lose entire amount
        pnl = -amount
    
    roi = pnl / amount if amount > 0 else 0
    
    return pnl, roi, is_winner


def run_backtest():
    """
    Main backtest function.
    Reconstructs signals from historical data and calculates performance.
    """
    init_backtest_db()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get all resolved markets
    c.execute('SELECT * FROM resolved_markets')
    markets_rows = c.fetchall()
    
    if not markets_rows:
        print(f"[{datetime.now()}] No resolved markets in database. Run collect_backtest_data() first.")
        conn.close()
        return
    
    print(f"[{datetime.now()}] Running backtest on {len(markets_rows)} markets...")
    
    results = []
    
    for market_row in markets_rows:
        market = {
            'condition_id': market_row[0],
            'question': market_row[1],
            'slug': market_row[2],
            'outcome': market_row[3],
            'end_date': market_row[4],
            'resolved_at': market_row[5],
            'final_yes_price': market_row[6],
            'category': market_row[7],
            'volume': market_row[8]
        }
        
        # Get trades for this market
        c.execute('''
            SELECT * FROM historical_trades
            WHERE condition_id = ?
            ORDER BY timestamp ASC
        ''', (market['condition_id'],))
        
        trades_rows = c.fetchall()
        
        for trade_row in trades_rows:
            trade = {
                'trade_hash': trade_row[0],
                'wallet': trade_row[1],
                'condition_id': trade_row[2],
                'timestamp': trade_row[3],
                'outcome': trade_row[4],
                'price': trade_row[5],
                'size': trade_row[6],
                'amount': trade_row[7]
            }
            
            # Reconstruct features
            features = reconstruct_signal_features(trade, market, conn)
            
            # Classify signal
            signal_type = classify_signal_type(features)
            
            if signal_type == 'NO_SIGNAL':
                continue
            
            # Calculate PnL
            pnl, roi, is_winner = calculate_trade_pnl(trade, market['outcome'])
            
            # Parse resolution timestamp
            try:
                end_dt = datetime.fromisoformat(market['end_date'].replace('Z', '+00:00'))
                resolution_ts = int(end_dt.timestamp())
            except:
                resolution_ts = 0
            
            result = SignalResult(
                signal_type=signal_type,
                market_question=market['question'],
                condition_id=market['condition_id'],
                trade_timestamp=trade['timestamp'],
                resolution_timestamp=resolution_ts,
                position=trade['outcome'],
                entry_price=trade['price'],
                amount=trade['amount'],
                market_outcome=market['outcome'],
                pnl=pnl,
                roi=roi,
                features=features,
                is_winner=is_winner
            )
            
            results.append(result)
            
            # Save to database
            c.execute('''
                INSERT INTO signal_results
                (signal_type, market_question, condition_id, trade_timestamp,
                 resolution_timestamp, position, entry_price, amount, market_outcome,
                 pnl, roi, features, is_winner, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                result.signal_type,
                result.market_question,
                result.condition_id,
                result.trade_timestamp,
                result.resolution_timestamp,
                result.position,
                result.entry_price,
                result.amount,
                result.market_outcome,
                result.pnl,
                result.roi,
                json.dumps(result.features),
                1 if result.is_winner else 0,
                datetime.now(timezone.utc).isoformat()
            ))
    
    conn.commit()
    conn.close()
    
    print(f"[{datetime.now()}] Backtest complete: {len(results)} signals analyzed")
    return results


# ══════════════════════════════════════════════════════════════════
# ANALYSIS & METRICS
# ══════════════════════════════════════════════════════════════════

def calculate_metrics() -> Dict:
    """
    Calculate comprehensive backtest metrics.
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('SELECT * FROM signal_results')
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        return {'error': 'No signal results found'}
    
    # Parse results
    results = []
    for row in rows:
        results.append({
            'signal_type': row[1],
            'pnl': row[10],
            'roi': row[11],
            'features': json.loads(row[12]) if row[12] else {},
            'is_winner': bool(row[13]),
            'amount': row[8]
        })
    
    # Overall metrics
    total_signals = len(results)
    total_pnl = sum(r['pnl'] for r in results)
    total_invested = sum(r['amount'] for r in results)
    overall_roi = total_pnl / total_invested if total_invested > 0 else 0
    win_rate = sum(1 for r in results if r['is_winner']) / total_signals if total_signals > 0 else 0
    
    # Per signal type
    by_type = {}
    for signal_type in ['ALPHA', 'INSIDER_CONFIRMED', 'CONFLICT', 'INSIDER_ONLY']:
        type_results = [r for r in results if r['signal_type'] == signal_type]
        if type_results:
            type_pnl = sum(r['pnl'] for r in type_results)
            type_invested = sum(r['amount'] for r in type_results)
            type_wins = sum(1 for r in type_results if r['is_winner'])
            
            by_type[signal_type] = {
                'count': len(type_results),
                'total_pnl': type_pnl,
                'roi': type_pnl / type_invested if type_invested > 0 else 0,
                'win_rate': type_wins / len(type_results),
                'avg_pnl': type_pnl / len(type_results)
            }
    
    # Per feature (feature importance)
    feature_performance = {}
    
    binary_features = [
        'is_new_wallet', 'is_very_new_wallet', 'is_low_activity',
        'is_large_bet', 'is_very_large_bet', 'is_longshot', 
        'is_contrarian', 'has_pre_event'
    ]
    
    for feature in binary_features:
        with_feature = [r for r in results if r['features'].get(feature, False)]
        without_feature = [r for r in results if not r['features'].get(feature, False)]
        
        if with_feature and without_feature:
            roi_with = sum(r['pnl'] for r in with_feature) / sum(r['amount'] for r in with_feature)
            roi_without = sum(r['pnl'] for r in without_feature) / sum(r['amount'] for r in without_feature)
            
            feature_performance[feature] = {
                'count_with': len(with_feature),
                'count_without': len(without_feature),
                'roi_with': roi_with,
                'roi_without': roi_without,
                'lift': roi_with - roi_without,
                'is_significant': abs(roi_with - roi_without) > 0.05
            }
    
    # Drawdown calculation
    cumulative_pnl = []
    running_pnl = 0
    for r in sorted(results, key=lambda x: x.get('trade_timestamp', 0)):
        running_pnl += r['pnl']
        cumulative_pnl.append(running_pnl)
    
    max_drawdown = 0
    peak = 0
    for pnl in cumulative_pnl:
        if pnl > peak:
            peak = pnl
        drawdown = peak - pnl
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    # ROI distribution
    rois = [r['roi'] for r in results]
    
    return {
        'total_signals': total_signals,
        'total_pnl': total_pnl,
        'total_invested': total_invested,
        'overall_roi': overall_roi,
        'win_rate': win_rate,
        'max_drawdown': max_drawdown,
        'by_signal_type': by_type,
        'feature_importance': feature_performance,
        'roi_distribution': {
            'min': min(rois) if rois else 0,
            'max': max(rois) if rois else 0,
            'mean': statistics.mean(rois) if rois else 0,
            'median': statistics.median(rois) if rois else 0,
            'stdev': statistics.stdev(rois) if len(rois) > 1 else 0
        }
    }


def print_backtest_report():
    """Print formatted backtest report."""
    metrics = calculate_metrics()
    
    if 'error' in metrics:
        print(f"Error: {metrics['error']}")
        return
    
    print("\n" + "=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)
    
    print(f"\n📊 OVERALL PERFORMANCE")
    print(f"   Total signals: {metrics['total_signals']}")
    print(f"   Total invested: ${metrics['total_invested']:,.0f}")
    print(f"   Total PnL: ${metrics['total_pnl']:,.0f}")
    print(f"   Overall ROI: {metrics['overall_roi']*100:+.1f}%")
    print(f"   Win rate: {metrics['win_rate']*100:.1f}%")
    print(f"   Max drawdown: ${metrics['max_drawdown']:,.0f}")
    
    print(f"\n📈 BY SIGNAL TYPE")
    for signal_type, data in metrics['by_signal_type'].items():
        print(f"\n   {signal_type}:")
        print(f"      Count: {data['count']}")
        print(f"      ROI: {data['roi']*100:+.1f}%")
        print(f"      Win rate: {data['win_rate']*100:.1f}%")
        print(f"      Avg PnL: ${data['avg_pnl']:,.0f}")
    
    print(f"\n🔬 FEATURE IMPORTANCE (by ROI lift)")
    sorted_features = sorted(
        metrics['feature_importance'].items(),
        key=lambda x: abs(x[1]['lift']),
        reverse=True
    )
    
    for feature, data in sorted_features:
        significance = "✓" if data['is_significant'] else " "
        print(f"   {significance} {feature}:")
        print(f"      With: {data['roi_with']*100:+.1f}% ({data['count_with']} trades)")
        print(f"      Without: {data['roi_without']*100:+.1f}% ({data['count_without']} trades)")
        print(f"      Lift: {data['lift']*100:+.1f}%")
    
    print(f"\n📉 ROI DISTRIBUTION")
    dist = metrics['roi_distribution']
    print(f"   Min: {dist['min']*100:+.1f}%")
    print(f"   Max: {dist['max']*100:+.1f}%")
    print(f"   Mean: {dist['mean']*100:+.1f}%")
    print(f"   Median: {dist['median']*100:+.1f}%")
    print(f"   StdDev: {dist['stdev']*100:.1f}%")
    
    print("\n" + "=" * 60)
    
    # Verdict
    if metrics['overall_roi'] > 0.10:
        print("✅ VERDICT: Positive edge detected. Continue to probabilistic modeling.")
    elif metrics['overall_roi'] > 0:
        print("⚠️ VERDICT: Weak edge. Review feature importance, tighten filters.")
    else:
        print("❌ VERDICT: No edge. Hypothesis falsified. Review methodology.")
    
    print("=" * 60 + "\n")


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python backtest.py [collect|run|report]")
        print("  collect - Fetch resolved markets and trades")
        print("  run     - Run backtest on collected data")
        print("  report  - Print backtest metrics")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "collect":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        collect_backtest_data(days_back=days)
    
    elif command == "run":
        run_backtest()
    
    elif command == "report":
        print_backtest_report()
    
    else:
        print(f"Unknown command: {command}")
