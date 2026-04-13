from datetime import datetime, timezone
import re
import trade_economics
from collector import get_active_markets, get_all_priority_markets, get_recent_trades_paginated, get_wallet_activity, get_market_by_condition_id
from analyzer import calculate_score, should_skip_alert
from event_detector_fixed import detect_pre_event_trade, calculate_latency_score, get_latency_insight
from database_fixed import (
    init_database, get_wallet_stats, update_wallet_stats, 
    save_trade, is_alert_sent, mark_alert_sent,
    get_recent_alerts_for_market
)
from irrationality import analyze_market_irrationality
from top_traders import is_top_trader
from config import (
    ALERT_THRESHOLD,
    MIN_BET_SIZE,
    COMBINED_SIGNAL_MIN_STRENGTH,
    CONFLICT_MIN_INSIDER_SCORE,
    INSIDER_ONLY_REQUIRES_PRE_EVENT,
    CONFLICT_SIGNAL_MODE,
)

def detect_insider_trades():
    """
    Main detection function with event latency and wallet tracking.
    
    FIXES applied:
    - NO positions: correct amount, PnL, effective odds
    - Filters: crypto 3d window, market maker 0.97, political longshots accessible, 2028 regex
    
    Returns: (alerts, raw_trades, markets) tuple
    """
    # Initialize database on first run (with backup)
    init_database()
    
    alerts = []
    execution_start = datetime.now()
    
    try:
        # Fetch markets - expanded coverage for geopolitical events
        markets = get_all_priority_markets()
        if not markets:
            print(f"[{datetime.now()}] ⚠️  WARNING: No markets fetched, aborting")
            return [], [], []
        
        print(f"[{datetime.now()}] Found {len(markets)} active markets")
        
        # Fetch trades with pagination
        trades = get_recent_trades_paginated(markets)
        
        if not trades:
            print(f"[{datetime.now()}] ⚠️  WARNING: No trades fetched")
            return [], [], markets
        
        print(f"[{datetime.now()}] Analyzing {len(trades)} trades...")
        
        # Pre-fetch wallet stats for all unique wallets (batch operation)
        print(f"[{datetime.now()}] Pre-fetching wallet stats for batch processing...")
        unique_wallets = set()
        for trade in trades:
            wallet = trade.get("proxyWallet")
            if wallet:
                unique_wallets.add(wallet)
        
        wallet_stats_cache = {}
        for wallet in unique_wallets:
            stats = get_wallet_stats(wallet)
            if stats:
                wallet_stats_cache[wallet] = stats
        
        print(f"[{datetime.now()}] Cached stats for {len(wallet_stats_cache)} wallets")
        
        # Analysis counters
        processed_count = 0
        filtered_small = 0
        filtered_no_wallet = 0
        filtered_no_condition = 0
        filtered_no_market = 0
        filtered_by_rules = 0
        filtered_duplicate = 0
        filtered_invalid_data = 0
        filtered_coordinated = 0
        filtered_weak_signal = 0
        pre_event_detected = 0
        error_count = 0
        
        for idx, trade in enumerate(trades):
            try:
                # Extract basic trade info
                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))
                outcome = trade.get("outcome", "Yes")
                side = trade.get("side", "BUY")
                
                # SELL trades = closing positions, not opening insider bets
                if side == "SELL":
                    continue
                
                # GENERAL TRADES API: price = YES token price ALWAYS
                # (different from top trader API where price = specific token price)
                # For non-binary outcomes (team names), detect side from title
                outcome_lower = str(outcome).lower()
                if outcome_lower == "no":
                    econ_outcome = "No"
                elif outcome_lower == "yes":
                    econ_outcome = "Yes"
                elif outcome_lower == "under":
                    econ_outcome = "No"
                elif outcome_lower == "over":
                    econ_outcome = "Yes"
                else:
                    # Team/player name: detect from title
                    from notifier import _is_second_in_vs_title
                    condition_id = trade.get("conditionId", "")
                    market = next((m for m in markets if m.get("conditionId") == condition_id), {})
                    market_title = market.get("question", "")
                    econ_outcome = "No" if _is_second_in_vs_title(outcome, market_title) else "Yes"
                
                econ = trade_economics.calculate(size, price, econ_outcome)
                
                is_no = econ.is_no
                amount = econ.cost
                effective_odds = econ.effective_odds
                
                # Validate data before processing
                if amount <= 0:
                    filtered_invalid_data += 1
                    continue
                
                if not (0 <= price <= 1):
                    filtered_invalid_data += 1
                    continue
                
                # Log progress every 100 trades
                if (idx + 1) % 100 == 0:
                    elapsed = (datetime.now() - execution_start).total_seconds()
                    print(f"[{datetime.now()}] Progress: {idx + 1}/{len(trades)} trades ({elapsed:.1f}s elapsed)")
                
                # Filter by minimum bet size
                if amount < MIN_BET_SIZE:
                    filtered_small += 1
                    continue
                
                # Extract wallet address
                wallet_address = trade.get("proxyWallet")
                if not wallet_address:
                    filtered_no_wallet += 1
                    continue
                
                # Extract condition ID
                condition_id = trade.get("conditionId")
                if not condition_id:
                    filtered_no_condition += 1
                    continue
                
                # Find market
                market = get_market_by_condition_id(condition_id, markets)
                if not market:
                    raw_slug = trade.get("slug", "")
                    clean_slug = re.sub(r'-\d{1,3}-\d{1,3}$', '', raw_slug)
                    
                    market = {
                        "question": trade.get("title", "Unknown market"),
                        "slug": clean_slug,
                        "conditionId": condition_id,
                        "endDate": trade.get("endDate")
                    }
                    filtered_no_market += 1
                
                # Check for duplicate alert
                trade_hash = trade.get("transactionHash", "")
                if is_alert_sent(wallet_address, trade_hash):
                    filtered_duplicate += 1
                    continue
                
                # Log high-value trades (show position type)
                position_label = "NO" if is_no else "YES"
                print(f"\n[{datetime.now()}] 💰 Large trade: ${amount:,.0f} ({position_label})")
                print(f"  Wallet: {wallet_address[:8]}...{wallet_address[-4:]}")
                print(f"  Market: {market.get('question', 'Unknown')[:60]}...")
                print(f"  Position: {position_label} @ {effective_odds*100:.1f}% effective odds (raw price: {price:.4f})")
                
                # Event Latency Detection
                latency_data = detect_pre_event_trade(trade, market)
                if latency_data:
                    pre_event_detected += 1
                    print(f"  {get_latency_insight(latency_data)}")
                    print(f"     Trade time: {latency_data['trade_time']}")
                    print(f"     Event time: {latency_data['event_time']}")
                
                # Get Wallet Historical Stats (from cache)
                wallet_stats = wallet_stats_cache.get(wallet_address)
                if wallet_stats:
                    print(f"  📊 Wallet History:")
                    print(f"     Total trades: {wallet_stats['total_trades']}")
                    print(f"     Pre-event trades: {wallet_stats['pre_event_trades']}")
                    print(f"     Insider Score: {wallet_stats['insider_score']:.1f}")
                    print(f"     Classification: {wallet_stats['classification']}")
                
                # Fetch wallet activity
                print(f"  → Fetching wallet activity...")
                wallet_data = get_wallet_activity(wallet_address)
                
                if wallet_data.get('total_count', 0) == 0:
                    print(f"  ⚠️  No wallet activity found, skipping")
                    continue
                
                # Calculate base suspicion score (now NO-aware)
                analysis = calculate_score(trade, wallet_data, market)
                
                # Add Latency Score
                latency_score = 0
                if latency_data:
                    latency_score = calculate_latency_score(latency_data['latency_seconds'])
                    analysis['score'] += latency_score
                    analysis['flags'].append(f"Pre-event latency: {latency_data['latency_minutes']:.0f}m")
                
                # Add Wallet History Score
                history_score = 0
                if wallet_stats and wallet_stats['total_trades'] >= 3:
                    if wallet_stats['insider_score'] >= 70:
                        history_score = 20
                        analysis['flags'].append(f"Known insider (score: {wallet_stats['insider_score']:.0f})")
                    elif wallet_stats['insider_score'] >= 50:
                        history_score = 10
                        analysis['flags'].append(f"Suspicious history (score: {wallet_stats['insider_score']:.0f})")
                
                analysis['score'] += history_score
                
                # Check if wallet is a top trader (leaderboard)
                top_trader_score = 0
                top_trader_info = None
                try:
                    top_trader_info = is_top_trader(wallet_address)
                    if top_trader_info:
                        top_trader_score = 30  # Significant bonus for top trader
                        analysis['score'] += top_trader_score
                        analysis['flags'].append(f"Top trader #{top_trader_info['rank']} (${top_trader_info['profit']:,.0f} profit)")
                        print(f"  👑 TOP TRADER: Rank #{top_trader_info['rank']}, profit ${top_trader_info['profit']:,.0f}")
                except Exception as e:
                    print(f"  ⚠️  Top trader check skipped: {e}")
                
                print(f"  📊 Score: {analysis['score']}/180 (base: {analysis['score'] - latency_score - history_score - top_trader_score}, latency: +{latency_score}, history: +{history_score}, top_trader: +{top_trader_score})")
                print(f"     Flags: {', '.join(analysis['flags']) if analysis['flags'] else 'None'}")
                print(f"     Wallet age: {analysis['wallet_age_days']} days")
                print(f"     Activities: {analysis['total_activities']}")
                print(f"     Effective odds: {analysis['odds']*100:.1f}%")
                if is_no:
                    print(f"     ⚠️  NO position — real bet: ${econ.cost:,.0f}, potential profit: ${econ.potential_profit:,.0f} ({econ.pnl_multiplier:.1f}x)")
                
                # Check if alert threshold met
                if analysis["score"] >= ALERT_THRESHOLD:
                    # Apply filters before alerting
                    latency_min = latency_data['latency_minutes'] if latency_data else None
                    
                    # FIX: Pass outcome to should_skip_alert for correct NO filtering
                    should_skip, skip_reason = should_skip_alert(
                        market_question=market.get("question", ""),
                        wallet_age_days=analysis['wallet_age_days'],
                        odds=price,  # raw price — should_skip_alert computes effective internally
                        total_activities=analysis['total_activities'],
                        end_date_str=market.get("endDate"),
                        amount=amount,
                        latency_minutes=latency_min,
                        outcome=outcome
                    )
                    
                    if should_skip:
                        filtered_by_rules += 1
                        print(f"  🚫 FILTERED: {skip_reason}")
                        print(f"     (Score was {analysis['score']} >= {ALERT_THRESHOLD}, but filtered out)")
                    else:
                        # Check for coordinated attack
                        recent_alerts = get_recent_alerts_for_market(market.get("question", ""), hours=6)
                        
                        if len(recent_alerts) >= 3:
                            filtered_coordinated += 1
                            print(f"  🚫 FILTERED: COORDINATED_ATTACK")
                            print(f"     Market: {market.get('question', '')[:60]}")
                            print(f"     Similar alerts in last 6h: {len(recent_alerts)}")
                            print(f"     Possible pump & dump or sybil attack")
                            continue
                        
                        # ══════════════════════════════════════════
                        # IRRATIONALITY ANALYSIS (Methodology v2)
                        # ══════════════════════════════════════════
                        
                        # Normalize position to YES/NO for combined signal logic
                        # Over/first-outcome = YES equivalent
                        # Under/second-outcome = NO equivalent
                        outcome_lower = (outcome or "Yes").lower()
                        if outcome_lower in ("yes", "over"):
                            normalized_position = "YES"
                        elif outcome_lower in ("no", "under"):
                            normalized_position = "NO"
                        else:
                            # Team/player names: detect from title (outcomeIndex unreliable)
                            from notifier import _is_second_in_vs_title
                            market_title = market.get("question", "")
                            normalized_position = "NO" if _is_second_in_vs_title(outcome, market_title) else "YES"
                        
                        irrationality_analysis = analyze_market_irrationality(
                            market_question=market.get("question", ""),
                            yes_price=price,  # raw YES price
                            end_date=market.get("endDate"),
                            insider_score=analysis['score'],
                            insider_position=normalized_position
                        )
                        
                        combined_signal = irrationality_analysis['combined_signal']
                        print(f"  📊 Combined Signal: {combined_signal['signal_type']} (strength: {combined_signal['signal_strength']})")
                        print(f"     Irrationality: {irrationality_analysis['irrationality']['irrationality_score']}/100")
                        print(f"     Mispricing: edge {irrationality_analysis['mispricing']['edge_percent']:+.1f}% ({irrationality_analysis['mispricing']['edge_quality']})")

                        # Additional signal-quality gating (post-score, post-rules)
                        signal_type = combined_signal.get('signal_type', 'INSIDER_ONLY')
                        signal_strength = combined_signal.get('signal_strength', 0)

                        if signal_strength < COMBINED_SIGNAL_MIN_STRENGTH:
                            filtered_weak_signal += 1
                            print(f"  🚫 FILTERED: WEAK_COMBINED_SIGNAL (strength {signal_strength} < {COMBINED_SIGNAL_MIN_STRENGTH})")
                            continue

                        if signal_type == "CONFLICT" and analysis['score'] < CONFLICT_MIN_INSIDER_SCORE:
                            filtered_weak_signal += 1
                            print(f"  🚫 FILTERED: CONFLICT_LOW_CONFIDENCE (insider score {analysis['score']} < {CONFLICT_MIN_INSIDER_SCORE})")
                            continue

                        if signal_type == "INSIDER_ONLY" and INSIDER_ONLY_REQUIRES_PRE_EVENT and latency_data is None:
                            filtered_weak_signal += 1
                            print("  🚫 FILTERED: INSIDER_ONLY_WITHOUT_PRE_EVENT")
                            continue
                        
                        # Create enhanced alert with correct NO data
                        alert = {
                            "market": market.get("question"),
                            "market_slug": market.get("slug"),
                            "event_slug": market.get("eventSlug") or market.get("slug"),
                            "wallet": wallet_address,
                            "analysis": analysis,
                            "timestamp": datetime.now().isoformat(),
                            "trade_hash": trade_hash,
                            "trade_timestamp": trade.get("timestamp"),
                            # Latency data
                            "latency": latency_data,
                            # Wallet stats
                            "wallet_stats": wallet_stats,
                            # Top trader info (if applicable)
                            "top_trader": top_trader_info,
                            # ══════════════════════════════════════════
                            # FIX: Trade data with correct NO handling
                            # ══════════════════════════════════════════
                            "trade_data": {
                                "outcome": outcome,
                                "outcomeIndex": trade.get("outcomeIndex", 0),
                                "normalized_position": normalized_position,
                                "side": trade.get("side"),
                                "price": econ.raw_price,
                                "effective_price": econ.effective_odds,
                                "size": size,
                                "amount": econ.cost,
                                "potential_pnl": econ.potential_profit,
                                "pnl_multiplier": econ.pnl_multiplier,
                                "is_no": econ.is_no,
                                "conditionId": condition_id,
                                "slug": market.get("slug"),
                                "eventSlug": market.get("eventSlug") or trade.get("eventSlug")
                            },
                            # ══════════════════════════════════════════
                            # NEW: Irrationality analysis data
                            # ══════════════════════════════════════════
                            "irrationality": irrationality_analysis['irrationality'],
                            "mispricing": irrationality_analysis['mispricing'],
                            "factors": irrationality_analysis['factors'],
                            "combined_signal": combined_signal
                        }
                        alerts.append(alert)
                        print(f"  🚨 ALERT! Score {analysis['score']} >= {ALERT_THRESHOLD}")

                        # CONFLICT demote: save for tracking but don't send Telegram
                        if signal_type == "CONFLICT" and CONFLICT_SIGNAL_MODE == "LOG_ONLY":
                            alert["log_only"] = True
                            print(f"  📋 CONFLICT → LOG_ONLY (mispricing model bias, saved for resolution tracking)")
                        
                        # Mark alert as sent
                        mark_alert_sent(
                            wallet_address, 
                            market.get("question"), 
                            trade_hash,
                            wallet_stats['insider_score'] if wallet_stats else 0,
                            latency_data['latency_seconds'] if latency_data else None
                        )
                else:
                    print(f"  ✓ Below threshold ({analysis['score']} < {ALERT_THRESHOLD})")
                
                # Save Trade to History
                trade_record = {
                    'wallet': wallet_address,
                    'market': market.get('question'),
                    'trade_timestamp': datetime.fromtimestamp(trade.get('timestamp'), tz=timezone.utc),
                    'event_timestamp': datetime.fromisoformat(latency_data['event_time']) if latency_data else None,
                    'latency_seconds': latency_data['latency_seconds'] if latency_data else None,
                    'position': outcome,
                    'size': econ.cost,
                    'odds': econ.effective_odds,
                    'is_pre_event': latency_data is not None,
                    'trade_hash': trade_hash
                }
                save_trade(trade_record)
                
                # Update Wallet Stats
                update_wallet_stats(wallet_address, {
                    'size': econ.cost,
                    'is_pre_event': latency_data is not None,
                    'latency_seconds': latency_data['latency_seconds'] if latency_data else None
                })
                
                processed_count += 1
                
            except Exception as e:
                error_count += 1
                print(f"  ❌ Error processing trade #{idx + 1}: {e}")
                import traceback
                traceback.print_exc()
                if error_count > 10:
                    print(f"[{datetime.now()}] ⚠️  Too many errors ({error_count}), stopping analysis")
                    break
                continue
        
        # Final summary
        execution_time = (datetime.now() - execution_start).total_seconds()
        
        print(f"\n[{datetime.now()}] ════════════════════════════════")
        print(f"[{datetime.now()}] DETECTION SUMMARY:")
        print(f"[{datetime.now()}] ════════════════════════════════")
        print(f"[{datetime.now()}] Total trades analyzed: {len(trades)}")
        print(f"[{datetime.now()}] Processed (≥${MIN_BET_SIZE:,}): {processed_count}")
        print(f"[{datetime.now()}] ")
        print(f"[{datetime.now()}] Filtered out:")
        print(f"[{datetime.now()}]   - Small bets (<${MIN_BET_SIZE:,}): {filtered_small}")
        print(f"[{datetime.now()}]   - Invalid data: {filtered_invalid_data}")
        print(f"[{datetime.now()}]   - No wallet address: {filtered_no_wallet}")
        print(f"[{datetime.now()}]   - No condition ID: {filtered_no_condition}")
        print(f"[{datetime.now()}]   - Market not found: {filtered_no_market}")
        print(f"[{datetime.now()}]   - Duplicate alerts: {filtered_duplicate}")
        print(f"[{datetime.now()}]   - Arbitrage/Short-term/Absurd: {filtered_by_rules}")
        print(f"[{datetime.now()}]   - Coordinated attacks: {filtered_coordinated}")
        print(f"[{datetime.now()}]   - Weak combined signals: {filtered_weak_signal}")
        print(f"[{datetime.now()}] ")
        print(f"[{datetime.now()}] 🔍 Pre-event trades detected: {pre_event_detected}")
        print(f"[{datetime.now()}] Errors encountered: {error_count}")
        print(f"[{datetime.now()}] Alerts generated: {len(alerts)}")
        print(f"[{datetime.now()}] Execution time: {execution_time:.1f}s")
        print(f"[{datetime.now()}] ════════════════════════════════")
        
        return alerts, trades, markets
        
    except Exception as e:
        print(f"[{datetime.now()}] ❌ FATAL ERROR in detect_insider_trades: {e}")
        import traceback
        traceback.print_exc()
        return [], [], []
