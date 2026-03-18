import json
import os
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple

from detector import detect_insider_trades
from notifier import send_telegram_alert, send_top_trader_alert
from top_traders import get_tracked_wallets, fetch_trader_recent_trades


def send_heartbeat(stats: Dict) -> None:
    """
    Send a lightweight status ping to Telegram after each cycle.
    Only sends if HEARTBEAT_ENABLED env var is set (opt-in).
    Silent failure — heartbeat should never crash the main flow.
    """
    try:
        if not os.getenv("HEARTBEAT_ENABLED"):
            return
        
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return

        trades = stats.get("trades_analyzed", 0)
        insiders = stats.get("insider_signals", 0)
        copies = stats.get("copy_candidates", 0)
        insider_sent = stats.get("insider_alerts_sent", 0)
        top_sent = stats.get("top_trader_alerts_sent", 0)
        elapsed = stats.get("elapsed_seconds", 0)
        errors = stats.get("errors", 0)

        # Only send detailed heartbeat if something happened or there were errors
        if insider_sent == 0 and top_sent == 0 and errors == 0:
            # Silent cycle — no need to spam
            return

        status = "⚠️" if errors > 0 else "✅"
        msg = (
            f"{status} Heartbeat | {datetime.utcnow().strftime('%H:%M')} UTC\n"
            f"Trades: {trades} | Signals: {insiders} | Sent: {insider_sent}+{top_sent}\n"
            f"Time: {elapsed:.0f}s"
        )
        if errors > 0:
            msg += f" | Errors: {errors}"

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={
            "chat_id": chat_id,
            "text": msg,
            "disable_notification": True,
        }, timeout=5)
    except Exception:
        pass  # Heartbeat must never crash main


def load_tracked_wallets():
    """Load tracked trade hashes (not wallets - we want alerts for each trade)."""
    path = Path("tracked_wallets.json")
    if path.exists():
        try:
            with open(path, "r") as f:
                data = json.load(f)
                # Migration: if old format (list of wallets), convert to new format
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], str) and data[0].startswith("0x"):
                    return {"wallets": data, "trade_hashes": []}
                if isinstance(data, dict):
                    return data
                return {"wallets": [], "trade_hashes": []}
        except Exception:
            return {"wallets": [], "trade_hashes": []}
    return {"wallets": [], "trade_hashes": []}


def save_tracked_wallets(tracked_data):
    path = Path("tracked_wallets.json")
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w") as f:
        json.dump(tracked_data, f, indent=2)
    temp_path.replace(path)


def load_alerts():
    path = Path("alerts.json")
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_alerts(alerts):
    path = Path("alerts.json")
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w") as f:
        json.dump(alerts, f, indent=2)
    temp_path.replace(path)


def _evaluate_financial_analyst_view(alert: Dict) -> Dict:
    """Create a compact financial-analyst view for execution/risk decisions."""
    analysis = alert.get("analysis", {})
    combined = alert.get("combined_signal", {})
    mispricing = alert.get("mispricing", {})
    irrationality = alert.get("irrationality", {})
    trade_data = alert.get("trade_data", {})

    edge_percent = float(mispricing.get("edge_percent", 0))
    insider_score = float(analysis.get("score", 0))
    signal_strength = float(combined.get("signal_strength", insider_score))
    irrationality_score = float(irrationality.get("irrationality_score", 0))
    amount = float(trade_data.get("amount", analysis.get("amount", 0)) or 0)

    quality = 0
    if combined.get("signal_type") == "ALPHA":
        quality += 35
    elif combined.get("signal_type") == "INSIDER_CONFIRMED":
        quality += 25
    elif combined.get("signal_type") == "INSIDER_ONLY":
        quality += 5

    quality += min(25, max(0, edge_percent * 1.5))
    quality += min(20, insider_score / 5)
    quality += min(20, irrationality_score / 5)
    quality = round(min(100, quality), 1)

    if quality >= 75:
        stance = "HIGH_CONVICTION"
    elif quality >= 55:
        stance = "SELECTIVE"
    else:
        stance = "WATCH_ONLY"

    if amount >= 10000 and stance == "HIGH_CONVICTION":
        risk_note = "High stake detected — copy with reduced sizing (25-40% of source risk)."
    elif amount > 0:
        risk_note = "Use fixed fractional risk (1-2% bankroll) and avoid averaging down."
    else:
        risk_note = "Insufficient sizing data — treat as exploratory signal."

    return {
        "signal_quality": quality,
        "stance": stance,
        "edge_percent": round(edge_percent, 2),
        "signal_strength": signal_strength,
        "insider_score": insider_score,
        "risk_note": risk_note,
    }


