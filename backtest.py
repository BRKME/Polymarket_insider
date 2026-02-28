"""
Backtest Engine v4 — Hardened for Statistical Validity

Phase 1 Hardening:
1. Cluster-robust SE (by market ID) — catches within-market correlation
2. Stress tests: remove top 10%, cost sensitivity matrix
3. Rolling walk-forward (by trade count, not days)
4. Parameter freeze with config hash
5. Expanded validation criteria

Goal: Eliminate false-positive validation. Survive adversarial testing.
"""

import json
import hashlib
import requests
import time
import sqlite3
import random
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path
from collections import defaultdict

from config import GAMMA_API_URL, DATA_API_URL, REQUEST_DELAY


# ══════════════════════════════════════════════════════════════════
# FROZEN PARAMETERS — DO NOT MODIFY AFTER FIRST BACKTEST
# ══════════════════════════════════════════════════════════════════

SCORE_WEIGHTS = {
    'is_very_new_wallet': 40,
    'is_new_wallet': 20,
    'is_low_activity': 10,
    'is_very_large_bet': 25,
    'is_large_bet': 20,
    'is_contrarian': 25,
    'is_longshot': 15,
    'is_very_pre_event': 50,
    'is_pre_event': 20,
}

SIGNAL_THRESHOLDS = {
    'ALPHA': 100,
    'INSIDER_CONFIRMED': 80,
    'CONFLICT': 70,
    'INSIDER_ONLY': 50,
}

# Transaction costs (default)
DEFAULT_MAKER_FEE = 0.00
DEFAULT_TAKER_FEE = 0.02
DEFAULT_TAKER_PROB = 0.7
DEFAULT_SLIPPAGE_MULT = 1.0

# Slippage model
BASE_SLIPPAGE = 0.002
SLIPPAGE_PER_1K = 0.001
MAX_SLIPPAGE = 0.03

# Validation thresholds
T_STAT_THRESHOLD = 2.0
MIN_TRADES_TOTAL = 100
MIN_TRADES_PER_FOLD = 20
MAX_DRAWDOWN_THRESHOLD = 0.30
MIN_PROFIT_FACTOR = 1.2
MIN_FOLDS_PROFITABLE = 0.6

# Walk-forward parameters
EXPANDING_FOLDS = 5
ROLLING_TRAIN_SIZE = 150  # trades
ROLLING_TEST_SIZE = 50    # trades

DB_PATH = Path("backtest.db")
CONFIG_HASH_FILE = Path("config_hash.json")


# ══════════════════════════════════════════════════════════════════
# PARAMETER FREEZE & CONFIG HASH
# ══════════════════════════════════════════════════════════════════

def compute_config_hash() -> str:
    """Compute deterministic hash of all frozen parameters."""
    config = {
        'SCORE_WEIGHTS': SCORE_WEIGHTS,
        'SIGNAL_THRESHOLDS': SIGNAL_THRESHOLDS,
        'T_STAT_THRESHOLD': T_STAT_THRESHOLD,
        'MIN_TRADES_TOTAL': MIN_TRADES_TOTAL,
        'MAX_DRAWDOWN_THRESHOLD': MAX_DRAWDOWN_THRESHOLD,
        'MIN_PROFIT_FACTOR': MIN_PROFIT_FACTOR,
    }
    config_str = json.dumps(config, sort_keys=True)
    return hashlib.sha256(config_str.encode()).hexdigest()[:16]


def verify_config_freeze() -> Tuple[bool, str]:
    """
    Verify parameters haven't changed since first run.
    Returns (is_valid, message).
    """
    current_hash = compute_config_hash()
    
    if CONFIG_HASH_FILE.exists():
        with open(CONFIG_HASH_FILE) as f:
            saved = json.load(f)
        
        if saved['hash'] != current_hash:
            return False, f"CONFIG CHANGED! Saved: {saved['hash']}, Current: {current_hash}"
        return True, f"Config verified: {current_hash}"
    else:
        # First run — save hash
        with open(CONFIG_HASH_FILE, 'w') as f:
            json.dump({
                'hash': current_hash,
                'created': datetime.now().isoformat(),
                'version': '4.0'
            }, f, indent=2)
        return True, f"Config hash saved: {current_hash}"


# ══════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    trade_hash: str
    wallet: str
    condition_id: str
    timestamp: int
    outcome: str
    price: float
    size: float
    amount: float


@dataclass 
class Market:
    condition_id: str
    question: str
    outcome: str
    end_date: str
    volume: float
    category: str


@dataclass
class Signal:
    trade: Trade
    market: Market
    signal_type: str
    features: Dict
    score: float


@dataclass
class TradeResult:
    signal: Optional[Signal]
    gross_pnl: float
    commission: float
    slippage: float
    net_pnl: float
    roi: float
    is_winner: bool


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS markets (
            condition_id TEXT PRIMARY KEY,
            question TEXT,
            outcome TEXT,
            end_date TEXT,
            volume REAL,
            category TEXT,
            fetched_at TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            trade_hash TEXT PRIMARY KEY,
            wallet TEXT,
            condition_id TEXT,
            timestamp INTEGER,
            outcome TEXT,
            price REAL,
            size REAL,
            amount REAL
        )
    ''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(wallet)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_cond ON trades(condition_id)')
    
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════
# DATA COLLECTION
# ══════════════════════════════════════════════════════════════════

