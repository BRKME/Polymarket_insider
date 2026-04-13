"""
Resolution Tracker — Closes the feedback loop.

Runs daily (via GitHub Actions). For each alert in alerts.json:
1. Checks if the market has resolved via Gamma API
2. Records the outcome (YES/NO)
3. Scores: did the insider's bet win?
4. Scores: was the model's mispricing call correct?
5. Saves stats to resolution_stats.json and prints summary to Telegram

This is essential for validating the system.
Without it, we cannot know if our signals are profitable.
"""

import json
import time
import requests
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from config import GAMMA_API_URL, REQUEST_DELAY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

ALERTS_PATH = Path("alerts.json")
STATS_PATH = Path("resolution_stats.json")

# Rate limiting
API_DELAY = 0.5  # seconds between API calls


def load_alerts() -> List[Dict]:
    if ALERTS_PATH.exists():
        with open(ALERTS_PATH) as f:
            return json.load(f)
    return []


def save_alerts(alerts: List[Dict]):
    temp = ALERTS_PATH.with_suffix(".tmp")
    with open(temp, "w") as f:
        json.dump(alerts, f, indent=2)
    temp.replace(ALERTS_PATH)


def load_stats() -> Dict:
    if STATS_PATH.exists():
        with open(STATS_PATH) as f:
            return json.load(f)
    return {
        "last_run": None,
        "total_checked": 0,
        "total_resolved": 0,
        "total_unresolved": 0,
        "insider_wins": 0,
        "insider_losses": 0,
        "model_correct": 0,
        "model_wrong": 0,
        "model_na": 0,
        "by_signal_type": {},
        "by_category": {},
        "history": [],
    }


def save_stats(stats: Dict):
    temp = STATS_PATH.with_suffix(".tmp")
    with open(temp, "w") as f:
        json.dump(stats, f, indent=2)
    temp.replace(STATS_PATH)


