import sqlite3
from datetime import datetime, timezone
from typing import Dict, Optional, List
import os
from pathlib import Path
import shutil
import threading

# FIX BUG #2: Persistent database path
# Use home directory for persistence across GitHub Actions runs
DATA_DIR = Path.home() / ".polymarket_data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "polymarket_insider.db"

# FIX ISSUE #15: Thread-local storage for thread safety
local = threading.local()

def get_db_connection():
    """
    Get thread-local database connection.
    FIX ISSUE #15: Thread-safe database access.
    FIX: Set isolation_level=None for manual transaction control.
    """
    if not hasattr(local, 'conn') or local.conn is None:
        local.conn = sqlite3.connect(
            str(DB_PATH), 
            timeout=30,
            check_same_thread=False,
            isolation_level=None  # Autocommit mode - we manage transactions manually
        )
        # Enable WAL mode for concurrent access
        local.conn.execute("PRAGMA journal_mode=WAL")
        local.conn.execute("PRAGMA busy_timeout=30000")
    return local.conn

def backup_database():
    """
    FIX ISSUE #20: Backup database before operations.
    Keeps last 7 daily backups.
    """
    if not DB_PATH.exists():
        return
    
    try:
        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(exist_ok=True)
        
        # Create backup with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = backup_dir / f"polymarket_insider_{timestamp}.db"
        
        shutil.copy2(DB_PATH, backup_path)
        print(f"[{datetime.now()}] ✓ Database backed up to {backup_path}")
        
        # Cleanup old backups (keep last 7 days)
        cleanup_old_backups(backup_dir, days=7)
        
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Backup failed: {e}")

def cleanup_old_backups(backup_dir: Path, days: int = 7):
    """Remove backups older than specified days"""
    try:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)
        
        for backup_file in backup_dir.glob("polymarket_insider_*.db"):
            if backup_file.stat().st_mtime < cutoff.timestamp():
                backup_file.unlink()
                print(f"[{datetime.now()}] 🗑️ Removed old backup: {backup_file.name}")
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️ Cleanup failed: {e}")