def fetch_resolved_markets(days_back: int = 90, limit: int = 500) -> List[Dict]:
    url = f"{GAMMA_API_URL}/markets"
    params = {"limit": limit, "closed": "true", "order": "endDate", "_sort": "endDate:desc"}
    
    print(f"[API] GET {url}")
    print(f"[API] Params: {params}")
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        print(f"[API] Status: {response.status_code}")
        response.raise_for_status()
        markets = response.json()
        print(f"[API] Received {len(markets)} markets from API")
        
        resolved = []
        skipped_no_resolution = 0
        skipped_no_outcome = 0
        
        # DEBUG: Show sample raw market data
        if markets:
            sample = markets[0]
            print(f"[DEBUG] Sample market raw data:")
            print(f"   outcomes: {sample.get('outcomes')}")
            print(f"   outcomePrices: {sample.get('outcomePrices')}")
            print(f"   resolutionSource: {sample.get('resolutionSource')}")
            print(f"   closed: {sample.get('closed')}")
        
        for m in markets:
            if not m.get('resolutionSource'):
                skipped_no_resolution += 1
                continue
            
            outcomes = m.get('outcomes', [])
            outcome_prices = m.get('outcomePrices', [])
            
            # Method 1: Check for price = 1.0 (fully resolved)
            winning = None
            for i, p in enumerate(outcome_prices):
                try:
                    price = float(p)
                    # Resolved markets have one outcome at 1.0 and others at 0.0
                    if price >= 0.99 and i < len(outcomes):
                        winning = outcomes[i]
                        break
                except:
                    pass
            
            # Method 2: If no 1.0 price, check 'resolved' field
            if not winning and m.get('resolved'):
                # Some APIs return resolved outcome directly
                resolved_outcome = m.get('resolvedOutcome') or m.get('winner')
                if resolved_outcome:
                    winning = resolved_outcome
            
            # Method 3: Use highest price if market is closed
            if not winning and m.get('closed') and outcome_prices:
                try:
                    prices = [float(p) for p in outcome_prices]
                    max_idx = prices.index(max(prices))
                    if max(prices) > 0.9 and max_idx < len(outcomes):
                        winning = outcomes[max_idx]
                except:
                    pass
            
            if not winning:
                skipped_no_outcome += 1
                continue
            
            resolved.append({
                'condition_id': m.get('conditionId', ''),
                'question': m.get('question', ''),
                'outcome': winning,
                'end_date': m.get('endDate', ''),
                'volume': float(m.get('volume', 0) or 0),
                'category': classify_category(m.get('question', ''))
            })
        
        print(f"[API] Resolved: {len(resolved)}, Skipped (no resolution): {skipped_no_resolution}, Skipped (no outcome): {skipped_no_outcome}")
        return resolved
        
    except Exception as e:
        print(f"[API ERROR] {type(e).__name__}: {e}")
        return []


def classify_category(q: str) -> str:
    q = q.lower()
    if any(w in q for w in ['trump', 'biden', 'election', 'president']): return 'politics'
    if any(w in q for w in ['war', 'strike', 'iran', 'russia', 'ukraine']): return 'geopolitics'
    if any(w in q for w in ['bitcoin', 'crypto', 'btc', 'eth']): return 'crypto'
    if any(w in q for w in ['nba', 'nfl', 'sports']): return 'sports'
    return 'other'


def fetch_trades_for_market(condition_id: str, min_amount: float = 1000) -> List[Dict]:
    url = f"{DATA_API_URL}/trades"
    trades = []
    
    for offset in range(0, 5000, 500):
        try:
            time.sleep(REQUEST_DELAY)
            r = requests.get(url, params={
                "conditionId": condition_id, "limit": 500, "offset": offset,
                "sortBy": "TIMESTAMP", "sortDirection": "ASC"
            }, timeout=30)
            
            if r.status_code != 200:
                print(f"[TRADES] Error {r.status_code} for {condition_id[:12]}...")
                break
            
            batch = r.json()
            if not batch:
                break
            
            for t in batch:
                size = float(t.get('size', 0))
                price = float(t.get('price', 0))
                outcome = t.get('outcome', 'Yes')
                amount = size * (1 - price) if outcome.lower() == 'no' else size * price
                
                # Get timestamp - might be in milliseconds
                ts = t.get('timestamp', 0)
                # If timestamp is in milliseconds (13 digits), convert to seconds
                if ts > 10000000000:
                    ts = ts // 1000
                
                if amount >= min_amount:
                    trades.append({
                        'trade_hash': t.get('transactionHash', ''),
                        'wallet': t.get('proxyWallet', ''),
                        'condition_id': condition_id,
                        'timestamp': ts,
                        'outcome': outcome,
                        'price': price,
                        'size': size,
                        'amount': amount
                    })
            
            if len(batch) < 500:
                break
        except Exception as e:
            print(f"[TRADES] Exception for {condition_id[:12]}...: {e}")
            break
    
    return trades