def fetch_market_by_condition_id(condition_id: str) -> Optional[Dict]:
    """Fetch market by conditionId — most reliable lookup for TOP_TRADER alerts."""
    if not condition_id:
        return None

    url = f"{GAMMA_API_URL}/markets"
    params = {"condition_id": condition_id, "limit": 1}

    try:
        time.sleep(API_DELAY)
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                return data[0]
    except Exception as e:
        print(f"  ⚠️  API error for conditionId '{condition_id[:20]}': {e}")

    # Fallback: try conditionId as query param name variation
    for param_name in ["conditionId", "condition_id"]:
        try:
            time.sleep(API_DELAY)
            resp = requests.get(url, params={param_name: condition_id, "limit": 1}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    return data[0]
        except:
            pass

    return None


def fetch_market_by_slug(slug: str) -> Optional[Dict]:
    """Fetch market data from Gamma API by slug."""
    if not slug:
        return None

    url = f"{GAMMA_API_URL}/markets"
    params = {"slug": slug, "limit": 1}

    try:
        time.sleep(API_DELAY)
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data and len(data) > 0:
                return data[0]
    except Exception as e:
        print(f"  ⚠️  API error for slug '{slug[:40]}': {e}")

    # Fallback: try event slug via events endpoint
    try:
        time.sleep(API_DELAY)
        resp = requests.get(f"{GAMMA_API_URL}/events", params={"slug": slug, "limit": 1}, timeout=15)
        if resp.status_code == 200:
            events = resp.json()
            if events and len(events) > 0:
                markets = events[0].get("markets", [])
                if markets:
                    return markets[0]
    except:
        pass

    return None


def fetch_market_by_question(question: str) -> Optional[Dict]:
    """Fallback: search by question text (first 60 chars)."""
    if not question:
        return None

    url = f"{GAMMA_API_URL}/markets"
    # Use closed=true to find resolved markets
    params = {"closed": "true", "limit": 20}

    try:
        time.sleep(API_DELAY)
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            markets = resp.json()
            q_lower = question.lower().strip()
            for m in markets:
                if m.get("question", "").lower().strip() == q_lower:
                    return m
    except Exception as e:
        print(f"  ⚠️  API error searching by question: {e}")

    return None


def determine_resolution(market: Dict) -> Optional[str]:
    """
    Determine winning outcome from market data.
    Returns 'Yes', 'No', or team/player name. None if unresolved.
    """
    if not market:
        return None

    # Must have a resolution source
    if not market.get("resolutionSource"):
        return None

    # Parse outcomes and prices
    outcomes = market.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except:
            return None

    prices = market.get("outcomePrices", [])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except:
            return None

    # Method 1: price = 1.0 (fully resolved)
    for i, p in enumerate(prices):
        try:
            if float(p) >= 0.99 and i < len(outcomes):
                return outcomes[i]
        except:
            pass

    # Method 2: resolvedOutcome field
    resolved = market.get("resolvedOutcome") or market.get("winner")
    if resolved:
        return resolved

    # Method 3: highest price in closed market
    if market.get("closed") and prices:
        try:
            float_prices = [float(p) for p in prices]
            max_idx = float_prices.index(max(float_prices))
            if max(float_prices) > 0.90 and max_idx < len(outcomes):
                return outcomes[max_idx]
        except:
            pass

    return None


def check_insider_win(alert: Dict, resolution: str) -> Optional[bool]:
    """
    Did the insider's/trader's bet win?
    Handles both insider alerts (trade_data) and TOP_TRADER alerts (trade).
    """
    # Extract position from either alert format
    trade_data = alert.get("trade_data", {})
    trade = alert.get("trade", {})
    
    # Get outcome: insider alerts use trade_data, TOP_TRADER uses trade
    outcome = trade_data.get("outcome") or trade.get("outcome", "Yes")
    normalized = trade_data.get("normalized_position")  # YES or NO from detector

    position = str(outcome).strip()
    resolved = str(resolution).strip()

    # 1. Binary resolution (Yes/No)
    if resolved.lower() in ("yes", "no"):
        if normalized:
            return normalized.lower() == resolved.lower()
        if position.lower() in ("yes", "no"):
            return position.lower() == resolved.lower()
        if position.lower() == "over":
            return resolved.lower() == "yes"
        if position.lower() == "under":
            return resolved.lower() == "no"
        return None

    # 2. Named resolution (team/player name)
    if position.lower() == resolved.lower():
        return True
    if position.lower() in resolved.lower() or resolved.lower() in position.lower():
        return True
    if position.lower() in ("yes", "no", "over", "under"):
        return None

    return False


def calculate_pnl(alert: Dict, insider_win: Optional[bool]) -> Optional[float]:
    """Calculate P&L for a resolved alert."""
    if insider_win is None:
        return None
    
    # Get entry cost and effective odds
    trade_data = alert.get("trade_data", {})
    trade = alert.get("trade", {})
    
    amount = float(trade_data.get("amount", 0) or alert.get("amount", 0) or 0)
    effective_odds = float(trade_data.get("effective_price", 0) or trade.get("price", 0) or 0)
    
    if amount <= 0 or effective_odds <= 0:
        return None
    
    if insider_win:
        # Win: payout = amount / effective_odds, profit = payout - amount
        pnl = amount * (1.0 / effective_odds - 1.0)
    else:
        # Loss: lose entire amount
        pnl = -amount
    
    return round(pnl, 2)


def check_model_correct(alert: Dict, resolution: str) -> Optional[bool]:
    """
    Was the model's mispricing assessment correct?

    Model says 'YES overpriced' (edge > 0) → correct if resolved NO.
    Model says 'NO overpriced' (edge < 0) → correct if resolved YES.
    No edge → N/A.
    """
    mispricing = alert.get("mispricing", {})
    edge = mispricing.get("edge", 0)

    if not edge or abs(edge) < 0.01:
        return None  # No opinion

    resolved_lower = str(resolution).lower()

    if resolved_lower not in ["yes", "no"]:
        return None  # Can't evaluate for non-binary

    if edge > 0:
        # Model says YES overpriced → should resolve NO
        return resolved_lower == "no"
    else:
        # Model says NO overpriced → should resolve YES
        return resolved_lower == "yes"


def update_by_bucket(stats: Dict, bucket_key: str, bucket_name: str, insider_win: Optional[bool], model_correct: Optional[bool]):
    """Update stats for a specific bucket (signal_type, category, etc.)."""
    bucket = stats.setdefault(bucket_key, {})
    entry = bucket.setdefault(bucket_name, {
        "total": 0, "insider_wins": 0, "insider_losses": 0,
        "model_correct": 0, "model_wrong": 0, "model_na": 0,
    })
    entry["total"] += 1

    if insider_win is True:
        entry["insider_wins"] += 1
    elif insider_win is False:
        entry["insider_losses"] += 1

    if model_correct is True:
        entry["model_correct"] += 1
    elif model_correct is False:
        entry["model_wrong"] += 1
    else:
        entry["model_na"] += 1


def run_resolution_check():
    print(f"[{datetime.now()}] ═══════════════════════════════")
    print(f"[{datetime.now()}] RESOLUTION TRACKER")
    print(f"[{datetime.now()}] ═══════════════════════════════")

    alerts = load_alerts()
    stats = load_stats()

    if not alerts:
        print("No alerts to check.")
        return

    # Only check alerts that don't have resolution yet
    unchecked = [a for a in alerts if not a.get("resolution")]
    print(f"Total alerts: {len(alerts)}, unchecked: {len(unchecked)}")

    if not unchecked:
        print("All alerts already resolved or checked.")
        return

    newly_resolved = 0
    still_open = 0
    api_errors = 0

    # De-duplicate by market to avoid redundant API calls
    lookup_cache: Dict[str, Optional[Dict]] = {}

    for i, alert in enumerate(unchecked):
        # Extract all possible lookup keys
        slug = alert.get("market_slug", "")
        event_slug = alert.get("event_slug", "")
        market_question = alert.get("market", "")
        
        # conditionId: different location for insider vs TOP_TRADER
        condition_id = ""
        trade_data = alert.get("trade_data", {})
        trade = alert.get("trade", {})
        if trade_data.get("conditionId"):
            condition_id = trade_data["conditionId"]
        elif trade.get("conditionId"):
            condition_id = trade["conditionId"]
        
        # Also try slugs from trade_data
        if not slug:
            slug = trade_data.get("slug", "") or trade.get("slug", "")
        if not event_slug:
            event_slug = trade_data.get("eventSlug", "") or trade.get("eventSlug", "")

        # Build cache key from best available identifier
        cache_key = condition_id or slug or event_slug or market_question[:60]
        
        if not cache_key:
            api_errors += 1
            continue

        # Try cache first
        if cache_key in lookup_cache:
            market_data = lookup_cache[cache_key]
        else:
            market_data = None
            
            # Cascade: conditionId → slug → event_slug → question
            if condition_id:
                market_data = fetch_market_by_condition_id(condition_id)
            
            if not market_data and slug:
                market_data = fetch_market_by_slug(slug)
            
            if not market_data and event_slug and event_slug != slug:
                market_data = fetch_market_by_slug(event_slug)
            
            if not market_data and market_question:
                market_data = fetch_market_by_question(market_question)
            
            lookup_cache[cache_key] = market_data

        if not market_data:
            api_errors += 1
            continue

        resolution = determine_resolution(market_data)

        if resolution:
            # Market is resolved!
            insider_win = check_insider_win(alert, resolution)
            model_correct = check_model_correct(alert, resolution)
            pnl = calculate_pnl(alert, insider_win)

            # Store resolution in the alert itself
            alert["resolution"] = {
                "outcome": resolution,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "insider_win": insider_win,
                "model_correct": model_correct,
                "pnl": pnl,
            }

            # Update global stats
            stats["total_resolved"] += 1
            if insider_win is True:
                stats["insider_wins"] += 1
            elif insider_win is False:
                stats["insider_losses"] += 1

            if model_correct is True:
                stats["model_correct"] += 1
            elif model_correct is False:
                stats["model_wrong"] += 1
            else:
                stats["model_na"] += 1

            # P&L tracking
            if pnl is not None:
                stats["total_pnl"] = round(stats.get("total_pnl", 0) + pnl, 2)

            # Update per-signal-type and per-category
            signal_type = alert.get("combined_signal", {}).get("signal_type", "UNKNOWN")
            category = alert.get("irrationality", {}).get("category", "unknown")

            update_by_bucket(stats, "by_signal_type", signal_type, insider_win, model_correct)
            update_by_bucket(stats, "by_category", category, insider_win, model_correct)

            newly_resolved += 1
            position = alert.get("trade_data", {}).get("outcome") or alert.get("trade", {}).get("outcome", "?")
            amount = float(alert.get("trade_data", {}).get("amount", 0) or alert.get("amount", 0) or 0)
            signal_type = alert.get("combined_signal", {}).get("signal_type") or alert.get("type", "?")
            win_str = "✅" if insider_win else ("❌" if insider_win is False else "❓")
            pnl_str = f"${pnl:+,.0f}" if pnl is not None else "?"
            print(f"  [{newly_resolved}] {market_question[:55]}")
            print(f"       {signal_type} | {position} ${amount:,.0f} | Resolved: {resolution} | {win_str} {pnl_str}")
        else:
            still_open += 1
            # Mark as checked so we don't spam the API
            alert.setdefault("resolution_last_check", datetime.now(timezone.utc).isoformat())

        stats["total_checked"] += 1

    # Save
    stats["last_run"] = datetime.now(timezone.utc).isoformat()

    # Append to history for trend tracking
    stats.setdefault("history", []).append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "newly_resolved": newly_resolved,
        "still_open": still_open,
        "total_resolved": stats["total_resolved"],
        "insider_wins": stats["insider_wins"],
        "insider_losses": stats["insider_losses"],
        "model_correct": stats["model_correct"],
        "model_wrong": stats["model_wrong"],
    })

    # Keep last 90 days of history
    stats["history"] = stats["history"][-90:]

    save_alerts(alerts)
    save_stats(stats)

    # Print summary
    print()
    print(f"[{datetime.now()}] ═══════════════════════════════")
    print(f"[{datetime.now()}] RESOLUTION SUMMARY")
    print(f"[{datetime.now()}] ═══════════════════════════════")
    print(f"  Newly resolved: {newly_resolved}")
    print(f"  Still open: {still_open}")
    print(f"  API errors: {api_errors}")
    print()

    total_resolved = stats["insider_wins"] + stats["insider_losses"]
    if total_resolved > 0:
        insider_wr = stats["insider_wins"] / total_resolved * 100
        print(f"  INSIDER WIN RATE: {stats['insider_wins']}/{total_resolved} ({insider_wr:.1f}%)")
    else:
        print(f"  INSIDER WIN RATE: no data yet")

    total_pnl = stats.get("total_pnl", 0)
    print(f"  CUMULATIVE P&L: ${total_pnl:+,.0f}")

    total_model = stats["model_correct"] + stats["model_wrong"]
    if total_model > 0:
        model_acc = stats["model_correct"] / total_model * 100
        print(f"  MODEL ACCURACY: {stats['model_correct']}/{total_model} ({model_acc:.1f}%)")
    else:
        print(f"  MODEL ACCURACY: no data yet")

    # Per signal type
    if stats.get("by_signal_type"):
        print()
        print("  BY SIGNAL TYPE:")
        for st, data in sorted(stats["by_signal_type"].items()):
            total = data["insider_wins"] + data["insider_losses"]
            if total > 0:
                wr = data["insider_wins"] / total * 100
                print(f"    {st}: {data['insider_wins']}/{total} ({wr:.1f}% win rate)")
            else:
                print(f"    {st}: {data['total']} alerts, no resolved data")

    # Send Telegram summary if there were new resolutions
    if newly_resolved > 0 and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_resolution_summary(stats, newly_resolved)

    return stats


