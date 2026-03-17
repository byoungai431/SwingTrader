# Watchlist of stocks to analyze
WATCHLIST = [
    # Tech
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN",
    "AMD", "AVGO", "ORCL", "CRM",
    # Finance
    "JPM", "V", "MA", "BAC", "GS", "BRK-B",
    # Healthcare
    "UNH", "LLY", "ABBV", "JNJ", "MRK",
    # Consumer
    "COST", "WMT", "HD", "MCD", "NKE",
    # Energy / Utilities
    "XOM", "CVX", "NEE",
    # Auto
    "TSLA", "F", "GM",
    # Media / Entertainment
    "NFLX", "DIS", "SPOT",
]

# How many days of price history to fetch
HISTORY_DAYS = "1y"

# Claude model to use
CLAUDE_MODEL = "claude-sonnet-4-6"

# ── Market Regime Filter ───────────────────────────────────────────────────────
# BUY signals are suppressed when VIX closes above this threshold.
# Historical context: VIX > 25 = elevated fear / choppy market; > 30 = crisis.
# Set to None to disable the filter entirely.
VIX_MAX = None

# ── Secrets: read from Streamlit secrets when running in the cloud,
#    fall back to local values for scripts (run_daily.py, backtest, etc.)
# ──────────────────────────────────────────────────────────────────────────────
def _secret(key, fallback):
    try:
        import streamlit as st
        return st.secrets[key]
    except Exception:
        return fallback

ANTHROPIC_API_KEY  = _secret("ANTHROPIC_API_KEY",  "")

# ── Notifications ─────────────────────────────────────────────────────────────

# Email (Gmail SMTP)
EMAIL_ENABLED      = _secret("EMAIL_ENABLED",      True)
EMAIL_FROM         = _secret("EMAIL_FROM",          "byoungai431@gmail.com")
EMAIL_APP_PASSWORD = _secret("EMAIL_APP_PASSWORD",  "oxtc hhpf thca lqfp")
EMAIL_TO           = _secret("EMAIL_TO",            "byoungai431@gmail.com")

# Push notifications via ntfy.sh
NTFY_ENABLED = _secret("NTFY_ENABLED", False)
NTFY_TOPIC   = _secret("NTFY_TOPIC",   "swingtrader-yourname")

# Telegram Bot notifications
TELEGRAM_ENABLED    = _secret("TELEGRAM_ENABLED",    True)
TELEGRAM_BOT_TOKEN  = _secret("TELEGRAM_BOT_TOKEN",  "8652669850:AAFn6TFReNYLcW4n1u9DlFSfHipOaTIVkqQ")
TELEGRAM_CHAT_ID    = _secret("TELEGRAM_CHAT_ID",    "7468464890")
