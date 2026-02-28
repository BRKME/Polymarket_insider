# Polymarket Insider Detection Policy

## Overview

This system detects three types of alpha-generating signals on Polymarket:

1. **Insider Trading** — Wallets with abnormal pre-event timing or suspicious patterns
2. **Irrational Mispricing** — Markets where emotion drives price away from rational probability
3. **Top Trader Signals** — Copy trades from consistently profitable leaderboard wallets

Each signal type has distinct detection logic, confidence thresholds, and recommended actions.

---

## Signal Type 1: Insider Detection

### Definition
Insider signal = wallet behavior that suggests advance knowledge of event outcome.

### Detection Criteria

| Factor | Points | Condition |
|--------|--------|-----------|
| New wallet | 40 | Created < 3 days ago |
| New wallet | 20 | Created < 7 days ago |
| Low activity | 10 | < 5 total transactions |
| Against trend | 25 | Betting on < 10% odds |
| Large bet | 20 | Position > $5,000 |
| Pre-event timing | 15 | Trade within 24h of resolution |
| Pre-event latency | +50 | Trade < 60 min before event |

**Alert threshold:** Score ≥ 70

### Pre-Event Latency (Critical Signal)

Latency = time between trade and event occurrence.

| Latency | Severity | Interpretation |
|---------|----------|----------------|
| < 15 min | CRITICAL | Almost certain insider |
| < 60 min | HIGH | Very likely insider |
| < 4 hours | MEDIUM | Probable insider |
| < 24 hours | LOW | Possible insider |

### Wallet Classification

Based on historical behavior:

| Classification | Criteria |
|----------------|----------|
| Probable Insider | Pre-event rate > 50%, score > 80 |
| Syndicate/Whale | Large consistent bets, coordinated timing |
| Professional | High volume, consistent patterns |
| Retail | Random timing, small bets |
| New | No history |

### Filters (Noise Reduction)

Exclude trades that are likely arbitrage or bot activity:

- 15-minute markets (HFT territory)
- Short-term price predictions (< 24h)
- Odds 45-55% (coin flips)
- Odds > 95% (arbitrage)
- Amount < $1,000 (noise)
- Coordinated attack detection (> 3 similar alerts in 6h)

---

## Signal Type 2: Irrational Mispricing

### Definition
Markets where behavioral biases create systematic mispricing exploitable via statistical edge.

*Reference: Vitalik Buterin's strategy — betting against irrational outcomes like "Trump wins Nobel Prize" or "USD collapses".*

### Two-Step Analysis

#### Step 1: Irrationality Detection

Score 0-100 based on:

| Factor | Points | Condition |
|--------|--------|-----------|
| Longshot in high-bias category | 35 | < 15% odds in meme/conspiracy |
| Longshot in medium-bias category | 15-25 | < 15% odds in politics/geopolitics |
| Volume spike | 25 | 3x average volume (hype cycle) |
| Category bias | 10-20 | Structurally prone to overpricing |
| Extreme price move | 15 | > 10% change in 24h |
| Crisis keywords | 10 | war, strike, attack, collapse |
| Large mispricing edge | 15 | Edge > 20% |

**Irrational threshold:** Score ≥ 40

#### Step 2: Mispricing Confirmation

Convert market price to implied probability, then compare to rational estimate.

**Rational Estimate Sources:**
- Historical base rates (not intuition)
- Institutional procedures
- Legal/physical constraints
- Structural incentives

**Base Rate Classes:**

