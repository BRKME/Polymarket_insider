"""
Bet Model — dynamic win rates, contrarian signals, Kelly sizing.

Three functions:
1. get_signal_stats() — load live WR from resolution_stats.json
2. contrarian_check() — if signal type loses >60%, suggest opposite bet
3. kelly_size() — calculate optimal bet size given market odds and our edge
"""

import json
from pathlib import Path
from typing import Dict, Optional, Tuple


STATS_PATH = Path("resolution_stats.json")

# Minimum resolved trades before trusting the WR
MIN_SAMPLE = 5

# Below this WR, signal is contrarian (opposite bet wins)
CONTRARIAN_THRESHOLD = 0.40


def get_signal_stats() -> Dict[str, Dict]:
    """
    Load live win rates from resolution_stats.json.
    Returns dict: {signal_type: {wins, losses, total, wr, pnl}}
    """
    try:
        if not STATS_PATH.exists():
            return {}
        with open(STATS_PATH) as f:
            data = json.load(f)
    except:
        return {}

    result = {}
    by_type = data.get("by_signal_type", {})
    
    for signal_type, d in by_type.items():
        w = d.get("insider_wins", 0)
        l = d.get("insider_losses", 0)
        total = w + l
        wr = w / total if total > 0 else 0.5
        
        result[signal_type] = {
            "wins": w,
            "losses": l,
            "total": total,
            "wr": wr,
            "reliable": total >= MIN_SAMPLE,
        }
    
    return result


def format_wr_line(signal_type: str, stats: Dict[str, Dict]) -> str:
    """
    Format first line of alert with dynamic win rate.
    Example: "📊 ALPHA: 76% WR (13W/4L) — strong signal"
    """
    s = stats.get(signal_type)
    if not s or not s["reliable"]:
        return f"📊 {signal_type}: новый сигнал (мало данных)"
    
    wr = s["wr"]
    w, l = s["wins"], s["losses"]
    
    if wr >= 0.70:
        label = "🟢 сильный сигнал"
    elif wr >= 0.50:
        label = "🟡 умеренный"
    elif wr >= 0.35:
        label = "🟠 слабый — рассмотреть противоположную ставку"
    else:
        label = "🔴 контрариан — противоположная ставка выгоднее"
    
    return f"📊 {signal_type}: {wr*100:.0f}% WR ({w}W/{l}L) — {label}"


def contrarian_check(signal_type: str, outcome: str, stats: Dict[str, Dict]) -> Optional[Dict]:
    """
    If signal type historically loses >60%, suggest opposite bet.
    
    Returns:
        None if signal is profitable or insufficient data
        Dict with contrarian recommendation if signal is a loser
    """
    s = stats.get(signal_type)
    if not s or not s["reliable"]:
        return None
    
    wr = s["wr"]
    if wr >= CONTRARIAN_THRESHOLD:
        return None  # Signal is OK, no contrarian needed
    
    # Opposite WR
    opposite_wr = 1.0 - wr
    
    # Determine opposite outcome
    outcome_lower = outcome.strip().lower()
    if outcome_lower in ("yes", "over"):
        opposite = "NO"
    elif outcome_lower in ("no", "under"):
        opposite = "YES"
    else:
        # Team name — can't determine opposite easily
        opposite = f"против {outcome}"
    
    return {
        "opposite_outcome": opposite,
        "opposite_wr": opposite_wr,
        "original_wr": wr,
        "note": f"⚡ КОНТРАРИАН: {signal_type} ошибается {(1-wr)*100:.0f}% времени. "
                f"Противоположная ставка ({opposite}) выигрывает {opposite_wr*100:.0f}% случаев."
    }


def kelly_size(market_odds: float, edge_wr: float, bankroll: float = 100.0) -> Dict:
    """
    Kelly Criterion bet sizing.
    
    Accept market odds as true probability of outcome.
    Calculate optimal bet size based on our edge (WR from stats).
    
    Args:
        market_odds: current market probability (0.0-1.0), e.g., 0.66
        edge_wr: our historical win rate for this signal type (0.0-1.0)
        bankroll: total bankroll (default $100 for percentage calc)
    
    Returns:
        Dict with bet recommendation
    
    Kelly formula: f = (bp - q) / b
    where: b = decimal odds - 1, p = prob of winning, q = 1-p
    """
    if market_odds <= 0 or market_odds >= 1 or edge_wr <= 0:
        return {"action": "SKIP", "reason": "Invalid odds or no edge"}
    
    # Decimal odds: if you buy at 66¢ and win $1, your profit = 1/0.66 - 1 = 0.515
    decimal_odds = 1.0 / market_odds
    b = decimal_odds - 1  # net profit per $1 bet
    
    p = edge_wr  # our probability of winning this bet
    q = 1 - p    # probability of losing
    
    # Kelly fraction
    kelly_f = (b * p - q) / b
    
    # Half-Kelly (conservative — standard practice)
    half_kelly = kelly_f / 2
    
    if kelly_f <= 0:
        return {
            "action": "SKIP",
            "kelly_pct": 0,
            "reason": f"Нет edge: наш WR {p*100:.0f}% при odds {market_odds*100:.0f}% = отрицательное EV"
        }
    
    # Bet amount
    bet_amount = round(bankroll * max(0, half_kelly), 2)
    
    # Expected value per bet
    ev = p * (1/market_odds - 1) - q
    ev_dollar = ev * bet_amount
    
    # Risk tier
    if half_kelly >= 0.15:
        tier = "🟢 КРУПНАЯ"
    elif half_kelly >= 0.05:
        tier = "🟡 СРЕДНЯЯ"
    elif half_kelly > 0:
        tier = "🔵 МАЛАЯ"
    else:
        tier = "⚪ ПРОПУСК"
    
    return {
        "action": "BET",
        "kelly_pct": round(kelly_f * 100, 1),
        "half_kelly_pct": round(half_kelly * 100, 1),
        "bet_amount": bet_amount,
        "ev_per_bet": round(ev_dollar, 2),
        "tier": tier,
        "summary": f"{tier} · ${bet_amount:.0f} ({half_kelly*100:.1f}% банка) · EV ${ev_dollar:+.0f}/ставка"
    }


def format_bet_recommendation(market_odds: float, signal_type: str, 
                                stats: Dict[str, Dict], bankroll: float = 100.0,
                                outcome: str = "") -> str:
    """
    Full recommendation line for alert.
    Combines WR + contrarian + Kelly.
    """
    lines = []
    
    # 1. Win rate line
    lines.append(format_wr_line(signal_type, stats))
    
    # 2. Contrarian check
    contra = contrarian_check(signal_type, outcome, stats)
    if contra:
        lines.append(contra["note"])
    
    # 3. Kelly sizing
    s = stats.get(signal_type)
    if s and s["reliable"]:
        wr = s["wr"]
        
        # For contrarian, use opposite WR and opposite odds
        if contra:
            effective_wr = contra["opposite_wr"]
            effective_odds = 1 - market_odds  # opposite side
            kelly = kelly_size(effective_odds, effective_wr, bankroll)
            if kelly["action"] == "BET":
                lines.append(f"💰 Контрариан Kelly: {kelly['summary']}")
        else:
            kelly = kelly_size(market_odds, wr, bankroll)
            if kelly["action"] == "BET":
                lines.append(f"💰 Kelly: {kelly['summary']}")
            elif kelly["action"] == "SKIP":
                lines.append(f"💰 Kelly: {kelly['reason']}")
    
    return "\n".join(lines)
