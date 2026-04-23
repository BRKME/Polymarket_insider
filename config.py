import os

# API Endpoints
GAMMA_API_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"

# Trading Thresholds
MIN_BET_SIZE = 1000  # $1,000 minimum (median insider bet = $1,447, $1500 cuts 57%)
ALERT_THRESHOLD = 70        # Score threshold for alerts (lowered from 80)
MAX_ODDS_THRESHOLD = 0.90   # Block >90% odds (favorite-longshot bias, SSRN #6443103)

# Combined signal gating (RELAXED for more signals)
COMBINED_SIGNAL_MIN_STRENGTH = 50      # Lowered from 80 (was too strict)
CONFLICT_MIN_INSIDER_SCORE = 60        # Lowered from 100 (was almost impossible)
INSIDER_ONLY_REQUIRES_PRE_EVENT = False # Allow INSIDER_ONLY without latency (was True)

# CONFLICT signal handling
# "ALERT"    = send Telegram alert (validated: 62% win rate on 8 resolved)
# "LOG_ONLY" = log but don't send Telegram alert
CONFLICT_SIGNAL_MODE = "ALERT"

# Market Filtering
BLOCK_15MIN_MARKETS = True  # Block HFT/bot markets
BLOCK_SHORT_PRICE_PREDICTIONS = True  # Block <24h price arbitrage

# Wallet Analysis Criteria
NEW_WALLET_DAYS_HIGH = 3    # Very new wallet (40 points)
NEW_WALLET_DAYS_LOW = 7     # New wallet (20 points)
NEW_WALLET_DAYS_MED = 14    # Recent wallet (10 points)
LOW_ACTIVITY_THRESHOLD = 20  # Low transaction count (raised from 5 — Gujarat wallet at 17txns missed by 10pts)
LOW_ODDS_THRESHOLD = 0.10   # Against trend: odds < 10%
TIME_TO_RESOLVE_HOURS = 24  # Close to deadline

# Scoring Weights
SCORES = {
    "wallet_age_high": 40,
    "wallet_age_low": 20,
    "wallet_age_med": 10,
    "against_trend": 25,
    "large_bet": 20,
    "timing": 15,
    "low_activity": 10
}

# API Request Settings - OPTIMIZED FOR GITHUB ACTIONS
# GitHub Actions minimum reliable cron interval = 5 minutes (*/5)
# */2 and */3 are unreliable and cause ~30 min actual intervals
TRADES_LIMIT = 500          # Real API limit (not 10000!)
MAX_PAGES = 20              # Up to 10,000 trades (20 × 500)
MINUTES_BACK = 20           # Look back 20 minutes for */5 frequency
                            # Increased from 10 for reliability (cron drift)
PAGE_DELAY = 1.0            # Delay between paginated requests
REQUEST_DELAY = 0.5         # Base delay for API requests

# Retry Configuration
MAX_RETRIES = 3             # Maximum retry attempts for failed requests
RETRY_DELAY = 5             # Base delay between retries (seconds)
RETRY_BACKOFF = 2           # Exponential backoff multiplier

# Rate Limit Handling
RATE_LIMIT_RETRY_DELAY = 60  # Wait time for 429 errors (seconds)
RATE_LIMIT_MAX_RETRIES = 2   # Max retries for rate limit errors

# Execution Limits
MAX_EXECUTION_TIME = 1800   # 30 minutes max execution (seconds)

# Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Validation (skip in CI environments)
if not os.getenv("CI") and not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY]):
    print("⚠️  WARNING: Missing required environment variables!")
    print(f"  TELEGRAM_BOT_TOKEN: {'✓' if TELEGRAM_BOT_TOKEN else '✗'}")
    print(f"  TELEGRAM_CHAT_ID: {'✓' if TELEGRAM_CHAT_ID else '✗'}")
    print(f"  OPENAI_API_KEY: {'✓' if OPENAI_API_KEY else '✗'}")
