"""
verify_journal.py — honest scorecard for the v5 event strategy.

Reads event_journal.jsonl (one alerted NO-candidate per line), resolves each
market's real outcome, and reports:
  • WR / ROI at flat stake on the NO bets we actually flagged
  • AI calibration: did AI's P(YES) track reality? (the core untested assumption)
  • Diversification: max simultaneous open positions vs the ~20 the math needs
    (per habr.com/ru/articles/1042056 — too few concurrent bets => variance ruins
     even a real edge)

Resolution reuses the fixed cascade from resolution_tracker (Gamma condition_ids
-> CLOB winner flag). No bets are placed; this only scores what was logged.

Run:  python verify_journal.py
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import resolution_tracker as rt

JOURNAL = Path("event_journal.jsonl")
CALIB = Path("calibration_journal.jsonl")
FLAT_STAKE = 10.0          # $ we assume per NO bet, for scoring
TARGET_CONCURRENT = 20     # diversification target from the math in the article


def _load() -> list:
    if not JOURNAL.exists():
        print("No journal yet — nothing to verify.")
        return []
    rows = []
    for line in JOURNAL.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _resolve_no_outcome(condition_id: str) -> Optional[bool]:
    """
    Did the NO side win? Returns True (NO won), False (NO lost), None (unresolved).
    Uses the same multi-source lookup we fixed in resolution_tracker,
    через дисковый кэш journal_resolver: резолюция необратима, один
    сетевой запрос на condition_id навсегда (до 15.07 еженедельный отчёт
    перевыкачивал ВСЕ строки заново каждый раз).
    """
    import journal_resolver as jr
    global _RES_CACHE
    if _RES_CACHE is None:
        _RES_CACHE = jr.ResolutionCache()
    outcome = _RES_CACHE.get_outcome(condition_id, jr._default_fetch)
    if outcome == "No":
        return True       # NO side won → our bet won
    if outcome == "Yes":
        return False      # YES won → our NO bet lost
    return None


_RES_CACHE = None


def _flush_resolution_cache() -> None:
    """Сохранить кэш на диск после пачки резолвов (вызывается в конце main)."""
    if _RES_CACHE is not None:
        try:
            _RES_CACHE.save()
        except Exception:
            pass


def _legacy_resolve_no_outcome(condition_id: str) -> Optional[bool]:
    """Старый прямой путь без кэша — оставлен для сверки/отладки."""
    market = rt.fetch_market_by_condition_id(condition_id)
    if not market:
        market = rt.fetch_market_by_clob(condition_id)
    if not market:
        return None
    resolution = rt.determine_resolution(market)
    if not resolution or resolution in ("EXPIRED", "UNRESOLVED"):
        return None
    # determine_resolution returns the winning side as "Yes"/"No" (or similar).
    r = str(resolution).strip().lower()
    if r in ("no", "false", "0"):
        return True       # NO side won → our bet won
    if r in ("yes", "true", "1"):
        return False      # YES won → our NO bet lost
    return None


def _load_calibration() -> list:
    if not CALIB.exists():
        return []
    rows = []
    for line in CALIB.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def brier_report() -> None:
    """Head-to-head: does Grok's P(YES) predict outcomes better than the market?

    This is the load-bearing question for the whole v5 strategy. The bet journal
    can't answer it — it only contains the markets where Grok and the market
    disagreed most (a biased, selected tail). The calibration journal logs EVERY
    estimate, so we can score both predictors on the same neutral sample:

        Brier = mean( (P(YES) - actual_YES)^2 )   — lower is better.

    If Grok's Brier is NOT clearly below the market's, the edge is an illusion:
    we'd just be betting against a crowd that's better-calibrated than our model.
    """
    rows = _load_calibration()
    if not rows:
        print("\n=== GROK vs MARKET (Brier) ===")
        print("  No calibration journal yet — run scan_events to start logging.")
        return

    import calibration_map as cm
    rows = cm.first_per_market(
        [r for r in rows if r.get("market_yes_price") is not None
         and r.get("ai_yes_estimate") is not None])   # n = рынки, а не переоценки
    pairs = []   # (market_yes, ai_yes, actual_yes, horizon_days)
    for r in rows:
        cid = r.get("condition_id", "")
        mkt = r.get("market_yes_price")
        ai = r.get("ai_yes_estimate")
        if not cid or mkt is None or ai is None:
            continue
        no_won = _resolve_no_outcome(cid)
        if no_won is None:
            continue
        actual_yes = 0.0 if no_won else 1.0   # NO won => YES did not happen
        pairs.append((float(mkt), float(ai), actual_yes, r.get("horizon_days")))

    print("\n=== GROK vs MARKET (Brier — the core test) ===")
    if len(pairs) < 5:
        print(f"  Only {len(pairs)} resolved estimates — too few to judge.")
        print(f"  Need ~30+ for a meaningful read. Keep the scanner running.")
        return

    n = len(pairs)
    brier_mkt = sum((m - a) ** 2 for m, _, a, _ in pairs) / n
    brier_ai = sum((g - a) ** 2 for _, g, a, _ in pairs) / n
    print(f"  Resolved estimates : {n}")
    print(f"  Market Brier       : {brier_mkt:.4f}  (the crowd)")
    print(f"  Grok   Brier       : {brier_ai:.4f}  (our model)")
    delta = brier_mkt - brier_ai
    if delta > 0.01:
        print(f"  ✅ Grok beats the market by {delta:.4f} — edge is plausible.")
    elif delta < -0.01:
        print(f"  ❌ Market beats Grok by {-delta:.4f} — NO real edge; betting")
        print(f"     against a better-calibrated crowd. Reconsider the strategy.")
    else:
        print(f"  ⚖️ Essentially tied (Δ={delta:+.4f}) — no demonstrated edge yet.")

    # Calibration of Grok across probability buckets on the full sample.
    print("\n  Grok calibration (predicted P(YES) vs actual YES rate):")
    for lo, hi in [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]:
        b = [a for _, g, a, _ in pairs if lo <= g < hi]
        if b:
            actual = sum(b) / len(b) * 100
            print(f"    Grok said {lo:.1f}-{hi:.1f}: actual YES {actual:.0f}% (n={len(b)})")

    # Horizon split — short bets resolve fast and give an EARLY verdict; we
    # shouldn't have to wait on year-out markets to learn if Grok beats the
    # crowd. Recompute the Brier delta on just the short-horizon resolved set.
    short = [(m, g, a) for m, g, a, hd in pairs if hd is not None and hd <= 45]
    if len(short) >= 5:
        ns = len(short)
        bm = sum((m - a) ** 2 for m, _, a in short) / ns
        bg = sum((g - a) ** 2 for _, g, a in short) / ns
        ds = bm - bg
        print(f"\n  Short-horizon (≤45d) early read — n={ns}: "
              f"market {bm:.4f} vs Grok {bg:.4f} (Δ={ds:+.4f})")


def verify() -> None:
    rows = _load()
    if not rows:
        return
    print(f"Journal entries: {len(rows)}")

    wins = losses = unresolved = 0
    pnl = 0.0
    total_staked = 0.0
    # Onchain-confirmed subset: bets whose fill came from a real trade, not the
    # alert-price fallback. This is the trustworthy ROI — the rest leans on a
    # theoretical entry. Tracked separately so we can see both.
    oc_wins = oc_losses = 0
    oc_pnl = 0.0
    oc_staked = 0.0
    calib = []   # (ai_yes, no_won) for calibration check

    import mark_to_market as mtm

    for r in rows:
        cid = r.get("condition_id", "")
        ai_yes = float(r.get("ai_yes_estimate", 0) or 0)
        # Prefer the actually-filled entry price & stake (manual mode) so the
        # scorecard measures what was really traded, not the alert-time theory.
        entry = mtm.entry_no_price(r)
        if not cid or entry is None or entry <= 0:
            unresolved += 1
            continue
        stake = mtm.position_stake(r)
        no_won = _resolve_no_outcome(cid)
        if no_won is None:
            unresolved += 1
            continue
        is_onchain = r.get("fill_source") == "onchain"
        calib.append((ai_yes, no_won))
        total_staked += stake
        if is_onchain:
            oc_staked += stake
        if no_won:
            wins += 1
            pnl += stake * (1.0 / entry - 1.0)   # payout at actual NO entry odds
            if is_onchain:
                oc_wins += 1
                oc_pnl += stake * (1.0 / entry - 1.0)
        else:
            losses += 1
            pnl -= stake
            if is_onchain:
                oc_losses += 1
                oc_pnl -= stake

    decided = wins + losses
    print("\n=== STRATEGY SCORECARD (NO on events) ===")
    if decided:
        wr = wins / decided * 100
        roi = pnl / total_staked * 100 if total_staked else 0.0
        avg_stake = total_staked / decided if decided else 0.0
        print(f"  Resolved bets : {decided}  ({unresolved} still open/unresolved)")
        print(f"  Win rate      : {wins}W/{losses}L = {wr:.0f}%")
        print(f"  Total staked  : ${total_staked:,.0f}  (avg ${avg_stake:.0f}/bet)")
        print(f"  P&L           : ${pnl:+,.2f}  ·  ROI {roi:+.1f}%")
        oc_decided = oc_wins + oc_losses
        if oc_decided:
            oc_roi = oc_pnl / oc_staked * 100 if oc_staked else 0.0
            oc_wr = oc_wins / oc_decided * 100
            print(f"  — of which on-chain confirmed (trustworthy): "
                  f"{oc_decided} bets, {oc_wr:.0f}% WR, ROI {oc_roi:+.1f}%")
        else:
            print(f"  — on-chain confirmed: 0 yet (fills auto-fill after you bet)")
    else:
        print(f"  No resolved bets yet ({unresolved} pending). Come back later.")

    # AI calibration: when AI said YES was unlikely (low ai_yes), did NO win more?
    if calib:
        print("\n=== AI CALIBRATION (is the estimate trustworthy?) ===")
        for lo, hi in [(0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]:
            b = [no_won for ay, no_won in calib if lo <= ay < hi]
            if b:
                no_rate = sum(b) / len(b) * 100
                print(f"  AI P(YES) {lo:.2f}-{hi:.2f}: NO won {no_rate:.0f}% (n={len(b)})")
        print("  (lower AI P(YES) should mean NO wins more — if flat, AI adds no signal)")

    # Diversification: max simultaneous open positions.
    print("\n=== DIVERSIFICATION (variance control) ===")
    intervals = []
    for r in rows:
        a = r.get("alerted_at")
        e = r.get("end_date")
        if not a or not e:
            continue
        try:
            start = datetime.fromisoformat(a.replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(e).replace("Z", "+00:00"))
            intervals.append((start, end))
        except Exception:
            continue
    max_concurrent = 0
    if intervals:
        points = []
        for s, e in intervals:
            points.append((s, 1))
            points.append((e, -1))
        points.sort(key=lambda x: x[0])
        cur = 0
        for _, d in points:
            cur += d
            max_concurrent = max(max_concurrent, cur)
    print(f"  Max simultaneous open positions: {max_concurrent} (target ~{TARGET_CONCURRENT})")
    if max_concurrent < TARGET_CONCURRENT:
        print(f"  ⚠️ Below target — too few concurrent bets means variance can sink")
        print(f"     even a real edge. Widen the funnel or wait for more candidates.")

    # The load-bearing test: is Grok actually better-calibrated than the crowd?
    brier_report()


if __name__ == "__main__":
    verify()
    _flush_resolution_cache()