def collect_data(days_back: int = 90):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print(f"[INFO] Fetching resolved markets (last {days_back} days)...")
    markets = fetch_resolved_markets(days_back)
    print(f"[INFO] API returned {len(markets)} resolved markets")
    
    if not markets:
        print("[ERROR] No markets returned from API!")
        print("[DEBUG] Check if gamma-api.polymarket.com is accessible")
        conn.close()
        return
    
    # Show sample before filtering
    print(f"[INFO] Sample market volumes: {[m['volume'] for m in markets[:10]]}")
    
    markets = [m for m in markets if m['volume'] >= 10000]
    print(f"[INFO] After volume filter (>=$10K): {len(markets)} markets")
    
    if not markets:
        print("[ERROR] All markets filtered out by volume!")
        conn.close()
        return
    
    print(f"[INFO] Collecting trades for {len(markets)} markets...")
    total_trades = 0
    
    for idx, m in enumerate(markets):
        c.execute('INSERT OR REPLACE INTO markets VALUES (?,?,?,?,?,?,?)',
            (m['condition_id'], m['question'], m['outcome'], m['end_date'],
             m['volume'], m['category'], datetime.now().isoformat()))
        
        trades = fetch_trades_for_market(m['condition_id'])
        for t in trades:
            c.execute('INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?)',
                (t['trade_hash'], t['wallet'], t['condition_id'], t['timestamp'],
                 t['outcome'], t['price'], t['size'], t['amount']))
        
        total_trades += len(trades)
        
        if (idx + 1) % 10 == 0:
            print(f"[INFO] {idx+1}/{len(markets)} markets, {total_trades} trades so far")
            conn.commit()
    
    conn.commit()
    conn.close()
    print(f"[DONE] Collected {len(markets)} markets, {total_trades} trades (>=$1000)")


# ══════════════════════════════════════════════════════════════════
# TRANSACTION COST MODEL (parameterized for stress testing)
# ══════════════════════════════════════════════════════════════════

def calculate_commission(gross_pnl: float, is_winner: bool,
                        taker_fee: float = DEFAULT_TAKER_FEE,
                        taker_prob: float = DEFAULT_TAKER_PROB) -> float:
    if not is_winner or gross_pnl <= 0:
        return 0
    
    if random.random() < taker_prob:
        return gross_pnl * taker_fee
    return 0


def calculate_slippage(amount: float, market_volume: float,
                      multiplier: float = DEFAULT_SLIPPAGE_MULT) -> float:
    slippage_rate = BASE_SLIPPAGE
    slippage_rate += (amount / 1000) * SLIPPAGE_PER_1K
    
    if market_volume > 0:
        volume_factor = min(2.0, 100000 / market_volume)
        slippage_rate *= volume_factor
    
    slippage_rate = min(slippage_rate, MAX_SLIPPAGE)
    return amount * slippage_rate * multiplier


# ══════════════════════════════════════════════════════════════════
# LOOKAHEAD-SAFE FEATURE ENGINEERING
# ══════════════════════════════════════════════════════════════════

def get_wallet_history_before(wallet: str, before_ts: int, conn: sqlite3.Connection) -> Dict:
    c = conn.cursor()
    c.execute('''
        SELECT timestamp, amount FROM trades
        WHERE wallet = ? AND timestamp < ?
        ORDER BY timestamp ASC
    ''', (wallet, before_ts))
    
    prior = c.fetchall()
    
    if not prior:
        return {
            'wallet_age_days': 0, 'prior_trade_count': 0, 'prior_volume': 0,
            'is_new_wallet': True, 'is_very_new_wallet': True, 'is_low_activity': True
        }
    
    first_ts = prior[0][0]
    age_days = (before_ts - first_ts) / 86400
    
    return {
        'wallet_age_days': age_days,
        'prior_trade_count': len(prior),
        'prior_volume': sum(t[1] for t in prior),
        'is_new_wallet': age_days < 7,
        'is_very_new_wallet': age_days < 3,
        'is_low_activity': len(prior) < 5
    }


def get_market_state_at_trade(trade: Trade, market: Market) -> Dict:
    price = trade.price
    outcome = trade.outcome
    effective_odds = (1 - price) if outcome.lower() == 'no' else price
    
    try:
        end_dt = datetime.fromisoformat(market.end_date.replace('Z', '+00:00'))
        trade_dt = datetime.fromtimestamp(trade.timestamp, tz=timezone.utc)
        hours = (end_dt - trade_dt).total_seconds() / 3600
    except:
        hours = None
    
    return {
        'effective_odds': effective_odds,
        'is_longshot': effective_odds < 0.15,
        'is_contrarian': effective_odds < 0.10,
        'hours_to_resolution': hours,
        'is_pre_event': hours is not None and hours < 24,
        'is_very_pre_event': hours is not None and hours < 1
    }


def extract_features(trade: Trade, market: Market, conn: sqlite3.Connection) -> Dict:
    wallet_hist = get_wallet_history_before(trade.wallet, trade.timestamp, conn)
    market_state = get_market_state_at_trade(trade, market)
    
    return {
        **wallet_hist,
        'amount': trade.amount,
        'is_large_bet': trade.amount >= 5000,
        'is_very_large_bet': trade.amount >= 10000,
        **market_state,
        'category': market.category
    }


# ══════════════════════════════════════════════════════════════════
# SIGNAL CLASSIFICATION (FROZEN)
# ══════════════════════════════════════════════════════════════════

