"""
Historical Data Collector — Build Dataset for Backtest

Problem: Polymarket API doesn't return historical trades for resolved markets.
Solution: Collect trades in real-time and store for future backtest.

Run every 10 minutes via cron/GitHub Actions.
After 30-90 days, will have sufficient data for proper backtest.

Data collected:
- All trades >=$1000 on active markets
- Market metadata (question, outcomes, end_date)
- Market resolutions when they occur
"""

import json
import sqlite3
import requests
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from config import GAMMA_API_URL, DATA_API_URL, REQUEST_DELAY


# ══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════

DB_PATH = Path("historical_data.db")
MIN_TRADE_AMOUNT = 1000  # $1000 minimum
COLLECTION_WINDOW_MINUTES = 15  # Look back 15 minutes each run


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════

def init_db():
    """Initialize database for historical data collection."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Markets table - track all seen markets
    c.execute('''
        CREATE TABLE IF NOT EXISTS markets (
            condition_id TEXT PRIMARY KEY,
            question TEXT,
            outcomes TEXT,
            end_date TEXT,
            category TEXT,
            first_seen TEXT,
            last_updated TEXT,
            is_resolved INTEGER DEFAULT 0,
            resolved_outcome TEXT,
            resolved_at TEXT,
            final_volume REAL
        )
    ''')
    
    # Trades table - all significant trades
    c.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            trade_hash TEXT PRIMARY KEY,
            condition_id TEXT,
            wallet TEXT,
            timestamp INTEGER,
            outcome TEXT,
            price REAL,
            size REAL,
            amount REAL,
            collected_at TEXT,
            FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
        )
    ''')
    
    # Collection log - track each collection run
    c.execute('''
        CREATE TABLE IF NOT EXISTS collection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            markets_checked INTEGER,
            new_trades INTEGER,
            new_markets INTEGER,
            resolutions_found INTEGER,
            duration_seconds REAL
        )
    ''')
    
    # Indices for performance
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_trades_cond ON trades(condition_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_markets_resolved ON markets(is_resolved)')
    
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════
# DATA FETCHING
# ══════════════════════════════════════════════════════════════════

def fetch_active_markets(limit: int = 200) -> List[Dict]:
    """Fetch currently active (not yet resolved) markets."""
    url = f"{GAMMA_API_URL}/markets"
    params = {
        "limit": limit,
        "active": "true",
        "closed": "false",
    }
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        markets = response.json()
        
        result = []
        for m in markets:
            # Parse outcomes from JSON string if needed
            outcomes = m.get('outcomes', [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = []
            
            volume = float(m.get('volume', 0) or 0)
            
            # Only track markets with decent volume
            if volume >= 5000:
                result.append({
                    'condition_id': m.get('conditionId', ''),
                    'question': m.get('question', ''),
                    'outcomes': outcomes,
                    'end_date': m.get('endDate', ''),
                    'volume': volume,
                    'category': classify_category(m.get('question', ''))
                })
        
        return result
    
    except Exception as e:
        print(f"[ERROR] Fetching active markets: {e}")
        return []


def fetch_recently_resolved(limit: int = 100) -> List[Dict]:
    """Fetch recently resolved markets to capture outcomes."""
    url = f"{GAMMA_API_URL}/markets"
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
        
        result = []
        for m in markets:
            if not m.get('resolutionSource'):
                continue
            
            # Parse outcomes
            outcomes = m.get('outcomes', [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except:
                    outcomes = []
            
            outcome_prices = m.get('outcomePrices', [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except:
                    outcome_prices = []
            
            # Find winning outcome
            winning = None
            for i, p in enumerate(outcome_prices):
                try:
                    if float(p) >= 0.99 and i < len(outcomes):
                        winning = outcomes[i]
                        break
                except:
                    pass
            
            if winning:
                result.append({
                    'condition_id': m.get('conditionId', ''),
                    'resolved_outcome': winning,
                    'volume': float(m.get('volume', 0) or 0)
                })
        
        return result
    
    except Exception as e:
        print(f"[ERROR] Fetching resolved markets: {e}")
        return []


def fetch_recent_trades(condition_id: str, minutes_back: int = COLLECTION_WINDOW_MINUTES) -> List[Dict]:
    """Fetch recent trades for a market."""
    url = f"{DATA_API_URL}/trades"
    
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(minutes=minutes_back)).timestamp())
    
    trades = []
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params={
            "conditionId": condition_id,
            "limit": 500,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC"
        }, timeout=30)
        
        if response.status_code != 200:
            return []
        
        batch = response.json()
        
        for t in batch:
            # Parse timestamp (might be milliseconds)
            ts = t.get('timestamp', 0)
            if ts > 10000000000:
                ts = ts // 1000
            
            # Only recent trades
            if ts < cutoff_ts:
                continue
            
            size = float(t.get('size', 0))
            price = float(t.get('price', 0))
            outcome = t.get('outcome', 'Yes')
            
            # Calculate amount
            if outcome.lower() == 'no':
                amount = size * (1 - price)
            else:
                amount = size * price
            
            if amount >= MIN_TRADE_AMOUNT:
                trades.append({
                    'trade_hash': t.get('transactionHash', ''),
                    'condition_id': condition_id,
                    'wallet': t.get('proxyWallet', ''),
                    'timestamp': ts,
                    'outcome': outcome,
                    'price': price,
                    'size': size,
                    'amount': amount
                })
        
        return trades
    
    except Exception as e:
        print(f"[ERROR] Fetching trades for {condition_id[:12]}: {e}")
        return []


def classify_category(q: str) -> str:
    """Classify market category."""
    q = q.lower()
    if any(w in q for w in ['trump', 'biden', 'election', 'president', 'congress']): 
        return 'politics'
    if any(w in q for w in ['war', 'strike', 'iran', 'russia', 'ukraine', 'military']): 
        return 'geopolitics'
    if any(w in q for w in ['bitcoin', 'crypto', 'btc', 'eth', 'ethereum']): 
        return 'crypto'
    if any(w in q for w in ['nba', 'nfl', 'mlb', 'nhl', 'sports', 'game', 'vs']): 
        return 'sports'
    if any(w in q for w in ['fed', 'rate', 'inflation', 'gdp', 'jobs']): 
        return 'macro'
    return 'other'


# ══════════════════════════════════════════════════════════════════
# MAIN COLLECTION
# ══════════════════════════════════════════════════════════════════

def run_collection():
    """Run one collection cycle."""
    start_time = time.time()
    init_db()
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    now = datetime.now(timezone.utc).isoformat()
    
    stats = {
        'markets_checked': 0,
        'new_trades': 0,
        'new_markets': 0,
        'resolutions_found': 0
    }
    
    print(f"\n{'═'*60}")
    print(f"HISTORICAL DATA COLLECTION — {now[:19]}")
    print('═'*60)
    
    # 1. Fetch active markets
    print("\n[1/3] Fetching active markets...")
    active_markets = fetch_active_markets()
    print(f"      Found {len(active_markets)} active markets (volume >= $5K)")
    
    # 2. Update markets table and collect trades
    print("\n[2/3] Collecting trades...")
    for m in active_markets:
        stats['markets_checked'] += 1
        
        # Check if market exists
        c.execute('SELECT condition_id FROM markets WHERE condition_id = ?', 
                  (m['condition_id'],))
        exists = c.fetchone()
        
        if not exists:
            # New market
            c.execute('''
                INSERT INTO markets (condition_id, question, outcomes, end_date, 
                                    category, first_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                m['condition_id'],
                m['question'],
                json.dumps(m['outcomes']),
                m['end_date'],
                m['category'],
                now,
                now
            ))
            stats['new_markets'] += 1
        else:
            # Update last_updated
            c.execute('UPDATE markets SET last_updated = ? WHERE condition_id = ?',
                     (now, m['condition_id']))
        
        # Fetch trades for this market
        trades = fetch_recent_trades(m['condition_id'])
        
        for t in trades:
            try:
                c.execute('''
                    INSERT OR IGNORE INTO trades 
                    (trade_hash, condition_id, wallet, timestamp, outcome, price, size, amount, collected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    t['trade_hash'],
                    t['condition_id'],
                    t['wallet'],
                    t['timestamp'],
                    t['outcome'],
                    t['price'],
                    t['size'],
                    t['amount'],
                    now
                ))
                if c.rowcount > 0:
                    stats['new_trades'] += 1
            except sqlite3.IntegrityError:
                pass  # Duplicate trade
        
        # Progress
        if stats['markets_checked'] % 20 == 0:
            print(f"      {stats['markets_checked']}/{len(active_markets)} markets, {stats['new_trades']} new trades")
    
    conn.commit()
    
    # 3. Check for resolutions
    print("\n[3/3] Checking for resolutions...")
    resolved = fetch_recently_resolved()
    
    for r in resolved:
        # Check if we have this market and it's not yet marked resolved
        c.execute('''
            SELECT condition_id, is_resolved FROM markets 
            WHERE condition_id = ? AND is_resolved = 0
        ''', (r['condition_id'],))
        
        row = c.fetchone()
        if row:
            c.execute('''
                UPDATE markets 
                SET is_resolved = 1, resolved_outcome = ?, resolved_at = ?, final_volume = ?
                WHERE condition_id = ?
            ''', (r['resolved_outcome'], now, r['volume'], r['condition_id']))
            stats['resolutions_found'] += 1
            print(f"      ✓ Resolved: {r['condition_id'][:12]}... → {r['resolved_outcome']}")
    
    conn.commit()
    
    # Log collection run
    duration = time.time() - start_time
    c.execute('''
        INSERT INTO collection_log (run_at, markets_checked, new_trades, new_markets, resolutions_found, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (now, stats['markets_checked'], stats['new_trades'], stats['new_markets'], 
          stats['resolutions_found'], duration))
    
    conn.commit()
    conn.close()
    
    # Summary
    print(f"\n{'─'*60}")
    print(f"COLLECTION COMPLETE")
    print(f"  Markets checked: {stats['markets_checked']}")
    print(f"  New markets: {stats['new_markets']}")
    print(f"  New trades: {stats['new_trades']}")
    print(f"  Resolutions: {stats['resolutions_found']}")
    print(f"  Duration: {duration:.1f}s")
    print('═'*60)
    
    return stats


def show_stats():
    """Show current database statistics."""
    if not DB_PATH.exists():
        print("No data collected yet. Run: python historical_collector.py collect")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    print(f"\n{'═'*60}")
    print("HISTORICAL DATA STATISTICS")
    print('═'*60)
    
    # Markets
    c.execute('SELECT COUNT(*) FROM markets')
    total_markets = c.fetchone()[0]
    
    c.execute('SELECT COUNT(*) FROM markets WHERE is_resolved = 1')
    resolved_markets = c.fetchone()[0]
    
    print(f"\n📊 Markets")
    print(f"   Total tracked: {total_markets}")
    print(f"   Resolved: {resolved_markets}")
    print(f"   Active: {total_markets - resolved_markets}")
    
    # Trades
    c.execute('SELECT COUNT(*) FROM trades')
    total_trades = c.fetchone()[0]
    
    c.execute('SELECT MIN(timestamp), MAX(timestamp) FROM trades')
    ts_range = c.fetchone()
    
    print(f"\n📈 Trades")
    print(f"   Total: {total_trades}")
    if ts_range[0] and ts_range[1]:
        first = datetime.fromtimestamp(ts_range[0])
        last = datetime.fromtimestamp(ts_range[1])
        days = (last - first).days + 1
        print(f"   Date range: {first.date()} → {last.date()} ({days} days)")
        if days > 0:
            print(f"   Avg trades/day: {total_trades / days:.1f}")
    
    # Trades with resolved outcomes (usable for backtest)
    c.execute('''
        SELECT COUNT(*) FROM trades t
        JOIN markets m ON t.condition_id = m.condition_id
        WHERE m.is_resolved = 1
    ''')
    backtest_ready = c.fetchone()[0]
    
    print(f"\n🧪 Backtest Ready")
    print(f"   Trades with resolved outcomes: {backtest_ready}")
    
    # Minimum needed
    min_needed = 100
    if backtest_ready >= min_needed:
        print(f"   ✅ Sufficient for backtest (>={min_needed})")
    else:
        print(f"   ⏳ Need {min_needed - backtest_ready} more resolved trades")
    
    # Estimated time to 100 trades
    c.execute('SELECT COUNT(*), MIN(run_at), MAX(run_at) FROM collection_log')
    log_stats = c.fetchone()
    if log_stats[0] > 1 and backtest_ready > 0:
        try:
            first_run = datetime.fromisoformat(log_stats[1].replace('Z', '+00:00'))
            last_run = datetime.fromisoformat(log_stats[2].replace('Z', '+00:00'))
            hours_elapsed = (last_run - first_run).total_seconds() / 3600
            if hours_elapsed > 0:
                rate = backtest_ready / hours_elapsed
                if rate > 0 and backtest_ready < min_needed:
                    hours_needed = (min_needed - backtest_ready) / rate
                    print(f"   ⏱️  Estimated: {hours_needed:.1f} hours to reach {min_needed}")
        except:
            pass
    
    # Category breakdown
    c.execute('''
        SELECT category, COUNT(*) as cnt FROM markets 
        GROUP BY category ORDER BY cnt DESC
    ''')
    categories = c.fetchall()
    
    print(f"\n📁 Categories")
    for cat, cnt in categories:
        print(f"   {cat}: {cnt}")
    
    # Collection history
    c.execute('''
        SELECT run_at, new_trades, resolutions_found FROM collection_log 
        ORDER BY id DESC LIMIT 5
    ''')
    recent_runs = c.fetchall()
    
    if recent_runs:
        print(f"\n🕐 Recent Collections")
        for run_at, new_trades, resolutions in recent_runs:
            print(f"   {run_at[:19]}: +{new_trades} trades, {resolutions} resolutions")
    
    conn.close()
    print('═'*60)


def export_for_backtest():
    """Export collected data to backtest.db format."""
    if not DB_PATH.exists():
        print("No data collected yet.")
        return
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Check if we have enough data
    c.execute('''
        SELECT COUNT(*) FROM trades t
        JOIN markets m ON t.condition_id = m.condition_id
        WHERE m.is_resolved = 1
    ''')
    count = c.fetchone()[0]
    
    if count < 100:
        print(f"Only {count} resolved trades. Need at least 100 for backtest.")
        conn.close()
        return
    
    # Export to backtest.db
    backtest_db = Path("backtest.db")
    if backtest_db.exists():
        backtest_db.unlink()
    
    bt_conn = sqlite3.connect(backtest_db)
    bt_c = bt_conn.cursor()
    
    # Create tables
    bt_c.execute('''
        CREATE TABLE markets (
            condition_id TEXT PRIMARY KEY,
            question TEXT,
            outcome TEXT,
            end_date TEXT,
            volume REAL,
            category TEXT,
            fetched_at TEXT
        )
    ''')
    
    bt_c.execute('''
        CREATE TABLE trades (
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
    
    # Copy resolved markets
    c.execute('''
        SELECT condition_id, question, resolved_outcome, end_date, final_volume, category, resolved_at
        FROM markets WHERE is_resolved = 1
    ''')
    
    markets_exported = 0
    for row in c.fetchall():
        bt_c.execute('INSERT INTO markets VALUES (?,?,?,?,?,?,?)', row)
        markets_exported += 1
    
    # Copy trades for resolved markets
    c.execute('''
        SELECT t.trade_hash, t.wallet, t.condition_id, t.timestamp, t.outcome, t.price, t.size, t.amount
        FROM trades t
        JOIN markets m ON t.condition_id = m.condition_id
        WHERE m.is_resolved = 1
    ''')
    
    trades_exported = 0
    for row in c.fetchall():
        bt_c.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)', row)
        trades_exported += 1
    
    bt_c.execute('CREATE INDEX idx_trades_ts ON trades(timestamp)')
    bt_c.execute('CREATE INDEX idx_trades_cond ON trades(condition_id)')
    
    bt_conn.commit()
    bt_conn.close()
    conn.close()
    
    print(f"✅ Exported to backtest.db:")
    print(f"   Markets: {markets_exported}")
    print(f"   Trades: {trades_exported}")
    print(f"\nRun: python backtest.py run")


# ══════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Historical Data Collector for Polymarket Backtest")
        print("")
        print("Usage: python historical_collector.py [command]")
        print("")
        print("Commands:")
        print("  collect - Run one collection cycle (run every 10 min)")
        print("  stats   - Show database statistics")
        print("  export  - Export to backtest.db format")
        print("")
        print("Setup:")
        print("  1. Run 'collect' every 10 minutes via cron or GitHub Actions")
        print("  2. Wait until stats shows >= 100 resolved trades")
        print("  3. Run 'export' to create backtest.db")
        print("  4. Run 'python backtest.py run' to validate")
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    if cmd == "collect":
        run_collection()
    elif cmd == "stats":
        show_stats()
    elif cmd == "export":
        export_for_backtest()
    else:
        print(f"Unknown command: {cmd}")
