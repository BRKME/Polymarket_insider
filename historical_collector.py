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
            final_volume REAL,
            slug TEXT
        )
    ''')
    
    # Add slug column if it doesn't exist (migration)
    try:
        c.execute('ALTER TABLE markets ADD COLUMN slug TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    
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
                    'slug': m.get('slug', ''),
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


def fetch_markets_closing_soon(hours: int = 72, limit: int = 100) -> List[Dict]:
    """
    Fetch markets that will close in the next N hours.
    This catches sports/short-term markets before they resolve.
    """
    url = f"{GAMMA_API_URL}/markets"
    
    # Get markets sorted by end date
    params = {
        "limit": limit,
        "active": "true",
        "closed": "false",
        "order": "endDate",
        "_sort": "endDate:asc"  # Soonest first
    }
    
    try:
        time.sleep(REQUEST_DELAY)
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        markets = response.json()
        
        cutoff = datetime.now(timezone.utc) + timedelta(hours=hours)
        result = []
        
        for m in markets:
            end_date_str = m.get('endDate', '')
            if not end_date_str:
                continue
            
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                if end_date <= cutoff:
                    outcomes = m.get('outcomes', [])
                    if isinstance(outcomes, str):
                        try:
                            outcomes = json.loads(outcomes)
                        except:
                            outcomes = []
                    
                    volume = float(m.get('volume', 0) or 0)
                    if volume >= 1000:  # Lower threshold for short-term markets
                        result.append({
                            'condition_id': m.get('conditionId', ''),
                            'question': m.get('question', ''),
                            'outcomes': outcomes,
                            'end_date': end_date_str,
                            'volume': volume,
                            'category': classify_category(m.get('question', ''))
                        })
            except:
                continue
        
        print(f"      [DEBUG] Found {len(result)} markets closing within {hours}h")
        return result
    
    except Exception as e:
        print(f"[ERROR] Fetching markets closing soon: {e}")
        return []


def fetch_recently_resolved(limit: int = 200) -> List[Dict]:
    """Fetch recently resolved markets to capture outcomes."""
    
    all_resolved = {}
    
    # Calculate cutoff date - only markets that ended after we started collecting
    # We started on 2026-02-28, so look for markets ending after 2026-02-25
    cutoff_date = "2026-02-25"
    
    # Try multiple API queries to catch recent resolutions
    queries = [
        # Most recent by endDate
        {"closed": "true", "order": "endDate", "_sort": "endDate:desc", "limit": limit},
        # High volume closed markets
        {"closed": "true", "order": "volume", "_sort": "volume:desc", "limit": limit},
    ]
    
    for params in queries:
        url = f"{GAMMA_API_URL}/markets"
        
        try:
            time.sleep(REQUEST_DELAY)
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            markets = response.json()
            
            for m in markets:
                cid = m.get('conditionId', '')
                end_date = m.get('endDate', '')
                
                # Filter: only include markets that ended recently
                if end_date and end_date >= cutoff_date:
                    if cid and cid not in all_resolved:
                        all_resolved[cid] = m
                    
        except Exception as e:
            print(f"[ERROR] Fetching resolved markets: {e}")
    
    print(f"      [DEBUG] Fetched {len(all_resolved)} closed markets after {cutoff_date}")
    
    result = []
    skipped_no_resolution = 0
    skipped_no_winner = 0
    
    for cid, m in all_resolved.items():
        if not m.get('resolutionSource'):
            skipped_no_resolution += 1
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
        
        # Find winning outcome - RELAXED threshold
        winning = None
        for i, p in enumerate(outcome_prices):
            try:
                price = float(p)
                if price >= 0.95 and i < len(outcomes):
                    winning = outcomes[i]
                    break
            except:
                pass
        
        # Fallback: use highest price if > 0.9
        if not winning and outcome_prices and outcomes:
            try:
                prices = [float(p) for p in outcome_prices]
                max_idx = max(range(len(prices)), key=lambda i: prices[i])
                if prices[max_idx] > 0.9:
                    winning = outcomes[max_idx]
            except:
                pass
        
        if not winning:
            skipped_no_winner += 1
            continue
        
        result.append({
            'condition_id': cid,
            'question': m.get('question', ''),
            'resolved_outcome': winning,
            'volume': float(m.get('volume', 0) or 0),
            'end_date': m.get('endDate', '')
        })
    
    print(f"      [DEBUG] Found {len(result)} resolved, skipped: {skipped_no_resolution} no resolution, {skipped_no_winner} no winner")
    
    # Show sample for debugging
    if result:
        sample = result[0]
        print(f"      [DEBUG] Sample resolved: end={sample.get('end_date', '')}, q={sample['question'][:50]}...")
    
    return result


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
    
    # 1. Fetch active markets + markets closing soon
    print("\n[1/3] Fetching markets...")
    active_markets = fetch_active_markets()
    print(f"      Found {len(active_markets)} active markets (volume >= $5K)")
    
    closing_soon = fetch_markets_closing_soon(hours=72)
    
    # Merge and deduplicate by condition_id
    all_markets = {m['condition_id']: m for m in active_markets}
    for m in closing_soon:
        if m['condition_id'] not in all_markets:
            all_markets[m['condition_id']] = m
    
    markets_list = list(all_markets.values())
    print(f"      Total unique markets to track: {len(markets_list)}")
    
    # 2. Update markets table and collect trades
    print("\n[2/3] Collecting trades...")
    for m in markets_list:
        stats['markets_checked'] += 1
        
        # Check if market exists
        c.execute('SELECT condition_id FROM markets WHERE condition_id = ?', 
                  (m['condition_id'],))
        exists = c.fetchone()
        
        if not exists:
            # New market
            c.execute('''
                INSERT INTO markets (condition_id, question, outcomes, end_date, 
                                    category, first_seen, last_updated, slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                m['condition_id'],
                m['question'],
                json.dumps(m['outcomes']),
                m['end_date'],
                m['category'],
                now,
                now,
                m.get('slug', '')
            ))
            stats['new_markets'] += 1
        else:
            # Update last_updated and slug
            c.execute('UPDATE markets SET last_updated = ?, slug = ? WHERE condition_id = ?',
                     (now, m.get('slug', ''), m['condition_id']))
        
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
            print(f"      {stats['markets_checked']}/{len(markets_list)} markets, {stats['new_trades']} new trades")
    
    conn.commit()
    
    # 3. Check for resolutions
    print("\n[3/3] Checking for resolutions...")
    
    # Get all unresolved tracked markets
    c.execute('SELECT condition_id, question, end_date, slug FROM markets WHERE is_resolved = 0')
    unresolved = c.fetchall()
    unresolved_cids = {row[0] for row in unresolved}
    
    print(f"      Total unresolved markets in DB: {len(unresolved)}")
    
    # Debug: show sample of our condition IDs
    sample_our_cids = list(unresolved_cids)[:3]
    print(f"      [DEBUG] Sample our CIDs: {[c[:30]+'...' for c in sample_our_cids]}")
    
    # APPROACH 1: Batch fetch closed markets from API
    try:
        url = f"{GAMMA_API_URL}/markets"
        params = {"closed": "true", "limit": 500}
        response = requests.get(url, params=params, timeout=30)
        
        if response.status_code == 200:
            closed_markets = response.json()
            print(f"      [API] Fetched {len(closed_markets)} closed markets from Gamma API")
            
            # Debug: show sample API condition IDs
            sample_api_cids = [m.get('conditionId', '')[:30]+'...' for m in closed_markets[:3]]
            print(f"      [DEBUG] Sample API CIDs: {sample_api_cids}")
            
            # Count matches before filtering
            matched_count = 0
            matched_but_no_source = 0
            
            # Cross-reference with our tracked markets
            for market in closed_markets:
                cid = market.get('conditionId')
                if not cid or cid not in unresolved_cids:
                    continue
                
                matched_count += 1
                
                # Check resolution source
                if not market.get('resolutionSource'):
                    matched_but_no_source += 1
                    continue
                
                # Parse outcome prices to find winner
                outcome_prices = market.get('outcomePrices', [])
                outcomes = market.get('outcomes', [])
                
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except:
                        continue
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except:
                        continue
                
                # Find winner (price >= 0.95)
                winning = None
                for i, p in enumerate(outcome_prices):
                    try:
                        if float(p) >= 0.95 and i < len(outcomes):
                            winning = outcomes[i]
                            break
                    except:
                        pass
                
                # Fallback: highest price > 0.9
                if not winning and outcome_prices and outcomes:
                    try:
                        prices = [float(p) for p in outcome_prices]
                        max_idx = max(range(len(prices)), key=lambda i: prices[i])
                        if prices[max_idx] > 0.9:
                            winning = outcomes[max_idx]
                    except:
                        pass
                
                if not winning:
                    continue
                
                # Update database
                c.execute('''
                    UPDATE markets 
                    SET is_resolved = 1, resolved_outcome = ?, resolved_at = ?, final_volume = ?
                    WHERE condition_id = ?
                ''', (
                    winning,
                    datetime.now(timezone.utc).isoformat(),
                    market.get('volume'),
                    cid
                ))
                stats['resolutions_found'] += 1
                
                question = market.get('question', '')[:50]
                print(f"      ✓ Resolved: {question}... → {winning}")
            
            # Debug: show match statistics
            print(f"      [DEBUG] Matched {matched_count} markets, {matched_but_no_source} without resolutionSource")
            
            conn.commit()
        else:
            print(f"      [API ERROR] Failed to fetch closed markets: {response.status_code}")
    except Exception as e:
        print(f"      [API ERROR] Exception fetching closed markets: {e}")
    
    # APPROACH 2: Check individual markets that should have resolved
    # Only check markets past end_date that weren't found above
    c.execute('SELECT condition_id, question, end_date, slug FROM markets WHERE is_resolved = 0')
    still_unresolved = c.fetchall()
    
    today = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    to_check = []
    sports_keywords = ['vs', 'nba', 'nfl', 'mlb', 'nhl', 'cs2', 'lol', 'dota', 'valorant', 'soccer', 'football']
    
    for cid, question, end_date, slug in still_unresolved:
        q_lower = (question or '').lower()
        is_sports = any(kw in q_lower for kw in sports_keywords)
        
        # Check sports markets regardless of end_date (they resolve quickly)
        if is_sports:
            to_check.append((cid, question, slug, 'sports'))
        # Also check markets past end_date
        elif end_date and end_date < today:
            to_check.append((cid, question, slug, 'past_end'))
    
    # Debug: show what we're checking
    sports_count = sum(1 for x in to_check if x[3] == 'sports')
    past_end_count = sum(1 for x in to_check if x[3] == 'past_end')
    print(f"      Checking {len(to_check)} markets: {sports_count} sports, {past_end_count} past end_date")
    
    checked = 0
    not_closed = 0
    no_resolution_source = 0
    no_winner = 0
    api_errors = 0
    
    for condition_id, question, slug, reason in to_check[:100]:  # Limit to 100 to avoid rate limits
        checked += 1
        if checked % 20 == 0:
            print(f"      Checked {checked}/{min(len(to_check), 100)}...")
        
        try:
            time.sleep(REQUEST_DELAY * 0.3)
            market = None
            
            # Try slug first
            if slug:
                url = f"{GAMMA_API_URL}/markets"
                response = requests.get(url, params={"slug": slug}, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        market = data[0] if isinstance(data, list) else data
            
            # Fallback to condition_id
            if not market:
                url = f"{GAMMA_API_URL}/markets"
                response = requests.get(url, params={"conditionId": condition_id}, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        market = data[0] if isinstance(data, list) else data
            
            if not market:
                api_errors += 1
                if api_errors <= 3:
                    print(f"      [API ERROR] No data for slug={slug}, cid={condition_id[:20]}...")
                continue
            
            # Debug: show first successful lookup
            if checked == 1:
                print(f"      [DEBUG] First lookup success: closed={market.get('closed')}, resolutionSource={bool(market.get('resolutionSource'))}")
            
            # Debug: show first few markets
            if checked <= 3:
                print(f"      [DEBUG] Market {checked}: closed={market.get('closed')}, resolutionSource={market.get('resolutionSource')}, q={question[:40]}...")
            
            # Check if resolved
            if not market.get('closed'):
                not_closed += 1
                continue
            if not market.get('resolutionSource'):
                no_resolution_source += 1
                continue
            
            # Parse outcomes
            outcomes = market.get('outcomes', [])
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except:
                    continue
            
            outcome_prices = market.get('outcomePrices', [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except:
                    continue
            
            # Find winner
            winning = None
            for i, p in enumerate(outcome_prices):
                try:
                    if float(p) >= 0.95 and i < len(outcomes):
                        winning = outcomes[i]
                        break
                except:
                    pass
            
            # Fallback: highest price > 0.9
            if not winning and outcome_prices and outcomes:
                try:
                    prices = [float(p) for p in outcome_prices]
                    max_idx = max(range(len(prices)), key=lambda i: prices[i])
                    if prices[max_idx] > 0.9:
                        winning = outcomes[max_idx]
                except:
                    pass
            
            if not winning:
                no_winner += 1
                continue
            
            # Mark as resolved!
            volume = float(market.get('volume', 0) or 0)
            c.execute('''
                UPDATE markets 
                SET is_resolved = 1, resolved_outcome = ?, resolved_at = ?, final_volume = ?
                WHERE condition_id = ?
            ''', (winning, now, volume, condition_id))
            stats['resolutions_found'] += 1
            print(f"      ✓ Resolved: {question[:50]}... → {winning}")
            
        except Exception as e:
            api_errors += 1
            continue
    
    print(f"      [RESULT] Checked: {checked}, Resolved: {stats['resolutions_found']}")
    print(f"      [BREAKDOWN] not_closed: {not_closed}, no_resolution_source: {no_resolution_source}, no_winner: {no_winner}, api_errors: {api_errors}")
    
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