def init_database():
    """
    Initialize database tables with proper constraints and indexes.
    FIX BUG #3: Add error handling.
    FIX ISSUE #9: Add indexes for performance.
    FIX ISSUE #16: Add data validation constraints.
    """
    try:
        # FIX ISSUE #20: Backup before init
        backup_database()
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Wallet performance tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallet_performance (
                wallet TEXT PRIMARY KEY,
                total_trades INTEGER DEFAULT 0 CHECK(total_trades >= 0),
                pre_event_trades INTEGER DEFAULT 0 CHECK(pre_event_trades >= 0),
                total_volume REAL DEFAULT 0 CHECK(total_volume >= 0),
                avg_latency_seconds REAL DEFAULT 0 CHECK(avg_latency_seconds >= 0),
                insider_score REAL DEFAULT 0 CHECK(insider_score >= 0 AND insider_score <= 100),
                classification TEXT DEFAULT 'Unknown',
                first_seen TIMESTAMP,
                last_updated TIMESTAMP
            )
        """)
        
        # Individual trade history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_timestamp TIMESTAMP NOT NULL,
                event_timestamp TIMESTAMP,
                latency_seconds REAL CHECK(latency_seconds >= 0),
                position TEXT,
                size REAL NOT NULL CHECK(size > 0),
                odds REAL NOT NULL CHECK(odds >= 0 AND odds <= 1),
                is_pre_event INTEGER DEFAULT 0 CHECK(is_pre_event IN (0, 1)),
                trade_hash TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Alert history (avoid duplicate alerts)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_hash TEXT UNIQUE,
                alert_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                insider_score REAL,
                latency_seconds REAL,
                sent INTEGER DEFAULT 0 CHECK(sent IN (0, 1))
            )
        """)
        
        # FIX ISSUE #9: Add indexes for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trade_wallet 
            ON trade_history(wallet)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trade_timestamp 
            ON trade_history(trade_timestamp DESC)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_wallet 
            ON alert_history(wallet)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_hash 
            ON alert_history(trade_hash)
        """)
        
        # Schema versioning for future migrations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Set initial version
        cursor.execute("INSERT OR IGNORE INTO schema_version VALUES (1, ?)", (datetime.now(timezone.utc),))
        
        conn.commit()
        print(f"[{datetime.now()}] ✓ Database initialized at {DB_PATH}")
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database initialization failed: {e}")
        raise

def get_wallet_stats(wallet: str) -> Optional[Dict]:
    """
    Get wallet performance statistics.
    FIX BUG #3: Add comprehensive error handling.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                total_trades, pre_event_trades, 
                total_volume, avg_latency_seconds, 
                insider_score, classification,
                first_seen, last_updated
            FROM wallet_performance
            WHERE wallet = ?
        """, (wallet,))
        
        row = cursor.fetchone()
        
        if row:
            return {
                'total_trades': row[0],
                'pre_event_trades': row[1],
                'total_volume': row[2],
                'avg_latency_seconds': row[3],
                'insider_score': row[4],
                'classification': row[5],
                'first_seen': row[6],
                'last_updated': row[7]
            }
        
        return None
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in get_wallet_stats: {e}")
        return None

def update_wallet_stats(wallet: str, trade_data: Dict):
    """
    Update wallet statistics with new trade.
    FIX BUG #4: Add transaction locks to prevent race conditions.
    FIX BUG #5: Remove outcome/profit tracking (Phase 1 doesn't know outcomes).
    """
    try:
        conn = get_db_connection()
        
        # FIX BUG #4: Use exclusive transaction to prevent race conditions
        conn.execute("BEGIN EXCLUSIVE")
        
        cursor = conn.cursor()
        
        # Get current stats with row lock
        cursor.execute("""
            SELECT total_trades, pre_event_trades, total_volume, avg_latency_seconds
            FROM wallet_performance
            WHERE wallet = ?
        """, (wallet,))
        
        row = cursor.fetchone()
        
        if row:
            # Update existing wallet
            total_trades = row[0] + 1
            pre_event_trades = row[1] + (1 if trade_data.get('is_pre_event') else 0)
            total_volume = row[2] + trade_data.get('size', 0)
            old_avg_latency = row[3]
            
            # Update latency average
            if trade_data.get('latency_seconds') and trade_data['latency_seconds'] > 0:
                avg_latency = (old_avg_latency * row[0] + trade_data['latency_seconds']) / total_trades
            else:
                avg_latency = old_avg_latency
            
            # Calculate insider score (simplified for Phase 1)
            insider_score = calculate_insider_score(
                pre_event_trades=pre_event_trades,
                total_trades=total_trades,
                avg_latency=avg_latency
            )
            
            # Classify wallet
            classification = classify_wallet(insider_score, pre_event_trades, total_trades)
            
            cursor.execute("""
                UPDATE wallet_performance 
                SET total_trades = ?, pre_event_trades = ?,
                    total_volume = ?, avg_latency_seconds = ?,
                    insider_score = ?, classification = ?,
                    last_updated = ?
                WHERE wallet = ?
            """, (
                total_trades, pre_event_trades,
                total_volume, avg_latency,
                insider_score, classification,
                datetime.now(timezone.utc), wallet
            ))
        else:
            # Insert new wallet
            cursor.execute("""
                INSERT INTO wallet_performance 
                (wallet, total_trades, pre_event_trades, total_volume, 
                 avg_latency_seconds, first_seen, last_updated, insider_score, classification)
                VALUES (?, 1, ?, ?, ?, ?, ?, 0, 'New')
            """, (
                wallet,
                1 if trade_data.get('is_pre_event') else 0,
                trade_data.get('size', 0),
                trade_data.get('latency_seconds', 0),
                datetime.now(timezone.utc),
                datetime.now(timezone.utc)
            ))
        
        conn.commit()
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in update_wallet_stats: {e}")
        conn.rollback()
        raise
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error in update_wallet_stats: {e}")
        conn.rollback()

def save_trade(trade_data: Dict) -> bool:
    """
    Save individual trade to history.
    FIX BUG #3: Add error handling.
    FIX BUG #8: Use timezone-aware timestamps.
    FIX ISSUE #16: Validate data before saving.
    """
    try:
        # FIX ISSUE #16: Validate data
        if trade_data.get('size', 0) <= 0:
            print(f"[{datetime.now()}] ⚠️ Invalid trade size: {trade_data.get('size')}")
            return False
        
        odds = trade_data.get('odds', 0)
        if not (0 <= odds <= 1):
            print(f"[{datetime.now()}] ⚠️ Invalid odds: {odds}")
            return False
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO trade_history 
            (wallet, market, trade_timestamp, event_timestamp, latency_seconds,
             position, size, odds, is_pre_event, trade_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_data.get('wallet'),
            trade_data.get('market'),
            trade_data.get('trade_timestamp'),
            trade_data.get('event_timestamp'),
            trade_data.get('latency_seconds'),
            trade_data.get('position'),
            trade_data.get('size'),
            trade_data.get('odds'),
            1 if trade_data.get('is_pre_event') else 0,
            trade_data.get('trade_hash')
        ))
        
        conn.commit()
        return True
        
    except sqlite3.IntegrityError:
        # Trade already exists (duplicate)
        return False
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in save_trade: {e}")
        return False

def is_alert_sent(wallet: str, trade_hash: str) -> bool:
    """
    Check if alert already sent for this trade.
    FIX BUG #3: Add error handling.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id FROM alert_history 
            WHERE wallet = ? AND trade_hash = ?
        """, (wallet, trade_hash))
        
        exists = cursor.fetchone() is not None
        return exists
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in is_alert_sent: {e}")
        return False

def mark_alert_sent(wallet: str, market: str, trade_hash: str, insider_score: float, latency_seconds: float = None):
    """
    Mark alert as sent.
    FIX BUG #3: Add error handling.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR IGNORE INTO alert_history 
            (wallet, market, trade_hash, alert_timestamp, insider_score, latency_seconds, sent)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (wallet, market, trade_hash, datetime.now(timezone.utc), insider_score, latency_seconds))
        
        conn.commit()
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in mark_alert_sent: {e}")