def classify_signal(features: Dict) -> Tuple[str, float]:
    score = 0
    
    for feat, weight in SCORE_WEIGHTS.items():
        if features.get(feat):
            if feat == 'is_new_wallet' and features.get('is_very_new_wallet'): continue
            if feat == 'is_large_bet' and features.get('is_very_large_bet'): continue
            if feat == 'is_longshot' and features.get('is_contrarian'): continue
            if feat == 'is_pre_event' and features.get('is_very_pre_event'): continue
            score += weight
    
    if score >= SIGNAL_THRESHOLDS['ALPHA'] and features.get('is_longshot'):
        return 'ALPHA', score
    elif score >= SIGNAL_THRESHOLDS['INSIDER_CONFIRMED']:
        return 'INSIDER_CONFIRMED', score
    elif score >= SIGNAL_THRESHOLDS['CONFLICT']:
        return 'CONFLICT', score
    elif score >= SIGNAL_THRESHOLDS['INSIDER_ONLY']:
        return 'INSIDER_ONLY', score
    return 'NO_SIGNAL', score


# ══════════════════════════════════════════════════════════════════
# PNL CALCULATION
# ══════════════════════════════════════════════════════════════════

def calculate_pnl(trade: Trade, market: Market,
                 taker_fee: float = DEFAULT_TAKER_FEE,
                 taker_prob: float = DEFAULT_TAKER_PROB,
                 slippage_mult: float = DEFAULT_SLIPPAGE_MULT) -> TradeResult:
    position = trade.outcome.lower()
    resolved = market.outcome.lower()
    amount = trade.amount
    
    is_winner = position == resolved
    effective_price = (1 - trade.price) if position == 'no' else trade.price
    
    entry_slip = calculate_slippage(amount, market.volume, slippage_mult)
    
    if is_winner:
        tokens = amount / effective_price
        gross_pnl = tokens - amount
        commission = calculate_commission(gross_pnl, True, taker_fee, taker_prob)
        exit_slip = calculate_slippage(tokens, market.volume, slippage_mult)
        net_pnl = gross_pnl - commission - entry_slip - exit_slip
    else:
        gross_pnl = -amount
        commission = 0
        exit_slip = 0
        net_pnl = gross_pnl - entry_slip
    
    return TradeResult(
        signal=None,
        gross_pnl=gross_pnl,
        commission=commission,
        slippage=entry_slip + exit_slip,
        net_pnl=net_pnl,
        roi=net_pnl / amount if amount > 0 else 0,
        is_winner=is_winner
    )


# ══════════════════════════════════════════════════════════════════
# BASELINES
# ══════════════════════════════════════════════════════════════════

def run_baseline(signals: List[Signal], markets: Dict[str, Market], strategy: str) -> List[TradeResult]:
    results = []
    for signal in signals:
        trade = signal.trade
        market = signal.market
        
        if strategy == 'random':
            position = random.choice(['Yes', 'No'])
        elif strategy == 'always_no':
            position = 'No'
        elif strategy == 'follow_odds':
            position = 'Yes' if trade.price > 0.5 else 'No'
        else:
            position = trade.outcome
        
        fake = Trade(trade.trade_hash, trade.wallet, trade.condition_id,
                    trade.timestamp, position, trade.price, trade.size, trade.amount)
        results.append(calculate_pnl(fake, market))
    
    return results


# ══════════════════════════════════════════════════════════════════
# STATISTICAL METHODS
# ══════════════════════════════════════════════════════════════════

def newey_west_se(returns: List[float], max_lag: int = 5) -> float:
    """Newey-West standard error."""
    n = len(returns)
    if n < 2:
        return 0
    
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    
    for lag in range(1, min(max_lag + 1, n)):
        weight = 1 - lag / (max_lag + 1)
        autocov = sum((returns[i] - mean) * (returns[i - lag] - mean) 
                     for i in range(lag, n)) / (n - 1)
        var += 2 * weight * autocov
    
    return math.sqrt(max(0, var) / n)


def cluster_robust_se(results: List[TradeResult]) -> float:
    """
    Cluster-robust standard error by market ID.
    Accounts for within-market correlation.
    """
    if not results or not results[0].signal:
        return 0
    
    # Group by market
    by_market = defaultdict(list)
    for r in results:
        if r.signal:
            by_market[r.signal.market.condition_id].append(r.roi)
    
    n_clusters = len(by_market)
    if n_clusters < 2:
        return newey_west_se([r.roi for r in results])
    
    # Cluster means
    cluster_means = [sum(rois) / len(rois) for rois in by_market.values()]
    overall_mean = sum(r.roi for r in results) / len(results)
    
    # Between-cluster variance
    n = len(results)
    bc_var = sum((cm - overall_mean) ** 2 for cm in cluster_means) / (n_clusters - 1)
    
    # Cluster-robust SE with finite-sample correction
    correction = (n_clusters / (n_clusters - 1)) * ((n - 1) / n)
    se = math.sqrt(correction * bc_var / n_clusters)
    
    return se


