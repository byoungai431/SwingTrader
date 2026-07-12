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
def _load_toml_secrets():
    """Load .streamlit/secrets.toml for headless (non-Streamlit) runs."""
    import os, tomllib, pathlib
    toml_path = pathlib.Path(__file__).parent / ".streamlit" / "secrets.toml"
    try:
        with open(toml_path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}

_toml_secrets = None

def _secret(key, fallback):
    global _toml_secrets
    try:
        import streamlit as st
        return st.secrets[key]
    except Exception:
        pass
    # Headless fallback: read from .streamlit/secrets.toml
    if _toml_secrets is None:
        _toml_secrets = _load_toml_secrets()
    return _toml_secrets.get(key, fallback)

ANTHROPIC_API_KEY  = _secret("ANTHROPIC_API_KEY",  "")

# ── Notifications ─────────────────────────────────────────────────────────────

# Email (Gmail SMTP)
EMAIL_ENABLED      = _secret("EMAIL_ENABLED",      True)
EMAIL_FROM         = _secret("EMAIL_FROM",          "byoungai431@gmail.com")
EMAIL_APP_PASSWORD = _secret("EMAIL_APP_PASSWORD",  "")
EMAIL_TO           = _secret("EMAIL_TO",            "byoungai431@gmail.com")

# Push notifications via ntfy.sh
NTFY_ENABLED = _secret("NTFY_ENABLED", False)
NTFY_TOPIC   = _secret("NTFY_TOPIC",   "swingtrader-yourname")

# Telegram Bot notifications
TELEGRAM_ENABLED       = _secret("TELEGRAM_ENABLED",       True)
TELEGRAM_BOT_TOKEN     = _secret("TELEGRAM_BOT_TOKEN",     "")
TELEGRAM_CHAT_ID       = _secret("TELEGRAM_CHAT_ID",       "7468464890")
TELEGRAM_GROUP_CHAT_ID = _secret("TELEGRAM_GROUP_CHAT_ID", "-4996149844")

# Archie webhook — best-effort forward of BUY / POSITION CLOSED messages to a
# friend's assistant. No-ops unless ARCHIE_TOKEN is set in secrets.
ARCHIE_ENABLED = _secret("ARCHIE_ENABLED", True)
ARCHIE_URL     = _secret("ARCHIE_URL", "https://archie-webhook.rootedfamilytree.org/webhook/swingtrader")
ARCHIE_TOKEN   = _secret("ARCHIE_TOKEN", "")