def get_recent_alerts_for_market(market: str, hours: int = 6) -> List[Dict]:
    """
    Get recent alerts for a specific market (for coordinated attack detection).
    Returns list of alerts within the last N hours.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Calculate cutoff time
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        
        cursor.execute("""
            SELECT wallet, market, alert_timestamp, insider_score, latency_seconds
            FROM alert_history
            WHERE market LIKE ? AND alert_timestamp >= ? AND sent = 1
            ORDER BY alert_timestamp DESC
        """, (f'%{market}%', cutoff))
        
        rows = cursor.fetchall()
        
        alerts = []
        for row in rows:
            # Parse trade_hash to get amount (if stored)
            # For now, just return basic info
            alerts.append({
                'wallet': row[0],
                'market': row[1],
                'timestamp': row[2],
                'score': row[3],
                'latency': row[4]
            })
        
        return alerts
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in get_recent_alerts_for_market: {e}")
        return []

def calculate_insider_score(pre_event_trades: int, total_trades: int, avg_latency: float) -> float:
    """
    Calculate insider probability score (0-100).
    FIX BUG #5: Simplified for Phase 1 (no outcome data yet).
    
    Weights:
    - Pre-event ratio: 50%
    - Latency: 50%
    """
    # Pre-event ratio score
    pre_event_ratio = (pre_event_trades / total_trades) if total_trades > 0 else 0
    pre_event_score = min(pre_event_ratio * 100, 100) * 0.50
    
    # Latency score (higher latency = higher score)
    # >30 min = max score
    latency_score = min(avg_latency / 1800 * 100, 100) * 0.50 if avg_latency > 0 else 0
    
    total_score = pre_event_score + latency_score
    return round(total_score, 2)

def classify_wallet(insider_score: float, pre_event_trades: int, total_trades: int) -> str:
    """Classify wallet based on insider score"""
    if total_trades < 3:
        return "New"
    elif insider_score >= 80:
        return "Probable Insider"
    elif insider_score >= 60:
        return "Syndicate/Whale"
    elif insider_score >= 30:
        return "Professional"
    else:
        return "Retail"

def get_top_insiders(limit: int = 10) -> List[Dict]:
    """
    Get top insider wallets by score.
    FIX BUG #3: Add error handling.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT wallet, insider_score, total_trades, pre_event_trades, 
                   classification
            FROM wallet_performance
            WHERE total_trades >= 3
            ORDER BY insider_score DESC
            LIMIT ?
        """, (limit,))
        
        rows = cursor.fetchall()
        
        return [
            {
                'wallet': row[0],
                'insider_score': row[1],
                'total_trades': row[2],
                'pre_event_trades': row[3],
                'classification': row[4]
            }
            for row in rows
        ]
        
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Database error in get_top_insiders: {e}")
        return []

def vacuum_database():
    """
    Compact database to reclaim space.
    Should be run weekly.
    """
    try:
        conn = get_db_connection()
        conn.execute("VACUUM")
        print(f"[{datetime.now()}] ✓ Database vacuumed")
    except sqlite3.Error as e:
        print(f"[{datetime.now()}] ❌ Vacuum failed: {e}")