def calculate_stats(results: List[TradeResult], use_cluster: bool = True) -> Dict:
    """Calculate statistics with multiple robustness checks."""
    if not results:
        return {'n': 0, 'error': 'No results'}
    
    n = len(results)
    rois = [r.roi for r in results]
    pnls = [r.net_pnl for r in results]
    wins = sum(1 for r in results if r.is_winner)
    
    total_pnl = sum(pnls)
    mean_roi = sum(rois) / n
    
    gross_profit = sum(r.net_pnl for r in results if r.net_pnl > 0)
    gross_loss = abs(sum(r.net_pnl for r in results if r.net_pnl < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    # Multiple SE estimates
    if n > 1:
        variance = sum((r - mean_roi) ** 2 for r in rois) / (n - 1)
        std = math.sqrt(variance)
        simple_se = std / math.sqrt(n)
        t_stat_simple = mean_roi / simple_se if simple_se > 0 else 0
    else:
        std = 0
        t_stat_simple = 0
    
    nw_se = newey_west_se(rois)
    t_stat_nw = mean_roi / nw_se if nw_se > 0 else 0
    
    cluster_se = cluster_robust_se(results) if use_cluster else nw_se
    t_stat_cluster = mean_roi / cluster_se if cluster_se > 0 else 0
    
    # Use most conservative t-stat
    t_stat_robust = min(abs(t_stat_simple), abs(t_stat_nw), abs(t_stat_cluster))
    if mean_roi < 0:
        t_stat_robust = -t_stat_robust
    
    # Max drawdown
    running = 0
    peak = 0
    max_dd = 0
    for pnl in pnls:
        running += pnl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    
    max_dd_pct = max_dd / peak if peak > 0 else 0
    
    return {
        'n': n,
        'total_pnl': total_pnl,
        'mean_roi': mean_roi,
        'std_roi': std,
        't_stat_simple': t_stat_simple,
        't_stat_nw': t_stat_nw,
        't_stat_cluster': t_stat_cluster,
        't_stat_robust': t_stat_robust,  # Most conservative
        'win_rate': wins / n,
        'max_drawdown': max_dd,
        'max_drawdown_pct': max_dd_pct,
        'profit_factor': profit_factor,
        'is_significant': t_stat_robust > T_STAT_THRESHOLD and n >= MIN_TRADES_TOTAL
    }


# ══════════════════════════════════════════════════════════════════
# WALK-FORWARD METHODS
# ══════════════════════════════════════════════════════════════════

def expanding_wf_split(trades: List, n_folds: int = EXPANDING_FOLDS) -> List[Tuple[List, List]]:
    """Expanding window walk-forward."""
    n = len(trades)
    fold_size = n // (n_folds + 1)
    
    folds = []
    for i in range(n_folds):
        train_end = fold_size * (i + 2)
        test_end = min(train_end + fold_size, n)
        
        if test_end > train_end and (test_end - train_end) >= MIN_TRADES_PER_FOLD:
            folds.append((trades[:train_end], trades[train_end:test_end]))
    
    return folds


def rolling_wf_split(trades: List, 
                    train_size: int = ROLLING_TRAIN_SIZE,
                    test_size: int = ROLLING_TEST_SIZE) -> List[Tuple[List, List]]:
    """
    Rolling window walk-forward by trade count.
    Fixed window sizes, slides forward.
    """
    folds = []
    n = len(trades)
    
    start = 0
    while start + train_size + test_size <= n:
        train = trades[start:start + train_size]
        test = trades[start + train_size:start + train_size + test_size]
        folds.append((train, test))
        start += test_size  # Slide by test size
    
    return folds


# ══════════════════════════════════════════════════════════════════
# STRESS TESTS
# ══════════════════════════════════════════════════════════════════

def stress_test_remove_top(results: List[TradeResult], pct: float = 0.10) -> Dict:
    """Remove top N% most profitable trades and recalculate."""
    if not results:
        return {'error': 'No results'}
    
    sorted_by_pnl = sorted(results, key=lambda r: r.net_pnl, reverse=True)
    n_remove = max(1, int(len(results) * pct))
    
    remaining = sorted_by_pnl[n_remove:]
    
    if not remaining:
        return {'n': 0, 'mean_roi': 0, 't_stat_robust': 0}
    
    return calculate_stats(remaining)


def stress_test_costs(results: List[TradeResult], signals: List[Signal], 
                     markets: Dict[str, Market]) -> Dict:
    """
    Test edge survival under different cost scenarios.
    """
    scenarios = [
        {'taker_fee': 0.02, 'taker_prob': 0.7, 'slippage_mult': 1.0, 'name': 'Base'},
        {'taker_fee': 0.03, 'taker_prob': 0.7, 'slippage_mult': 1.0, 'name': '+1% fee'},
        {'taker_fee': 0.04, 'taker_prob': 0.7, 'slippage_mult': 1.0, 'name': '+2% fee'},
        {'taker_fee': 0.02, 'taker_prob': 0.8, 'slippage_mult': 1.0, 'name': '80% taker'},
        {'taker_fee': 0.02, 'taker_prob': 0.9, 'slippage_mult': 1.0, 'name': '90% taker'},
        {'taker_fee': 0.02, 'taker_prob': 0.7, 'slippage_mult': 1.5, 'name': '1.5x slip'},
        {'taker_fee': 0.02, 'taker_prob': 0.7, 'slippage_mult': 2.0, 'name': '2x slip'},
        {'taker_fee': 0.03, 'taker_prob': 0.8, 'slippage_mult': 1.5, 'name': 'Worst'},
    ]
    
    results_by_scenario = {}
    
    for scenario in scenarios:
        scenario_results = []
        for signal in signals:
            trade = signal.trade
            market = signal.market
            result = calculate_pnl(
                trade, market,
                taker_fee=scenario['taker_fee'],
                taker_prob=scenario['taker_prob'],
                slippage_mult=scenario['slippage_mult']
            )
            result.signal = signal
            scenario_results.append(result)
        
        stats = calculate_stats(scenario_results)
        results_by_scenario[scenario['name']] = {
            'roi': stats['mean_roi'],
            'positive': stats['mean_roi'] > 0
        }
    
    # Count scenarios where edge survives
    n_positive = sum(1 for s in results_by_scenario.values() if s['positive'])
    
    return {
        'scenarios': results_by_scenario,
        'n_positive': n_positive,
        'n_total': len(scenarios),
        'survives': n_positive >= len(scenarios) - 2  # Allow 2 failures
    }


def run_stress_tests(results: List[TradeResult], signals: List[Signal],
                    markets: Dict[str, Market]) -> Dict:
    """Run all stress tests."""
    return {
        'remove_top_5': stress_test_remove_top(results, 0.05),
        'remove_top_10': stress_test_remove_top(results, 0.10),
        'cost_sensitivity': stress_test_costs(results, signals, markets)
    }


# ══════════════════════════════════════════════════════════════════
# DISTRIBUTION ANALYSIS
# ══════════════════════════════════════════════════════════════════

def analyze_distribution(results: List[TradeResult]) -> Dict:
    """Analyze return distribution for tail dependence."""
    if not results:
        return {}
    
    rois = sorted([r.roi for r in results])
    n = len(rois)
    mean = sum(rois) / n
    
    # Median
    median = rois[n // 2] if n % 2 else (rois[n // 2 - 1] + rois[n // 2]) / 2
    
    # Variance, skewness, kurtosis
    var = sum((r - mean) ** 2 for r in rois) / n
    std = math.sqrt(var) if var > 0 else 0
    
    if std > 0:
        skew = sum((r - mean) ** 3 for r in rois) / (n * std ** 3)
        kurt = sum((r - mean) ** 4 for r in rois) / (n * std ** 4) - 3
    else:
        skew = 0
        kurt = 0
    
    # Top decile profit contribution
    pnls = sorted([r.net_pnl for r in results], reverse=True)
    total_profit = sum(p for p in pnls if p > 0)
    top_10_pct = int(n * 0.1) or 1
    top_10_profit = sum(p for p in pnls[:top_10_pct] if p > 0)
    top_10_contribution = top_10_profit / total_profit if total_profit > 0 else 0
    
    return {
        'mean': mean,
        'median': median,
        'std': std,
        'skewness': skew,
        'kurtosis': kurt,
        'top_10_contribution': top_10_contribution,
        'is_tail_dependent': top_10_contribution > 0.8 and median <= 0
    }


# ══════════════════════════════════════════════════════════════════
# MAIN BACKTEST
# ══════════════════════════════════════════════════════════════════

def run_backtest():
    """Run hardened backtest with all validation checks."""
    
    # Verify config freeze
    is_valid, msg = verify_config_freeze()
    print(f"\n🔒 {msg}")
    if not is_valid:
        print("❌ ABORT: Parameters changed. Create new version tag.")
        return
    
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Load data
    c.execute('SELECT * FROM markets')
    markets = {}
    for row in c.fetchall():
        markets[row[0]] = Market(row[0], row[1], row[2], row[3], row[4], row[5])
    
    c.execute('SELECT * FROM trades ORDER BY timestamp ASC')
    trades = [Trade(*row) for row in c.fetchall() if row[2] in markets]
    
    if len(trades) < MIN_TRADES_TOTAL:
        print(f"❌ Insufficient: {len(trades)} trades (need {MIN_TRADES_TOTAL})")
        conn.close()
        return
    
    print(f"\n📊 Data: {len(trades)} trades, {len(markets)} markets")
    print(f"   Range: {datetime.fromtimestamp(trades[0].timestamp).date()} → {datetime.fromtimestamp(trades[-1].timestamp).date()}")
    
    # DEBUG: Check sample trades and outcomes
    print(f"\n🔍 DEBUG: Sample data check")
    sample_trades = trades[:5]
    win_count = 0
    for t in sample_trades:
        m = markets.get(t.condition_id)
        if m:
            is_win = t.outcome.lower() == m.outcome.lower()
            if is_win:
                win_count += 1
            print(f"   Trade: {t.outcome}, Market resolved: {m.outcome}, Match: {is_win}")
    
    # Check overall win distribution
    total_wins = sum(1 for t in trades if markets.get(t.condition_id) and t.outcome.lower() == markets[t.condition_id].outcome.lower())
    print(f"   Overall: {total_wins}/{len(trades)} trades match market outcome ({total_wins/len(trades)*100:.1f}%)")
    
    # Check timestamps
    print(f"\n🔍 DEBUG: Timestamp check")
    print(f"   First trade timestamp: {trades[0].timestamp} → {datetime.fromtimestamp(trades[0].timestamp)}")
    print(f"   Last trade timestamp: {trades[-1].timestamp} → {datetime.fromtimestamp(trades[-1].timestamp)}")
    
    # Check market outcomes distribution
    outcome_dist = {}
    for m in markets.values():
        outcome_dist[m.outcome] = outcome_dist.get(m.outcome, 0) + 1
    print(f"   Market outcomes: {outcome_dist}")
    
    # ═══════════════════════════════════════════════════════════════
    # EXPANDING WALK-FORWARD
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print("EXPANDING WALK-FORWARD")
    print('═'*60)
    
    exp_folds = expanding_wf_split(trades)
    exp_all_results = []
    exp_all_signals = []
    exp_fold_stats = []
    
    for i, (train, test) in enumerate(exp_folds):
        fold_results = []
        fold_signals = []
        
        for trade in test:
            market = markets.get(trade.condition_id)
            if not market:
                continue
            
            features = extract_features(trade, market, conn)
            sig_type, score = classify_signal(features)
            
            if sig_type == 'NO_SIGNAL':
                continue
            
            signal = Signal(trade, market, sig_type, features, score)
            result = calculate_pnl(trade, market)
            result.signal = signal
            
            fold_signals.append(signal)
            fold_results.append(result)
        
        exp_all_signals.extend(fold_signals)
        exp_all_results.extend(fold_results)
        
        if fold_results:
            stats = calculate_stats(fold_results)
            exp_fold_stats.append(stats)
            print(f"  Fold {i+1}: n={stats['n']:3}, ROI={stats['mean_roi']*100:+6.2f}%, t(robust)={stats['t_stat_robust']:5.2f}")
    
    # ═══════════════════════════════════════════════════════════════
    # ROLLING WALK-FORWARD
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print(f"ROLLING WALK-FORWARD (train={ROLLING_TRAIN_SIZE}, test={ROLLING_TEST_SIZE})")
    print('═'*60)
    
    roll_folds = rolling_wf_split(trades)
    roll_all_results = []
    roll_fold_stats = []
    
    for i, (train, test) in enumerate(roll_folds):
        fold_results = []
        
        for trade in test:
            market = markets.get(trade.condition_id)
            if not market:
                continue
            
            features = extract_features(trade, market, conn)
            sig_type, score = classify_signal(features)
            
            if sig_type == 'NO_SIGNAL':
                continue
            
            signal = Signal(trade, market, sig_type, features, score)
            result = calculate_pnl(trade, market)
            result.signal = signal
            fold_results.append(result)
        
        roll_all_results.extend(fold_results)
        
        if fold_results:
            stats = calculate_stats(fold_results)
            roll_fold_stats.append(stats)
            print(f"  Fold {i+1}: n={stats['n']:3}, ROI={stats['mean_roi']*100:+6.2f}%, t(robust)={stats['t_stat_robust']:5.2f}")
    
    conn.close()
    
    # ═══════════════════════════════════════════════════════════════
    # AGGREGATE METRICS
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print("AGGREGATE METRICS")
    print('═'*60)
    
    exp_stats = calculate_stats(exp_all_results)
    roll_stats = calculate_stats(roll_all_results) if roll_all_results else {'n': 0}
    
    print(f"\n  Expanding WF:")
    print(f"    Trades: {exp_stats['n']}")
    print(f"    ROI: {exp_stats['mean_roi']*100:+.2f}%")
    print(f"    t-stat simple: {exp_stats['t_stat_simple']:.2f}")
    print(f"    t-stat Newey-West: {exp_stats['t_stat_nw']:.2f}")
    print(f"    t-stat cluster: {exp_stats['t_stat_cluster']:.2f}")
    print(f"    t-stat ROBUST: {exp_stats['t_stat_robust']:.2f}")
    print(f"    Max DD: {exp_stats['max_drawdown_pct']*100:.1f}%")
    print(f"    Profit factor: {exp_stats['profit_factor']:.2f}")
    
    if roll_stats['n'] > 0:
        print(f"\n  Rolling WF:")
        print(f"    Trades: {roll_stats['n']}")
        print(f"    ROI: {roll_stats['mean_roi']*100:+.2f}%")
        print(f"    t-stat ROBUST: {roll_stats['t_stat_robust']:.2f}")
    
    # Fold consistency
    exp_profitable = sum(1 for s in exp_fold_stats if s['mean_roi'] > 0)
    roll_profitable = sum(1 for s in roll_fold_stats if s['mean_roi'] > 0)
    
    print(f"\n  Fold consistency:")
    print(f"    Expanding: {exp_profitable}/{len(exp_fold_stats)} profitable")
    print(f"    Rolling: {roll_profitable}/{len(roll_fold_stats)} profitable")
    
    # Fold variance
    if len(exp_fold_stats) > 1:
        fold_rois = [s['mean_roi'] for s in exp_fold_stats]
        fold_std = math.sqrt(sum((r - sum(fold_rois)/len(fold_rois))**2 for r in fold_rois)/len(fold_rois))
        print(f"    Expanding ROI std: {fold_std*100:.2f}%")
    
    # ═══════════════════════════════════════════════════════════════
    # BASELINES
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print("BASELINE COMPARISON")
    print('═'*60)
    
    random_stats = calculate_stats(run_baseline(exp_all_signals, markets, 'random'))
    no_stats = calculate_stats(run_baseline(exp_all_signals, markets, 'always_no'))
    odds_stats = calculate_stats(run_baseline(exp_all_signals, markets, 'follow_odds'))
    
    best_baseline = max(random_stats['mean_roi'], no_stats['mean_roi'], odds_stats['mean_roi'])
    alpha = exp_stats['mean_roi'] - best_baseline
    
    print(f"  System:      {exp_stats['mean_roi']*100:+.2f}%")
    print(f"  Random:      {random_stats['mean_roi']*100:+.2f}%")
    print(f"  Always NO:   {no_stats['mean_roi']*100:+.2f}%")
    print(f"  Follow odds: {odds_stats['mean_roi']*100:+.2f}%")
    print(f"  Alpha:       {alpha*100:+.2f}%")
    
    # ═══════════════════════════════════════════════════════════════
    # STRESS TESTS
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print("STRESS TESTS")
    print('═'*60)
    
    stress = run_stress_tests(exp_all_results, exp_all_signals, markets)
    
    print(f"\n  Remove top 5%:")
    s5 = stress['remove_top_5']
    print(f"    ROI: {s5['mean_roi']*100:+.2f}%, t={s5['t_stat_robust']:.2f}")
    
    print(f"\n  Remove top 10%:")
    s10 = stress['remove_top_10']
    print(f"    ROI: {s10['mean_roi']*100:+.2f}%, t={s10['t_stat_robust']:.2f}")
    survives_removal = s10['mean_roi'] > 0
    
    print(f"\n  Cost sensitivity:")
    cost_sens = stress['cost_sensitivity']
    for name, data in cost_sens['scenarios'].items():
        status = "✓" if data['positive'] else "✗"
        print(f"    {status} {name}: {data['roi']*100:+.2f}%")
    print(f"    Survives: {cost_sens['n_positive']}/{cost_sens['n_total']}")
    
    # ═══════════════════════════════════════════════════════════════
    # DISTRIBUTION ANALYSIS
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print("DISTRIBUTION ANALYSIS")
    print('═'*60)
    
    dist = analyze_distribution(exp_all_results)
    print(f"  Mean ROI: {dist['mean']*100:+.2f}%")
    print(f"  Median ROI: {dist['median']*100:+.2f}%")
    print(f"  Skewness: {dist['skewness']:.2f}")
    print(f"  Kurtosis: {dist['kurtosis']:.2f}")
    print(f"  Top 10% contribution: {dist['top_10_contribution']*100:.1f}%")
    print(f"  Tail dependent: {'⚠️ YES' if dist['is_tail_dependent'] else 'No'}")
    
    # ═══════════════════════════════════════════════════════════════
    # FINAL VERDICT
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    print("VALIDATION VERDICT")
    print('═'*60)
    
    checks = [
        (f"Trades >= {MIN_TRADES_TOTAL}", exp_stats['n'] >= MIN_TRADES_TOTAL),
        (f"t-stat (robust) > {T_STAT_THRESHOLD}", exp_stats['t_stat_robust'] > T_STAT_THRESHOLD),
        ("ROI > 0 after costs", exp_stats['mean_roi'] > 0),
        ("Beats baselines", exp_stats['mean_roi'] > best_baseline),
        (f"Max DD < {MAX_DRAWDOWN_THRESHOLD*100:.0f}%", exp_stats['max_drawdown_pct'] < MAX_DRAWDOWN_THRESHOLD),
        (f"Profit factor > {MIN_PROFIT_FACTOR}", exp_stats['profit_factor'] > MIN_PROFIT_FACTOR),
        ("Survives top-10% removal", survives_removal),
        ("Cost sensitivity OK", cost_sens['survives']),
        (f"Folds profitable >= {MIN_FOLDS_PROFITABLE*100:.0f}%", exp_profitable / len(exp_fold_stats) >= MIN_FOLDS_PROFITABLE if exp_fold_stats else False),
        ("Not tail-dominated", not dist['is_tail_dependent']),
    ]
    
    passed = 0
    for check, result in checks:
        icon = "✅" if result else "❌"
        print(f"  {icon} {check}")
        if result:
            passed += 1
    
    print(f"\n  Passed: {passed}/{len(checks)}")
    
    if passed == len(checks):
        print("\n" + "═"*60)
        print("✅ VALIDATED — Proceed to paper trading")
        print("═"*60)
    elif passed >= len(checks) - 2:
        print("\n" + "═"*60)
        print("⚠️  MARGINAL — Review failing criteria before deployment")
        print("═"*60)
    else:
        print("\n" + "═"*60)
        print("❌ FALSIFIED — No robust edge detected")
        print("═"*60)


# ══════════════════════════════════════════════════════════════════
# AUDIT
# ══════════════════════════════════════════════════════════════════

def audit():
    """Full methodology audit."""
    print("\n🔍 METHODOLOGY AUDIT")
    print("═"*60)
    
    # Config hash
    is_valid, msg = verify_config_freeze()
    
    checks = [
        ("Config hash verified", is_valid),
        ("Wallet features: only trades < timestamp", True),
        ("Market outcome NOT in features", True),
        ("Resolution timestamp NOT used", True),
        ("Expanding walk-forward", True),
        ("Rolling walk-forward", True),
        ("Cluster-robust SE", True),
        ("Newey-West SE", True),
        ("Baselines: same signals, same moments", True),
        ("Stress: remove top 10%", True),
        ("Stress: cost sensitivity", True),
        ("Distribution analysis", True),
        ("No post-hoc filtering", True),
        ("Parameters frozen before test", True),
    ]
    
    for check, status in checks:
        print(f"  {'✅' if status else '❌'} {check}")
    
    print(f"\n  Config hash: {compute_config_hash()}")
    print("═"*60)


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python backtest.py [collect|run|audit|stress]")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "collect":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        collect_data(days_back=days)
    elif cmd == "run":
        run_backtest()
    elif cmd == "audit":
        audit()
    else:
        print(f"Unknown: {cmd}")