def _split_by_goals(alerts: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """
    Goal 1: Find insiders.
    Goal 2: Find irrational trades worth copying.
    """
    insiders: List[Dict] = []
    irrational_copy_candidates: List[Dict] = []

    for alert in alerts:
        combined = alert.get("combined_signal", {})
        mispricing = alert.get("mispricing", {})

        analyst_view = _evaluate_financial_analyst_view(alert)
        alert["financial_analyst"] = analyst_view

        # Goal 1: all meaningful insider alerts (existing detector already pre-filtered)
        insiders.append(alert)

        # Goal 2: copy only when there is both informational + pricing edge
        signal_type = combined.get("signal_type", "")
        edge_percent = float(mispricing.get("edge_percent", 0))
        stance = analyst_view["stance"]

        is_copy_candidate = (
            signal_type in {"ALPHA", "INSIDER_CONFIRMED"}
            and edge_percent >= 3.0
            and stance in {"HIGH_CONVICTION", "SELECTIVE"}
        )

        if is_copy_candidate:
            irrational_copy_candidates.append(alert)

    return insiders, irrational_copy_candidates


def _print_goal_summary(insiders: List[Dict], irrational_copy_candidates: List[Dict]) -> None:
    print(f"[{datetime.now()}] 🎯 Goal #1 (find insiders): {len(insiders)} signals")
    print(
        f"[{datetime.now()}] 🎯 Goal #2 (irrational trades to copy): "
        f"{len(irrational_copy_candidates)} candidates"
    )

    if irrational_copy_candidates:
        print(f"[{datetime.now()}] Top copy candidates (financial analyst view):")
        sorted_candidates = sorted(
            irrational_copy_candidates,
            key=lambda x: x.get("financial_analyst", {}).get("signal_quality", 0),
            reverse=True,
        )
        for idx, candidate in enumerate(sorted_candidates[:5], start=1):
            fa = candidate.get("financial_analyst", {})
            sig = candidate.get("combined_signal", {})
            market = candidate.get("market", "Unknown market")
            print(
                f"  {idx}. {market[:90]} | {sig.get('signal_type', 'N/A')} | "
                f"quality {fa.get('signal_quality', 0)}/100 | edge {fa.get('edge_percent', 0):+.1f}%"
            )


def scan_top_traders(tracked_hashes: set) -> List[Dict]:
    """
    Scan top traders for recent activity.
    Returns list of alerts for new positions from top traders.
    """
    print(f"[{datetime.now()}] 👑 Scanning top traders...")
    
    try:
        top_wallets = get_tracked_wallets()
        if not top_wallets:
            print(f"[{datetime.now()}] No top traders meet criteria")
            return []
        
        print(f"[{datetime.now()}] Tracking {len(top_wallets)} top traders")
        
        alerts = []
        total_trades_found = 0
        traders_with_trades = 0
        
        for address, trader_info in list(top_wallets.items())[:20]:  # Limit to top 20 to avoid rate limits
            trades = fetch_trader_recent_trades(address, minutes_back=60)  # Increased to 60 min
            
            if trades:
                traders_with_trades += 1
                total_trades_found += len(trades)
            
            for trade in trades:
                trade_hash = trade.get('transactionHash', '')
                
                # Skip if already alerted
                if trade_hash in tracked_hashes:
                    continue
                
                # Get price/odds
                price = float(trade.get('price', 0))
                outcome = trade.get('outcome', 'Yes').lower()
                
                # Calculate effective odds
                if outcome == 'no':
                    effective_odds = 1 - price
                else:
                    effective_odds = price
                
                # Skip extreme odds (97%+ or 3%-) - near zero profit potential
                if effective_odds >= 0.97 or effective_odds <= 0.03:
                    continue
                
                # Build alert
                amount = float(trade.get('size', 0)) * price
                if amount < 1500:  # Skip small trades (filter noise from top traders)
                    continue
                
                # Get market name from trade data
                # API returns 'title' and 'slug' directly on trade, not nested in 'market'
                market_name = trade.get('title', '') or trade.get('market', {}).get('question', 'Unknown market')
                market_slug = trade.get('eventSlug', '') or trade.get('slug', '') or trade.get('market', {}).get('slug', '')
                
                alert = {
                    'type': 'TOP_TRADER',
                    'trade_hash': trade_hash,
                    'wallet': address,
                    'trader': trader_info,
                    'trade': trade,
                    'market': market_name,
                    'market_slug': market_slug,
                    'amount': amount,
                }
                alerts.append(alert)
                print(f"[{datetime.now()}] 👑 Top trader #{trader_info['rank']} trade: ${amount:,.0f} on {market_name[:50]}")
        
        print(f"[{datetime.now()}] 🎯 Goal #3: {traders_with_trades}/20 traders had trades, {total_trades_found} total trades, {len(alerts)} alerts")
        return alerts
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Error scanning top traders: {e}")
        import traceback
        traceback.print_exc()
        return []


def main():
    start_time = datetime.now()
    print(f"[{start_time}] Starting Polymarket insider detector...")
    
    # Preload leaderboard cache once to avoid repeated API calls
    print(f"[{datetime.now()}] Preloading top traders leaderboard...")
    _ = get_tracked_wallets()  # This caches the result for the entire run

    tracked_data = load_tracked_wallets()
    tracked_hashes = set(tracked_data.get("trade_hashes", []))
    tracked_wallets = set(tracked_data.get("wallets", []))  # keep for stats
    existing_alerts = load_alerts()

    # === GOAL 1 & 2: Insider detection + Irrational trades ===
    new_alerts = detect_insider_trades()
    insiders, irrational_copy_candidates = _split_by_goals(new_alerts)
    _print_goal_summary(insiders, irrational_copy_candidates)

    sent_count = 0
    for alert in insiders:
        trade_hash = alert.get("trade_hash", "")
        wallet = alert["wallet"]

        # Deduplicate by trade_hash (not wallet) - allows multiple alerts per wallet
        if trade_hash and trade_hash in tracked_hashes:
            print(f"[{datetime.now()}] Trade {trade_hash[:12]}... already alerted, skipping")
            continue

        if send_telegram_alert(alert):
            if trade_hash:
                tracked_hashes.add(trade_hash)
            tracked_wallets.add(wallet)
            existing_alerts.append(alert)
            sent_count += 1
            print(f"[{datetime.now()}] ✅ Alert sent for trade {trade_hash[:12]}... (wallet {wallet[:8]}...)")
        else:
            print(f"[{datetime.now()}] ❌ Failed to send alert for {wallet[:8]}...")

    # === GOAL 3: Top Traders ===
    top_trader_alerts = scan_top_traders(tracked_hashes)
    top_trader_sent = 0
    
    for alert in top_trader_alerts:
        trade_hash = alert.get("trade_hash", "")
        
        if trade_hash and trade_hash in tracked_hashes:
            continue
        
        if send_top_trader_alert(alert):
            if trade_hash:
                tracked_hashes.add(trade_hash)
            existing_alerts.append(alert)
            top_trader_sent += 1
            print(f"[{datetime.now()}] ✅ Top trader alert sent: #{alert['trader']['rank']}")
        else:
            print(f"[{datetime.now()}] ❌ Failed to send top trader alert")

    tracked_data = {
        "wallets": list(tracked_wallets),
        "trade_hashes": list(tracked_hashes),
    }
    save_tracked_wallets(tracked_data)
    save_alerts(existing_alerts)

    print(
        f"[{datetime.now()}] Completed. "
        f"Insider signals: {len(insiders)}, copy candidates: {len(irrational_copy_candidates)}, "
        f"insider alerts sent: {sent_count}, top trader alerts: {top_trader_sent}"
    )

    # === HEARTBEAT ===
    elapsed = (datetime.now() - start_time).total_seconds()
    send_heartbeat({
        "trades_analyzed": len(new_alerts),
        "insider_signals": len(insiders),
        "copy_candidates": len(irrational_copy_candidates),
        "insider_alerts_sent": sent_count,
        "top_trader_alerts_sent": top_trader_sent,
        "elapsed_seconds": elapsed,
        "errors": 0,
    })


if __name__ == "__main__":
    main()