| Class | Probability | Example |
|-------|-------------|---------|
| Historically near zero | ~1% | Celebrity becomes president |
| Rare | ~5% | Unusual political outcome |
| Occasional | ~15% | Plausible but unlikely |
| Common | ~35% | Genuine uncertainty (don't trade) |

**Edge Calculation:**
```
Edge = Market Price - Rational Estimate
EV(NO) = (1 - Rational Estimate) - (1 - Market Price)
```

**Edge Quality:**

| Edge | Quality | Action |
|------|---------|--------|
| > 2× min_edge | STRONG | High conviction trade |
| > min_edge | MODERATE | Consider with sizing |
| > 0 | WEAK | Monitor only |
| ≤ 0 | NONE | No trade |

**Category Minimum Edge:**

| Category | Min Edge | Rationale |
|----------|----------|-----------|
| Meme | 3% | High noise, low bar |
| Conspiracy | 4% | Very high bias |
| Politics (far) | 5% | Time uncertainty |
| Politics (near) | 3% | More predictable |
| Geopolitics | 5% | Fat tails |
| Macro | 6% | Regime uncertainty |
| Sports | 5% | Efficient markets |
| Crypto | 5% | Volatile |

### Combined Signal Types

| Signal | Condition | Interpretation |
|--------|-----------|----------------|
| 🔥 ALPHA | Insider NO + Mispricing confirmed | Highest conviction — insider confirms statistical edge |
| ⚠️ CONFLICT | Insider YES + Market overpriced | Manual analysis needed — insider may know something OR is irrational |
| 🚨 INSIDER_CONFIRMED | Insider YES + Market underpriced | Follow insider — real information likely |
| ❓ CONTRARIAN | Insider NO + Market underpriced | Unusual — insider sees hidden risk |
| 👁️ INSIDER_ONLY | Insider activity, no clear mispricing | Monitor — signal without statistical edge |

---

## Signal Type 3: Top Trader Copy

### Definition
Replicate positions from consistently profitable Polymarket traders.

### Data Source
Polymarket Leaderboard: `https://polymarket.com/leaderboard`

### Selection Criteria

| Metric | Threshold | Rationale |
|--------|-----------|-----------|
| Profit (All Time) | > $50,000 | Proven track record |
| Win Rate | > 55% | Consistent edge |
| Volume | > $100,000 | Serious trader |
| Recent Activity | Last 7 days | Still active |
| Market Diversity | > 3 categories | Not one-trick |

### Copy Logic

1. **Monitor top 50 leaderboard wallets**
2. **Detect new positions** (not rebalancing)
3. **Filter by conviction:**
   - Position size > 5% of their typical bet
   - Not hedging existing position
   - Market has > 48h to resolution
4. **Alert with context:**
   - Trader's historical accuracy in this category
   - Position size relative to their bankroll
   - Current leaderboard rank

### Risk Management

- **Size cap:** Max 25-40% of source position
- **Diversification:** No more than 3 positions from same trader
- **Staleness:** Ignore positions > 24h old
- **Correlation:** Check for leaderboard herding (multiple top traders same position)

---

## Action Framework

### Signal → Decision Matrix

| Signal Type | Strength | Edge | Action |
|-------------|----------|------|--------|
| ALPHA | > 100 | > 10% | Execute with full sizing |
| ALPHA | > 100 | 5-10% | Execute with reduced sizing |
| INSIDER_CONFIRMED | > 80 | Any | Execute with moderate sizing |
| CONFLICT | Any | > 20% | Manual review required |
| TOP_TRADER | High conviction | N/A | Copy with 25-40% sizing |
| INSIDER_ONLY | > 100 | None | Monitor only |
| Any | < 50 | < 3% | No action |

### Pre-Trade Checklist

1. **Verify base rate** — Is rational estimate evidence-based?
2. **Check liquidity** — Thin order book = emotional pricing
3. **Calculate EV** — Must be positive after fees
4. **Assess tail risk** — Any mechanism for event to occur?
5. **Size appropriately** — 1-2% bankroll for exploratory, 3-5% for high conviction

### Post-Signal Workflow

```
1. Signal received
2. Verify market still open
3. Check current odds (may have moved)
4. Recalculate edge with current price
5. If edge still valid → execute
6. Set exit conditions (time-based or price-based)
7. Log trade for performance tracking
```

---

## Excluded Scenarios

### Never Trade

- Markets resolving < 1 hour (manipulation risk)
- Odds > 95% or < 5% (low EV, high variance)
- Coordinated pump (> 3 wallets, same market, < 6h)
- Markets with < $10,000 total volume (illiquid)
- Sports betting (efficient, no edge)
- Short-term crypto prices (arbitrage bots dominate)

### Requires Manual Override

- Geopolitical events with active news cycle
- Markets involving legal proceedings
- Celebrity/meme markets with viral potential
- Any CONFLICT signal

---

## Performance Tracking

### Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Signal accuracy | > 60% | Correct direction / total signals |
| ALPHA accuracy | > 75% | ALPHA signals that profit |
| Average edge captured | > 5% | Actual return vs predicted edge |
| False positive rate | < 20% | Signals that were noise |

### Attribution

Track which signal type generates returns:
- Insider timing
- Mispricing edge
- Top trader copy
- Combined signals

---

## Known Limitations & Caveats

### Insider Detection Limitations

1. **Rational insiders behave differently** — Real insiders likely:
   - Use aged wallets, not new ones
   - Enter gradually over 1-3 days, not minutes before
   - Split positions across multiple wallets
   - Avoid extreme odds that attract attention

2. **Pre-event timing ≠ insider access** — Could be:
   - Fast Twitter/news reactor
   - Lucky speculation
   - Coordinated pump group

3. **Scoring is heuristic, not probabilistic** — Fixed point values (40, 25, 15) create false precision without calibration data.

### Irrational Mispricing Limitations

1. **Base rates are subjective** — "Historically near zero" without empirical frequency data is opinion, not model.

2. **Category min_edge not calibrated** — 3-6% thresholds are arbitrary without backtest validation.

3. **Information lag ignored** — Market at 12% vs base rate 5% may reflect early information update, not irrationality.

4. **Polymarket may be semi-efficient** — Especially in politics and macro where sophisticated traders participate.

### Top Trader Copy Limitations

1. **Leaderboard may be manipulated** — Wash trading, sybil attacks.

2. **Hidden exposures** — Top trader may have hedges on Binance, OTC, or correlated markets. Copying visible position = copying risk without protection.

3. **Win rate ≠ EV** — 70% win rate with negative R-multiple = net loss.

4. **Survivorship bias** — Leaderboard shows current winners, not long-term consistency.

### Cognitive Risks

| Risk | Description |
|------|-------------|
| Illusion of insider detection | System detects anomalies, not confirmed insiders |
| Confirmation bias | Scoring validates preconceptions |
| Authority bias | Copying top traders assumes their edge transfers |
| Overconfidence | Threshold-based scoring feels precise but isn't |
| False precision | Point values imply accuracy we don't have |

### Validation Requirements v4

| # | Criterion | Threshold | Type |
|---|-----------|-----------|------|
| 1 | Trade count | ≥ 100 | Hard |
| 2 | t-stat (robust) | > 2.0 | Hard |
| 3 | ROI after costs | > 0 | Hard |
| 4 | vs Baselines | > best | Hard |
| 5 | Max drawdown | < 30% | Hard |
| 6 | Profit factor | > 1.2 | Hard |
| 7 | Survives top-10% removal | ROI > 0 | Stress |
| 8 | Cost sensitivity | 6/8 scenarios | Stress |
| 9 | Folds profitable | ≥ 60% | Stability |
| 10 | Tail dependence | Not dominated | Distribution |

### t-stat Robustness

Three SE estimators are computed, most conservative used:
- **Simple:** Standard √(var/n)
- **Newey-West:** Autocorrelation correction (lag 5)
- **Cluster-robust:** Grouped by market ID

If significance disappears under clustering → edge likely spurious.

### Walk-Forward Methodology

**Expanding:** Train [0→T], Test [T→T+k], expand T
**Rolling:** Fixed window (150 train, 50 test), slide forward

Both must show consistent performance across folds.

### Stress Tests

Remove top N% most profitable trades and recalculate:
- If ROI turns negative → system is tail-dependent
- This is a red flag, not automatic disqualification

Cost sensitivity across 8 scenarios:
- Must survive at least 6/8 with positive ROI
- Includes worst-case (+2% fee, 90% taker, 2x slippage)

### Parameter Freeze

All parameters are hashed (SHA256) before first backtest:
```
SCORE_WEIGHTS, SIGNAL_THRESHOLDS, T_STAT_THRESHOLD, etc.
```

Changing any parameter after seeing results → new version required.
Config hash is stored in `config_hash.json`.

### Validation Status

**Current status: UNVALIDATED HYPOTHESIS**

Run `python backtest.py run` to validate. 
System must pass 10/10 criteria to be considered validated.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 2.4 | 2026-02 | Hardened backtest: cluster-robust SE, stress tests, rolling WF, config hash |
| 2.3 | 2026-02 | Walk-forward CV, Newey-West t-stat, stability tests, hard filters |
| 2.2 | 2026-02 | Scientifically rigorous backtest: lookahead prevention, t-stat, baselines |
| 2.1 | 2026-02 | Added Limitations section, backtest engine, validation framework |
| 2.0 | 2026-02 | Added Top Trader copy, revised UI, action framework |
| 1.0 | 2026-01 | Initial insider + irrationality system |
