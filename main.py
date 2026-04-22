import json
import os
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

from detector import detect_insider_trades
from notifier import send_telegram_alert, send_top_trader_alert
from top_traders import get_tracked_wallets, fetch_trader_recent_trades
import trade_economics


def extract_ai_verdict(ai_context: str) -> str:
    """Extract COPY/SKIP verdict from AI response text."""
    if not ai_context:
        return "NONE"
    upper = ai_context.upper()
    if "LEAN COPY" in upper:
        return "LEAN_COPY"
    elif "LEAN SKIP" in upper:
        return "LEAN_SKIP"
    elif "COPY" in upper and "SKIP" not in upper:
        return "COPY"
    elif "SKIP" in upper:
        return "SKIP"
    return "UNCLEAR"


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

        # Only send heartbeat on errors — successful alerts already notify the chat
        if errors == 0:
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
    # Rotate trade_hashes: keep only last 5000 to prevent infinite growth
    MAX_HASHES = 5000
    hashes = tracked_data.get("trade_hashes", [])
    if len(hashes) > MAX_HASHES:
        tracked_data["trade_hashes"] = hashes[-MAX_HASHES:]
    
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
    # Rotate: keep only the last 500 alerts to prevent infinite growth
    # alerts.json was already 1.2MB — this caps it at ~300KB
    MAX_ALERTS = 500
    if len(alerts) > MAX_ALERTS:
        alerts = alerts[-MAX_ALERTS:]
    
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
        # Debug counters
        tt_dedup = 0
        tt_extreme_odds = 0
        tt_small = 0
        tt_crypto = 0
        tt_low_roi = 0
        
        for address, trader_info in list(top_wallets.items())[:20]:  # Limit to top 20 to avoid rate limits
            trades = fetch_trader_recent_trades(address, minutes_back=60)  # Increased to 60 min
            
            if trades:
                traders_with_trades += 1
                total_trades_found += len(trades)
            
            for trade in trades:
                trade_hash = trade.get('transactionHash', '')
                
                # Skip if already alerted
                if trade_hash in tracked_hashes:
                    tt_dedup += 1
                    continue
                
                # ══════════════════════════════════════════
                # TOP TRADER API: price = specific token price
                # NOT YES-token price like general trades API.
                # cost = size × price ALWAYS. No YES/NO conversion needed.
                # ══════════════════════════════════════════
                price = float(trade.get('price', 0))
                outcome = trade.get('outcome', 'Yes')
                size = float(trade.get('size', 0))
                
                cost = size * price
                effective_odds = price
                
                # Skip extreme odds (97%+ or 3%-) - near zero profit potential
                if effective_odds >= 0.97 or effective_odds <= 0.03:
                    tt_extreme_odds += 1
                    continue
                
                if cost < 1000:  # Skip small trades (match MIN_BET_SIZE)
                    tt_small += 1
                    continue
                
                # Skip daily crypto/price markets
                market_name = trade.get('title', '') or trade.get('market', {}).get('question', 'Unknown market')
                title_lower = market_name.lower()
                
                if 'up or down' in title_lower:
                    tt_crypto += 1
                    continue
                
                crypto_kw = ['bitcoin', 'ethereum', 'solana', 'btc', 'eth', 'crypto']
                price_kw = ['above', 'below', 'less than', 'more than', 'price',
                            'dip to', 'hit', 'drop to', 'fall to', 'rise to',
                            'reach', 'crash']
                if any(k in title_lower for k in crypto_kw) and any(k in title_lower for k in price_kw):
                    tt_crypto += 1
                    continue
                
                # Skip low ROI trades (>93% odds)
                if effective_odds >= 0.93:
                    tt_low_roi += 1
                    continue
                market_slug = trade.get('eventSlug', '') or trade.get('slug', '') or trade.get('market', {}).get('slug', '')
                
                alert = {
                    'type': 'TOP_TRADER',
                    'trade_hash': trade_hash,
                    'wallet': address,
                    'trader': trader_info,
                    'trade': trade,
                    'market': market_name,
                    'market_slug': market_slug,
                    'amount': cost,
                }
                alerts.append(alert)
                print(f"[{datetime.now()}] 👑 Top trader #{trader_info['rank']} trade: ${cost:,.0f} {outcome} @ {effective_odds*100:.0f}% on {market_name[:50]}")
        
        print(f"[{datetime.now()}] 🎯 Goal #3: {traders_with_trades}/20 traders had trades, {total_trades_found} total trades, {len(alerts)} alerts")
        print(f"[{datetime.now()}]   Filtered: dedup={tt_dedup}, extreme_odds={tt_extreme_odds}, small(<$1.5K)={tt_small}, crypto={tt_crypto}, low_roi(>93%)={tt_low_roi}")
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
    new_alerts, raw_trades, markets = detect_insider_trades()
    insiders, irrational_copy_candidates = _split_by_goals(new_alerts)
    _print_goal_summary(insiders, irrational_copy_candidates)

    sent_count = 0
    log_only_count = 0
    for alert in insiders:
        trade_hash = alert.get("trade_hash", "")
        wallet = alert["wallet"]

        # Deduplicate by trade_hash (not wallet) - allows multiple alerts per wallet
        if trade_hash and trade_hash in tracked_hashes:
            print(f"[{datetime.now()}] Trade {trade_hash[:12]}... already alerted, skipping")
            continue

        # LOG_ONLY alerts: save for resolution tracking but skip Telegram
        if alert.get("log_only"):
            if trade_hash:
                tracked_hashes.add(trade_hash)
            existing_alerts.append(alert)
            log_only_count += 1
            print(f"[{datetime.now()}] 📋 LOG_ONLY: {alert.get('market', '')[:60]} (saved, no Telegram)")
            continue

        # Generate AI context for insider alerts
        try:
            from ai_context import generate_trade_context
            
            trade_data = alert.get('trade_data', {})
            outcome_name = trade_data.get('outcome', 'Yes')
            market_name = alert.get('market', '')
            # General trades API: raw_price = YES token price
            raw_price = float(trade_data.get('price', 0.5))
            # For second-team outcomes: their token price = 1 - raw_price
            effective_price = (1 - raw_price) if trade_data.get('is_no') else raw_price
            odds_pct = effective_price * 100
            
            context = generate_trade_context(
                market_title=market_name,
                outcome=str(outcome_name),
                odds_pct=odds_pct,
                amount=float(alert.get('analysis', {}).get('amount', 0)),
            )
            if context:
                alert['ai_context'] = context
                alert['ai_verdict'] = extract_ai_verdict(context)
                print(f"[{datetime.now()}] 🤖 AI: {context[:80]}")
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️  AI skipped: {e}")

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
        
        # Generate AI context (cheap, ~$0.003 per call)
        try:
            from ai_context import generate_trade_context
            
            trade = alert.get('trade', {})
            outcome_name = trade.get('outcome', 'Unknown')
            market_name = alert.get('market', '')
            price = float(trade.get('price', 0.5))
            # price IS the specific token price — no conversion needed
            odds_pct = price * 100
            
            context = generate_trade_context(
                market_title=market_name,
                outcome=str(outcome_name),
                odds_pct=odds_pct,
                trader_rank=alert.get('trader', {}).get('rank', 0),
                amount=alert.get('amount', 0),
            )
            if context:
                alert['ai_context'] = context
                alert['ai_verdict'] = extract_ai_verdict(context)
                print(f"[{datetime.now()}] 🤖 AI: {context[:80]}")
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️  AI skipped: {e}")
        
        if send_top_trader_alert(alert):
            if trade_hash:
                tracked_hashes.add(trade_hash)
            existing_alerts.append(alert)
            top_trader_sent += 1
            print(f"[{datetime.now()}] ✅ Top trader alert sent: #{alert['trader']['rank']}")
        else:
            print(f"[{datetime.now()}] ❌ Failed to send top trader alert")

    # === GOAL 4: Whale Watch ===
    whale_sent = 0
    whale_cooldown = {}
    whale_cooldown_path = Path("whale_cooldown.json")
    try:
        from whale_watch import analyze_whale_flows, format_whale_alert
        
        if raw_trades and markets:
            whale_signals = analyze_whale_flows(raw_trades, markets)
            
            # Load whale cooldown (prevent same market alerting more than once per 24h)
            try:
                if whale_cooldown_path.exists():
                    with open(whale_cooldown_path) as f:
                        whale_cooldown = json.load(f)
                else:
                    whale_cooldown = {}
            except:
                whale_cooldown = {}
            
            for signal in whale_signals[:3]:  # Max 3 whale alerts per run
                # Dedup by market — 24h cooldown
                whale_key = f"whale_{signal['condition_id']}_{signal['dominant_side']}"
                
                # Check tracked_hashes (within-run dedup)
                if whale_key in tracked_hashes:
                    print(f"[{datetime.now()}] 🐋 Whale dedup (hash): {signal['market'][:40]}")
                    continue
                
                # Check 24h cooldown
                last_sent = whale_cooldown.get(whale_key)
                if last_sent:
                    hours_ago = (datetime.utcnow() - datetime.fromisoformat(last_sent)).total_seconds() / 3600
                    if hours_ago < 24:
                        print(f"[{datetime.now()}] 🐋 Whale cooldown ({hours_ago:.0f}h ago): {signal['market'][:40]}")
                        continue
                
                # Generate AI context
                try:
                    from ai_context import generate_trade_context
                    context = generate_trade_context(
                        market_title=signal['market'],
                        outcome=signal['dominant_side'],
                        odds_pct=(signal.get('yes_price', 0.5) or 0.5) * 100,
                        amount=signal['dominant_volume'],
                    )
                    if context:
                        signal['ai_context'] = context
                except Exception as e:
                    print(f"[{datetime.now()}] ⚠️  Whale AI skipped: {e}")
                
                # Send Telegram
                msg = format_whale_alert(signal)
                if signal.get('ai_context'):
                    msg += f"\n🤖 AI\n→ {signal['ai_context']}"
                
                msg += f"\nPolymarket Insiders | {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
                
                print(f"[{datetime.now()}] 🐋 Sending whale alert ({len(msg)} chars)...")
                
                try:
                    token = os.getenv("TELEGRAM_BOT_TOKEN")
                    chat_id = os.getenv("TELEGRAM_CHAT_ID")
                    if token and chat_id:
                        url = f"https://api.telegram.org/bot{token}/sendMessage"
                        resp = requests.post(url, json={
                            "chat_id": chat_id,
                            "text": msg,
                            "disable_web_page_preview": True,
                        }, timeout=10)
                        if resp.status_code == 200:
                            tracked_hashes.add(whale_key)
                            whale_cooldown[whale_key] = datetime.utcnow().isoformat()
                            existing_alerts.append(signal)
                            whale_sent += 1
                            print(f"[{datetime.now()}] ✅ Whale alert sent: ${signal['dominant_volume']:,.0f} {signal['dominant_side']} on {signal['market'][:40]}")
                        else:
                            print(f"[{datetime.now()}] ❌ Whale Telegram error: {resp.status_code} {resp.text[:100]}")
                    else:
                        print(f"[{datetime.now()}] ❌ Whale: missing TELEGRAM_BOT_TOKEN or CHAT_ID")
                except Exception as e:
                    print(f"[{datetime.now()}] ❌ Whale alert failed: {e}")
    except Exception as e:
        print(f"[{datetime.now()}] ⚠️  Whale watch error: {e}")
        import traceback
        traceback.print_exc()
    
    # Save whale cooldown (clean entries older than 7 days)
    try:
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        whale_cooldown = {k: v for k, v in whale_cooldown.items() if v > cutoff}
        with open(whale_cooldown_path, "w") as f:
            json.dump(whale_cooldown, f, indent=2)
    except Exception:
        pass

    tracked_data = {
        "wallets": list(tracked_wallets),
        "trade_hashes": list(tracked_hashes),
    }
    save_tracked_wallets(tracked_data)
    save_alerts(existing_alerts)

    print(
        f"[{datetime.now()}] Completed. "
        f"Insider signals: {len(insiders)}, copy candidates: {len(irrational_copy_candidates)}, "
        f"insider alerts sent: {sent_count}, log_only (CONFLICT): {log_only_count}, "
        f"top trader alerts: {top_trader_sent}, whale alerts: {whale_sent}"
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