def send_resolution_summary(stats: Dict, newly_resolved: int):
    """Send daily resolution summary to Telegram."""
    wins = stats["insider_wins"]
    losses = stats["insider_losses"]
    determined = wins + losses
    total_resolved = stats["total_resolved"]
    undetermined = total_resolved - determined

    msg = f"📊 RESOLUTION TRACKER\n\n"
    msg += f"New: +{newly_resolved} | Total: {total_resolved} resolved\n\n"

    # Insider win rate
    if determined > 0:
        wr = wins / determined * 100
        msg += f"INSIDER WIN RATE: {wr:.0f}%\n"
        msg += f"{wins}W / {losses}L (of {determined} determined)"
        if undetermined > 0:
            msg += f"\n{undetermined} unmatched (sports/named markets)"
        total_pnl = stats.get("total_pnl", 0)
        if total_pnl != 0:
            msg += f"\nP&L: ${total_pnl:+,.0f}"
    else:
        msg += "INSIDER WIN RATE: no data yet"

    # Per-signal breakdown — only show signals with 5+ resolved
    by_st = stats.get("by_signal_type", {})
    breakdowns = []
    for st in ["ALPHA", "INSIDER_CONFIRMED", "CONFLICT", "INSIDER_ONLY", "UNKNOWN"]:
        data = by_st.get(st, {})
        w = data.get("insider_wins", 0)
        l = data.get("insider_losses", 0)
        t = w + l
        if t >= 3:
            breakdowns.append(f"  {st}: {w}W/{l}L ({w/t*100:.0f}%)")

    if breakdowns:
        msg += "\n\n" + "\n".join(breakdowns)

    # Model accuracy — only show if meaningful sample
    total_model = stats["model_correct"] + stats["model_wrong"]
    if total_model >= 10:
        model_acc = stats["model_correct"] / total_model * 100
        msg += f"\n\nMODEL: {stats['model_correct']}/{total_model} ({model_acc:.0f}%)"

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    msg += f"\n\nPolymarket Insiders | {timestamp} UTC"

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "disable_notification": True,
        }, timeout=10)
        print("✓ Resolution summary sent to Telegram")
    except Exception as e:
        print(f"⚠️  Failed to send Telegram summary: {e}")


if __name__ == "__main__":
    run_resolution_check()
