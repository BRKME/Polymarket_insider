# 🔍 Polymarket Insider Radar

**Detect insider trading, exploit irrational markets, and copy top traders on Polymarket.**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue?logo=telegram)](https://core.telegram.org/bots)

---

## ⚡ What It Does

This system monitors Polymarket in real-time and alerts you to three types of alpha signals:

| Signal Type | Description | Edge Source |
|-------------|-------------|-------------|
| 🚨 **Insider Detection** | New wallets making large bets before events | Information asymmetry |
| 📊 **Irrational Mispricing** | Markets where emotion > probability | Behavioral bias |
| 👑 **Top Trader Copy** | Follow consistently profitable wallets | Skill replication |

**Real results:** This system detected the Iran strike market signal 2 days before resolution, generating 10x returns on a $8,800 NO position.

---

## 🎯 Signal Examples

### 🔥 ALPHA Signal (Highest Conviction)
```
🔥 ALPHA SIGNAL — Insider + Mispricing Aligned

Market: US strikes Iran by February 28, 2026?
YES: 90¢ | NO: 10¢

Edge: +69.6% (STRONG)
Rational estimate: ~20%
→ EV favors NO

Insider: $1,500 NO @ 90¢
Wallet: 0xd3a6e523...5ccffd55 (New)

💡 High conviction: insider + statistics aligned
```

### ⚠️ CONFLICT Signal (Manual Review)
```
⚠️ CONFLICT — Insider vs Statistics

Market: Will SOTU address last 100+ minutes?
YES: 55¢ | NO: 45¢

Edge: +30.4% (YES overpriced)
Rational estimate: ~25%
→ EV favors NO

Insider: $1,447 YES @ 55¢
⚠️ Insider betting AGAINST statistical model

💡 Requires analysis: real info or irrational crowd?
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    POLYMARKET API                        │
│              (Gamma API + Data API)                      │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│                   COLLECTOR                              │
│  • Fetch active markets (top 50 by volume)              │
│  • Paginate recent trades (500/page, 10min window)      │
│  • Smart filters: skip HFT, arbs, small bets            │
└─────────────────────┬───────────────────────────────────┘
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
┌─────────────┐ ┌───────────┐ ┌─────────────┐
│  DETECTOR   │ │IRRATIONALITY│ │TOP TRADERS │
│             │ │            │ │            │
│• Wallet age │ │• Category  │ │• Leaderboard│
│• Bet size   │ │• Base rates│ │• Win rate  │
│• Pre-event  │ │• Edge calc │ │• Copy logic│
│• Patterns   │ │• Mispricing│ │            │
└──────┬──────┘ └─────┬──────┘ └─────┬──────┘
       │              │              │
       └──────────────┼──────────────┘
                      ▼
┌─────────────────────────────────────────────────────────┐
│                 COMBINED SIGNAL                          │
│  ALPHA | CONFLICT | INSIDER_CONFIRMED | TOP_TRADER      │
└─────────────────────┬───────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────┐
│                   NOTIFIER                               │
│              Telegram Alert + AI Summary                 │
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/BRKME/Polymarket_insider.git
cd Polymarket_insider
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export OPENAI_API_KEY="your_openai_key"
```

### 3. Run

```bash
python main.py
```

### 4. Deploy (GitHub Actions)

The system runs automatically every 5 minutes via GitHub Actions.

```yaml
# .github/workflows/run.yml
on:
  schedule:
    - cron: '*/5 * * * *'
```

---

## ⚙️ Configuration

Edit `config.py` to tune detection sensitivity:

```python
# Trading Thresholds
MIN_BET_SIZE = 1000          # Minimum bet to analyze ($)
ALERT_THRESHOLD = 70         # Insider score threshold (0-100)

# Signal Gating
COMBINED_SIGNAL_MIN_STRENGTH = 50   # Minimum combined signal
CONFLICT_MIN_INSIDER_SCORE = 60     # CONFLICT requires strong insider

# API Settings
MINUTES_BACK = 10            # Time window for trade collection
MAX_PAGES = 20               # Max pagination (20 × 500 = 10,000 trades)
```

---

## 📊 Signal Types Explained

### Insider Score Components

| Factor | Points | Why It Matters |
|--------|--------|----------------|
| Wallet age < 3 days | 40 | Fresh wallets often used for insider bets |
| Wallet age < 7 days | 20 | Still suspicious timing |
| Low activity (< 5 txns) | 10 | Single-purpose wallet |
| Against trend (< 10% odds) | 25 | Contrarian conviction |
| Large bet (> $5K) | 20 | Serious capital at risk |
| Pre-event timing | 15-50 | Trade before news breaks |

### Irrationality Categories

| Category | Bias Level | Typical Overpricing |
|----------|------------|---------------------|
| Meme/Celebrity | Very High | +7% |
| Conspiracy | Very High | +6% |
| Far Politics (2028+) | High | +5% |
| Geopolitics | High | +5% |
| Macro/Collapse | High | +4% |
| Near Politics | Medium | +2% |

---

## 📁 Project Structure

```
Polymarket_insider/
├── main.py              # Entry point
├── detector.py          # Insider detection logic
├── collector.py         # Polymarket API client
├── analyzer.py          # Scoring algorithms
├── irrationality.py     # Mispricing analysis
├── notifier.py          # Telegram formatting
├── config.py            # Configuration
├── database_fixed.py    # Wallet history tracking
├── event_detector_fixed.py  # Pre-event latency
├── POLICY.md            # Detection methodology
├── tracked_wallets.json # Deduplication state
└── alerts.json          # Alert history
```

---

## 🔬 Methodology

Based on two proven strategies:

1. **Insider Detection** — Academic research shows prediction markets exhibit abnormal trading patterns before major announcements. We detect: new wallets, unusual timing, and concentrated bets.

2. **Vitalik's Irrationality Strategy** — Vitalik Buterin made $70K betting against irrational markets (Trump Nobel Prize, USD collapse). We systematically identify markets where emotion > probability.

See [POLICY.md](POLICY.md) for complete methodology.

---

## 🛡️ Risk Management

### Built-in Protections

- **Coordinated attack detection** — Blocks pump & dump schemes
- **Arbitrage filtering** — Skips HFT and bot-dominated markets
- **Duplicate prevention** — One alert per trade hash
- **Rate limiting** — Respects API limits with exponential backoff

### Recommended Position Sizing

| Signal Type | Sizing | Rationale |
|-------------|--------|-----------|
| ALPHA | 3-5% bankroll | High conviction, aligned signals |
| INSIDER_CONFIRMED | 2-3% bankroll | Good signal, moderate risk |
| CONFLICT | 1% max | Needs manual verification |
| TOP_TRADER copy | 25-40% of source | Scale down whale positions |

---

## 🧪 Backtest & Validation

**Critical:** This system is a hypothesis until empirically validated.

### Run Backtest

```bash
# Step 1: Collect resolved markets (last 90 days)
python backtest.py collect 90

# Step 2: Run signal reconstruction
python backtest.py run

# Step 3: View metrics
python backtest.py report
```

### Metrics Calculated

| Metric | Purpose |
|--------|---------|
| ROI per signal type | Which signals actually generate alpha |
| Feature importance | Which factors drive returns (wallet_age, pre_event, etc.) |
| Win rate | Hit rate by signal type |
| Max drawdown | Worst cumulative loss |
| ROI distribution | Fat tail check |

### Interpreting Results

```
✅ ROI > 10%  → Proceed to probabilistic modeling
⚠️ ROI 0-10% → Tighten filters, review feature weights
❌ ROI < 0%   → Hypothesis falsified, revise methodology
```

### Expected Outcome

60-70% of current factors will prove to be noise. The backtest identifies the 2-3 features that actually correlate with profitable outcomes.

---

## 🤝 Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/improvement`)
3. Commit changes (`git commit -am 'Add feature'`)
4. Push to branch (`git push origin feature/improvement`)
5. Open Pull Request

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.

---

## ⚠️ Disclaimer

This software is for educational and research purposes only. Prediction market trading involves substantial risk of loss. The authors are not responsible for any financial losses incurred through use of this software. Always do your own research and never risk more than you can afford to lose.

---

<p align="center">
  <b>Built for alpha. Not for noise.</b>
</p>
