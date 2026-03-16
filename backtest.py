"""
backtest.py
Walk-forward technical backtest over watchlist stocks.

Signal logic mirrors the 4 strategies used in signal_engine.py (no Claude API calls).
Exit priority: Stop-Loss → Take-Profit → Opposing Signal → 20-day Max Hold → End of Period

Run:
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 backtest.py
"""

import io
import os
import json
import random
import numpy as np
import pandas as pd
import yfinance as yf
import urllib.request
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(__file__)
WL_FILE     = os.path.join(BASE_DIR, "watchlist.json")
OUT_DIR     = os.path.join(BASE_DIR, "backtest results")
os.makedirs(OUT_DIR, exist_ok=True)

START_DATE  = "2022-01-01"
END_DATE    = "2026-01-01"
TRADE_SIZE       = 2_000   # $ per signal (flat)
STARTING_BALANCE = 10_000  # $ starting account balance for compound growth simulation
POSITION_PCT     = 0.20    # fraction of account per trade (20% = $2,000 on a $10,000 account)
COMMISSION    = 0.001      # 0.1% per side
ATR_MULT      = 2.0        # stop = entry ± ATR_MULT × ATR14
TP_PCT_3STAR  = 0.15       # take-profit for 3★ trades
TP_PCT_4STAR  = 0.20       # take-profit for 4★ trades
TP_PCT_5STAR  = 0.25       # take-profit for 5★ trades (deep oversold RSI strategy)
RSI_5STAR_ENTRY = 25       # RSI below this triggers a 5★ trade (overrides 3-of-4 gate)
RSI_5STAR_EXIT  = 72       # Exit 5★ when RSI rises above this
RSI_5STAR_DELAY = 0        # Bars to wait after RSI<25 signal before entering (0 = immediate)
FLOOR_5STAR     = 0.00     # Soft floor: exit 5★ if trade drops this % from entry (0 = disabled)
TRAIL_TRIGGER = 0.07       # activate trailing stop once trade gains this % from entry
TRAIL_PCT     = 0.08       # trailing stop trails this % below the running high
BREAKEVEN_TRIGGER = 0.00   # move stop to entry once trade gains this % (0 = disabled)
MAX_HOLD      = 30         # trading days before force-close (3★/4★)
MAX_HOLD_5STAR = 35        # trading days before force-close for 5★ trades

# ── Exit Tests (toggle each independently) ────────────────────────────────────
MACD_REVERSAL_EXIT   = False # Exit when MACD histogram crosses below 0 after being positive, only if profitable
STALE_CUT_DAYS       = 12    # Exit if trade gain < STALE_CUT_MIN_GAIN after this many days (0 = disabled)
STALE_CUT_MIN_GAIN   = 0.00  # Minimum gain % to avoid stale cut (0.00 = cut if not positive; 0.03 = cut if < 3%)
PROFIT_LOCK_TRIGGER  = 0.00  # Once gain reaches this %, floor stop at PROFIT_LOCK_FLOOR (0 = disabled)
PROFIT_LOCK_FLOOR    = 0.005 # Floor stop at entry + this % gain once PROFIT_LOCK_TRIGGER is hit
RSI_OVERBOUGHT_EXIT  = False # Exit when RSI crosses back below 70 after going overbought (>70), only if profitable
RSI_ONLY_MODE        = False # Entry fires on RSI < RSI_ENTRY_MAX alone (bypasses 3-of-4); exit when RSI > RSI_ONLY_EXIT
RSI_ONLY_EXIT        = 68    # Exit when RSI rises above this level (RSI_ONLY_MODE only)
USE_SP500        = True     # True = S&P 500 universe; False = watchlist or random pool
SP500_LIMIT      = 500     # cap number of S&P 500 tickers to use
USE_RANDOM_POOL  = False   # True = 600 random non-S&P500 US stocks; overrides USE_SP500/watchlist
RANDOM_POOL_SIZE = 600     # number of random stocks to sample
RANDOM_SEED      = 42      # fixed seed for reproducibility

# ── Entry Filters (new — toggle each independently to measure impact) ───────────
RSI_ENTRY_MAX       = 40    # RSI must be below this at entry (baseline was 40)
WEEKLY_TREND_FILTER = False # Only enter if price > 100-day MA (proxy for weekly trend up)
RS_SPY_FILTER       = False # Only enter if stock's 5-day return > SPY's 5-day return
EARNINGS_FILTER     = False # Skip signal if earnings date is within 5 calendar days of entry
SPY_REGIME_5STAR    = False # Only allow 5★ entries when SPY is above its own 200-day MA (bull market gate)

# ── VIX Regime Filter ─────────────────────────────────────────────────────────
# When VIX_FILTER = True, BUY entries are blocked on days VIX > VIX_FILTER_MAX.
# Set VIX_FILTER = False to run without blocking (but still tag trades by regime
# so the report shows high-VIX vs normal-VIX P&L comparison).
VIX_FILTER     = False  # Set True to apply the filter; False to just show comparison
VIX_FILTER_MAX = 25     # Threshold: VIX > this = elevated fear

# ── Load tickers ───────────────────────────────────────────────────────────────
if USE_RANDOM_POOL:
    # Step 1: get S&P 500 tickers to exclude
    _req = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 (compatible; backtest/1.0)"},
    )
    with urllib.request.urlopen(_req) as _resp:
        _html = _resp.read()
    _sp500_set = set(t.replace(".", "-") for t in pd.read_html(io.BytesIO(_html))[0]["Symbol"].tolist())

    # Step 2: fetch broad US stock universe from NASDAQ screener (NASDAQ + NYSE)
    _pool = []
    _headers = {
        "User-Agent": "Mozilla/5.0 (compatible; backtest/1.0)",
        "Accept": "application/json,text/plain,*/*",
    }
    for exchange in ["nasdaq", "nyse"]:
        try:
            _url = (f"https://api.nasdaq.com/api/screener/stocks"
                    f"?tableonly=true&limit=3000&exchange={exchange}")
            _req2 = urllib.request.Request(_url, headers=_headers)
            with urllib.request.urlopen(_req2) as _resp2:
                _data = json.loads(_resp2.read())
            for row in _data["data"]["table"]["rows"]:
                sym = row["symbol"].strip().replace(".", "-")
                # Keep only simple alphabetic tickers not in S&P 500
                if sym and sym not in _sp500_set and sym.isalpha() and len(sym) <= 5:
                    _pool.append(sym)
            print(f"  {exchange.upper()}: fetched {len(_pool)} non-S&P500 candidates so far")
        except Exception as e:
            print(f"  Warning: could not fetch {exchange} tickers — {e}")

    random.seed(RANDOM_SEED)
    TICKERS = random.sample(_pool, min(RANDOM_POOL_SIZE, len(_pool)))
    print(f"Random pool: {len(_pool)} eligible stocks → sampled {len(TICKERS)} (seed={RANDOM_SEED})")
elif USE_SP500:
    _req = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 (compatible; backtest/1.0)"},
    )
    with urllib.request.urlopen(_req) as _resp:
        _html = _resp.read()
    _table  = pd.read_html(io.BytesIO(_html))[0]
    TICKERS = [t.replace(".", "-") for t in _table["Symbol"].tolist()][:SP500_LIMIT]
    print(f"Loaded {len(TICKERS)} S&P 500 tickers from Wikipedia")
else:
    with open(WL_FILE) as f:
        TICKERS = json.load(f)
    print(f"Loaded {len(TICKERS)} tickers from watchlist")

# ── Indicator helpers (self-contained, no external deps) ──────────────────────
def _rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _macd_hist(close, fast=12, slow=26, sig=9):
    ema_f  = close.ewm(span=fast, adjust=False).mean()
    ema_s  = close.ewm(span=slow, adjust=False).mean()
    macd   = ema_f - ema_s
    signal = macd.ewm(span=sig, adjust=False).mean()
    return macd - signal

def _atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

def build_indicator_df(raw_df):
    df              = raw_df.copy()
    df["rsi"]       = _rsi(df["Close"])
    df["macd_hist"] = _macd_hist(df["Close"])
    df["ma50"]      = df["Close"].rolling(50).mean()
    df["ma100"]     = df["Close"].rolling(100).mean()   # weekly trend proxy (20-week MA)
    df["ma200"]     = df["Close"].rolling(200).mean()
    df["atr14"]     = _atr(df["High"], df["Low"], df["Close"])
    df["vol_avg20"] = df["Volume"].rolling(20).mean()
    df["rel_vol"]   = df["Volume"] / df["vol_avg20"]
    df["ret5"]      = df["Close"].pct_change(5)         # 5-day return for RS vs SPY filter
    return df

# ── Signal logic ───────────────────────────────────────────────────────────────
# Entry: 3-of-4 BUY conditions required + 2-candle confirmation + MA200 regime filter.
# BUY conditions (each measures a different dimension):
#   1. Oversold depth  : RSI < 40              (how far price pulled back)
#   2. MACD momentum   : histogram crosses > 0  (momentum direction reversal)
#   3. MA50 bounce     : price was at/below MA50 yesterday, reclaimed today (pullback entry timing)
#   4. Volume spike    : rel_vol >= 1.5x avg    (institutional participation — independent of price)
# Confidence stars = number of BUY conditions that fired + RSI oversold bonuses (max 4★):
#   3 conditions fired → 3★  (minimum viable trade)
#   4 conditions fired → 4★  (all signals aligned)
#   RSI bonus (applied after entry gate, does not affect entry logic):
#     RSI dipped below 40 in last 3 bars → +1★ (max tier: 4★)
# SELL conditions (only when price < MA200 — bear regime, used to EXIT longs only):
#   1. Overbought exit : RSI > 65
#   2. MACD reversal   : histogram crossing below 0 (cur < 0 < prev)
#   3. Death Cross     : MA50 < MA200

def get_signal(df, i):
    if i < 2:
        return None
    r  = df.iloc[i]
    p  = df.iloc[i - 1]
    pp = df.iloc[i - 2]

    for col in ("rsi", "macd_hist", "ma50", "ma200", "atr14"):
        if pd.isna(r[col]):
            return None

    death = r["ma50"] < r["ma200"]

    buy_conds = [
        r["rsi"] < RSI_ENTRY_MAX,                                             # oversold/pullback dip
        r["macd_hist"] > 0 and p["macd_hist"] <= 0,                          # momentum turned positive
        r["Close"] > r["ma50"] and p["Close"] <= p["ma50"] * 1.01,           # MA50 pullback bounce: just reclaimed from below
        not pd.isna(r.get("rel_vol", float("nan"))) and r["rel_vol"] >= 1.5, # volume spike (participation)
    ]
    sell_conds = [
        r["rsi"] > 65,
        r["macd_hist"] < 0 and p["macd_hist"] >= 0,             # histogram crossed below 0
        death,
    ]

    buys  = sum(bool(c) for c in buy_conds)
    sells = sum(bool(c) for c in sell_conds)

    # 2-candle confirmation: require 2 consecutive closes in signal direction
    two_green   = r["Close"] > p["Close"] and p["Close"] > pp["Close"]
    two_red     = r["Close"] < p["Close"] and p["Close"] < pp["Close"]

    # MA200 regime filter: only trade with the dominant trend
    bull_regime = r["Close"] > r["ma200"]   # BUY only above MA200 (uptrend)
    bear_regime = r["Close"] < r["ma200"]   # SELL only below MA200 (downtrend)

    if RSI_ONLY_MODE:
        if r["rsi"] < RSI_ENTRY_MAX and bull_regime:
            return "BUY"
        return None

    if buys >= 3 and sells == 0 and two_green and bull_regime:
        # Weekly trend filter: only enter when price is above 100-day MA
        if WEEKLY_TREND_FILTER and (pd.isna(r.get("ma100")) or r["Close"] <= r["ma100"]):
            return None
        return "BUY"
    if sells >= 2 and buys == 0 and two_red and bear_regime:
        return "SELL"
    return None

def strategy_labels(df, i, direction):
    r  = df.iloc[i]
    p  = df.iloc[i - 1]
    pp = df.iloc[i - 2]
    if direction == "BUY":
        labels = []
        if r["rsi"] < RSI_5STAR_ENTRY:
            labels.append(f"Deep Oversold 5★ (RSI {r['rsi']:.1f})")
        elif any(df["rsi"].iloc[max(0, i-2):i+1] < RSI_ENTRY_MAX):
            labels.append(f"Oversold Dip (RSI {r['rsi']:.1f})")
        if r["macd_hist"] > 0 and p["macd_hist"] <= 0:
            labels.append("MACD Momentum Cross")
        if r["Close"] > r["ma50"] and p["Close"] <= p["ma50"] * 1.01:
            labels.append("MA50 Bounce")
        if not pd.isna(r.get("rel_vol", float("nan"))) and r["rel_vol"] >= 1.5:
            labels.append(f"Volume Spike ({r['rel_vol']:.1f}x)")
        return ", ".join(labels)
    else:
        labels = []
        if r["rsi"] > 65:
            labels.append(f"Overbought (RSI {r['rsi']:.1f})")
        if r["macd_hist"] < 0 and p["macd_hist"] >= 0:
            labels.append("MACD Histogram crossed below 0")
        if r["ma50"] < r["ma200"]:
            labels.append("Death Cross")
        return ", ".join(labels)

def confidence_count(df, i):
    """Count how many BUY conditions fired, plus RSI oversold bonus (3–5 stars max)."""
    r  = df.iloc[i]
    p  = df.iloc[i - 1]
    # 5★ override: deep oversold RSI — return 5 immediately
    if r["rsi"] < RSI_5STAR_ENTRY:
        return 5
    count = 0
    if r["rsi"] < RSI_ENTRY_MAX:
        count += 1
    if r["macd_hist"] > 0 and p["macd_hist"] <= 0:
        count += 1
    if r["Close"] > r["ma50"] and p["Close"] <= p["ma50"] * 1.01:
        count += 1
    if not pd.isna(r.get("rel_vol", float("nan"))) and r["rel_vol"] >= 1.5:
        count += 1
    # RSI oversold bonus: +1★ if RSI dipped below 40 in last 3 bars (deep oversold recovery)
    if any(df["rsi"].iloc[max(0, i-2):i+1] < 40):
        count += 1
    return min(count, 4)

# ── Download data ──────────────────────────────────────────────────────────────
# Pull 1 extra year before START_DATE to warm up the 200-day MA
warmup = (pd.Timestamp(START_DATE) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
_need_spy = (RS_SPY_FILTER or SPY_REGIME_5STAR) and "SPY" not in TICKERS
tickers_to_dl = TICKERS + (["SPY"] if _need_spy else [])
print(f"Downloading data for {len(tickers_to_dl)} tickers ({warmup} → {END_DATE})...")
raw = yf.download(tickers_to_dl, start=warmup, end=END_DATE, progress=True, auto_adjust=True)

# ── SPY data (used for relative-strength filter and/or regime gate) ────────────
spy_ret5   = pd.Series(dtype=float)
spy_regime = {}   # date → True if SPY is in bull regime (2-bar confirmed, not a 1-day dip)
if RS_SPY_FILTER or SPY_REGIME_5STAR:
    try:
        spy_df  = raw.xs("SPY", axis=1, level=1).copy() if raw.columns.nlevels > 1 else raw.copy()
        spy_df["ret5"]  = spy_df["Close"].pct_change(5)
        spy_df["ma200"] = spy_df["Close"].rolling(200).mean()
        spy_ret5   = spy_df["ret5"]
        # Bull regime requires 2 consecutive closes above MA200 to allow entry
        above = spy_df["Close"] > spy_df["ma200"]
        bull_confirmed = above & above.shift(1).fillna(False)
        spy_regime = bull_confirmed.to_dict()   # True = 2-bar bull confirmed, allow 5★; False = block
        bull_days = sum(spy_regime.values())
        print(f"  SPY data ready — regime gate: {bull_days} / {len(spy_regime)} days with 2-bar bull confirmation")
    except Exception as e:
        print(f"  Warning: could not extract SPY data — {e}")

# ── VIX data (used for regime filter and comparison tagging) ──────────────────
vix_elevated = {}   # date → True if VIX closed above VIX_FILTER_MAX
try:
    print(f"Downloading VIX data ({warmup} → {END_DATE})...")
    vix_raw = yf.download("^VIX", start=warmup, end=END_DATE, progress=False, auto_adjust=True)
    vix_close = vix_raw["Close"].squeeze()
    for date, val in vix_close.items():
        vix_elevated[date] = float(val) > VIX_FILTER_MAX
    high_vix_days = sum(vix_elevated.values())
    print(f"  VIX data ready — {high_vix_days} / {len(vix_elevated)} days with VIX > {VIX_FILTER_MAX}")
except Exception as e:
    print(f"  WARNING: VIX data unavailable — {e}. Regime filter/tagging disabled.")

# ── Pre-fetch earnings dates ────────────────────────────────────────────────────
earnings_map = {}   # {ticker: set of pd.Timestamp dates}
if EARNINGS_FILTER:
    print(f"Fetching earnings dates for {len(TICKERS)} tickers (this may take a minute)...")
    for _tk in TICKERS:
        try:
            ed = yf.Ticker(_tk).earnings_dates
            if ed is not None and not ed.empty:
                earnings_map[_tk] = set(ed.index.normalize().tz_localize(None))
        except Exception:
            pass
    print(f"  Earnings data loaded for {len(earnings_map)} / {len(TICKERS)} tickers")

# ── Run backtest per ticker ────────────────────────────────────────────────────
all_trades = []
START_TS   = pd.Timestamp(START_DATE)

for ticker in TICKERS:
    try:
        # Extract per-ticker DataFrame from multi-ticker download
        lvl = raw.columns.get_level_values(1) if raw.columns.nlevels > 1 else None
        if lvl is not None:
            if ticker not in lvl:
                print(f"  {ticker}: not found in download, skipping")
                continue
            df = raw.xs(ticker, axis=1, level=1).copy()
        else:
            df = raw.copy()

        df = df.dropna(subset=["Close"])
        if len(df) < 210:
            print(f"  {ticker}: insufficient history, skipping")
            continue

        df = build_indicator_df(df)

        # Backtest window (indicators computed over full df for warm-up accuracy)
        bt_idx = df.index[df.index >= START_TS]
        if bt_idx.empty:
            continue

        position       = None
        hold_days      = 0
        cooldown       = 0   # bars before new entries allowed after a stop-loss
        pending_5star  = 0   # bars since RSI<RSI_5STAR_ENTRY first fired (0 = no pending)

        for date in bt_idx:
            i     = df.index.get_loc(date)
            row   = df.iloc[i]
            close = row["Close"]
            high  = row["High"]
            low   = row["Low"]
            atr   = row["atr14"]

            if cooldown > 0:
                cooldown -= 1

            # Cancel pending 5★ if RSI recovered above threshold or position is open
            if pending_5star > 0 and (position is not None or
                    pd.isna(row["rsi"]) or row["rsi"] >= RSI_5STAR_ENTRY):
                pending_5star = 0

            # ── Manage open position ───────────────────────────────────────
            if position is not None:
                hold_days += 1
                exit_price  = None
                exit_reason = None

                # Track RSI above 50 for momentum exit
                if not pd.isna(row["rsi"]) and row["rsi"] > 50:
                    position["rsi_above_50"] = True

                # Track RSI above 70 for overbought exit
                if not pd.isna(row["rsi"]) and row["rsi"] > 70:
                    position["rsi_above_70"] = True

                # Track running high (used for peak_gain on all trades)
                if high > position["trail_high"]:
                    position["trail_high"] = high
                # Update trailing stop — not for 5★ (no stop-loss on 5★ trades)
                if not position.get("is_5star", False):
                    gain_pct = (position["trail_high"] - position["entry_price"]) / position["entry_price"]
                    if gain_pct >= TRAIL_TRIGGER:
                        trail_stop = position["trail_high"] * (1 - TRAIL_PCT)
                        if trail_stop > position["stop"]:
                            position["stop"] = trail_stop

                # Breakeven stop: once trade gains BREAKEVEN_TRIGGER, floor stop at entry
                if BREAKEVEN_TRIGGER > 0:
                    gain_from_entry = (close - position["entry_price"]) / position["entry_price"]
                    if gain_from_entry >= BREAKEVEN_TRIGGER and position["entry_price"] > position["stop"]:
                        position["stop"] = position["entry_price"]

                # Profit-lock stop: once trade gains PROFIT_LOCK_TRIGGER, floor stop at entry + PROFIT_LOCK_FLOOR
                if PROFIT_LOCK_TRIGGER > 0:
                    gain_from_entry = (close - position["entry_price"]) / position["entry_price"]
                    profit_floor = position["entry_price"] * (1 + PROFIT_LOCK_FLOOR)
                    if gain_from_entry >= PROFIT_LOCK_TRIGGER and profit_floor > position["stop"]:
                        position["stop"] = profit_floor

                if not position.get("is_5star", False) and low <= position["stop"]:
                    exit_price  = position["stop"]
                    exit_reason = "Stop-Loss"
                elif high >= position["target"]:
                    exit_price  = position["target"]
                    exit_reason = "Take-Profit"

                # Momentum exit: RSI crosses below 50 after rising above it — only if trade is profitable
                # Not active for 5★ trades (5★ only exits on RSI>68, TP, or max hold)
                current_gain = (close - position["entry_price"]) / position["entry_price"]
                if (exit_price is None and not position.get("is_5star", False) and
                        position["rsi_above_50"] and current_gain > 0 and
                        not pd.isna(row["rsi"]) and row["rsi"] < 50 and
                        i > 0 and not pd.isna(df.iloc[i - 1]["rsi"]) and df.iloc[i - 1]["rsi"] >= 50):
                    exit_price  = close
                    exit_reason = "RSI Momentum Exit"

                # RSI overbought exit: RSI crosses back below 70 after being overbought — only if profitable
                if (exit_price is None and RSI_OVERBOUGHT_EXIT and position["rsi_above_70"] and current_gain > 0 and
                        not pd.isna(row["rsi"]) and row["rsi"] < 70 and
                        i > 0 and not pd.isna(df.iloc[i - 1]["rsi"]) and df.iloc[i - 1]["rsi"] >= 70):
                    exit_price  = close
                    exit_reason = "RSI Overbought Exit"

                # RSI-only mode exit: exit when RSI rises above RSI_ONLY_EXIT threshold
                if (exit_price is None and RSI_ONLY_MODE and
                        not pd.isna(row["rsi"]) and row["rsi"] > RSI_ONLY_EXIT):
                    exit_price  = close
                    exit_reason = f"RSI Exit (>{RSI_ONLY_EXIT})"

                # 5★ RSI exit: exit when RSI rises above RSI_5STAR_EXIT (no profit requirement)
                if (exit_price is None and position.get("is_5star", False) and
                        not pd.isna(row["rsi"]) and row["rsi"] > RSI_5STAR_EXIT):
                    exit_price  = close
                    exit_reason = f"RSI Exit 5★ (>{RSI_5STAR_EXIT})"

                # 5★ soft floor: exit if trade drops FLOOR_5STAR% from entry
                if (exit_price is None and position.get("is_5star", False) and
                        FLOOR_5STAR > 0 and current_gain <= -FLOOR_5STAR):
                    exit_price  = close
                    exit_reason = f"Floor Stop 5★ (-{int(FLOOR_5STAR*100)}%)"

                # MACD reversal exit: histogram crosses back below 0 after being positive — only if profitable
                if (exit_price is None and MACD_REVERSAL_EXIT and current_gain > 0 and
                        not pd.isna(row["macd_hist"]) and row["macd_hist"] < 0 and
                        i > 0 and not pd.isna(df.iloc[i - 1]["macd_hist"]) and df.iloc[i - 1]["macd_hist"] >= 0):
                    exit_price  = close
                    exit_reason = "MACD Reversal Exit"

                # Stale cut: exit if trade gain < STALE_CUT_MIN_GAIN by STALE_CUT_DAYS
                # Not active for 5★ trades
                if (exit_price is None and not position.get("is_5star", False) and
                        STALE_CUT_DAYS > 0 and hold_days >= STALE_CUT_DAYS and current_gain < STALE_CUT_MIN_GAIN):
                    exit_price  = close
                    exit_reason = f"Stale Cut ({STALE_CUT_DAYS}d)"

                max_hold_limit = MAX_HOLD_5STAR if position.get("is_5star", False) else MAX_HOLD
                if exit_price is None and hold_days >= max_hold_limit:
                    exit_price  = close
                    exit_reason = f"Max Hold ({max_hold_limit}d)"

                if exit_price is None and not position.get("is_5star", False):
                    sig = get_signal(df, i)
                    if sig == "SELL":
                        exit_price  = close
                        exit_reason = "Sell Signal"

                if exit_price is not None:
                    entry    = position["entry_price"]
                    pos_size = position["trade_size"]
                    pnl_pct    = (exit_price - entry) / entry
                    pnl_dollar = pnl_pct * pos_size - pos_size * COMMISSION * 2

                    all_trades.append({
                        "ticker":           ticker,
                        "signal":           position["direction"],
                        "entry_date":       position["entry_date"].strftime("%Y-%m-%d"),
                        "entry_price":      round(entry, 2),
                        "stop":             round(position["stop"], 2),
                        "target":           round(position["target"], 2),
                        "exit_date":        date.strftime("%Y-%m-%d"),
                        "exit_price":       round(exit_price, 2),
                        "exit_reason":      exit_reason,
                        "hold_days":        hold_days,
                        "pnl_pct":          pnl_pct,
                        "pnl_dollar":       pnl_dollar,
                        "strategies":       position["strategies"],
                        "confidence_stars": position["confidence_stars"],
                        "trade_size":       pos_size,
                        "peak_gain":        (position["trail_high"] - entry) / entry,
                        "vix_high":         vix_elevated.get(position["entry_date"], False),
                    })
                    if exit_reason == "Stop-Loss":
                        cooldown = 5   # block re-entry for 5 bars after a stop-out
                    position  = None
                    hold_days = 0
                    continue

            # ── Check entry (only when flat and cooldown expired) ──────────
            if position is None and not pd.isna(atr) and atr > 0 and cooldown == 0:
                bull_regime_now = not pd.isna(row["ma200"]) and row["Close"] > row["ma200"]

                # 5★ path: RSI deep oversold with configurable delay
                spy_bull  = not SPY_REGIME_5STAR or spy_regime.get(date, True)
                vix_block = VIX_FILTER and vix_elevated.get(date, False)
                if (not pd.isna(row["rsi"]) and row["rsi"] < RSI_5STAR_ENTRY and bull_regime_now and spy_bull
                        and not vix_block):
                    if pending_5star == 0:
                        pending_5star = 1   # start delay clock — don't enter yet
                    else:
                        pending_5star += 1

                    if pending_5star > RSI_5STAR_DELAY:
                        # Delay elapsed — enter as 5★ at today's close
                        pending_5star = 0
                        target = close * (1 + TP_PCT_5STAR)
                        position = {
                            "direction":        "BUY",
                            "entry_date":       date,
                            "entry_price":      close,
                            "stop":             0,
                            "target":           target,
                            "trail_high":       close,
                            "strategies":       strategy_labels(df, i, "BUY"),
                            "confidence_stars": 5,
                            "trade_size":       TRADE_SIZE,
                            "rsi_above_50":     False,
                            "rsi_above_70":     False,
                            "is_5star":         True,
                        }
                        hold_days = 0

                else:
                    # 3★/4★ path: standard 3-of-4 gate (RSI >= RSI_5STAR_ENTRY)
                    sig = get_signal(df, i)

                    # VIX regime filter: block BUY entries on high-fear days
                    if sig == "BUY" and VIX_FILTER and vix_elevated.get(date, False):
                        sig = None

                    # Relative strength filter: stock 5-day return must beat SPY
                    if sig == "BUY" and RS_SPY_FILTER:
                        stock_ret = row.get("ret5", float("nan"))
                        spy_r     = spy_ret5.get(date, float("nan"))
                        if not pd.isna(stock_ret) and not pd.isna(spy_r) and stock_ret <= spy_r:
                            sig = None

                    # Earnings filter: skip if earnings within 5 calendar days of entry
                    if sig == "BUY" and EARNINGS_FILTER and ticker in earnings_map:
                        if any(0 <= (ed - date.tz_localize(None) if date.tzinfo else ed - date).days <= 5
                               for ed in earnings_map[ticker]):
                            sig = None

                    if sig == "BUY":
                        stars  = confidence_count(df, i)
                        tp_pct = TP_PCT_4STAR if stars == 4 else TP_PCT_3STAR
                        stop   = close - ATR_MULT * atr
                        target = close * (1 + tp_pct)
                        position = {
                            "direction":        "BUY",
                            "entry_date":       date,
                            "entry_price":      close,
                            "stop":             stop,
                            "target":           target,
                            "trail_high":       close,
                            "strategies":       strategy_labels(df, i, "BUY"),
                            "confidence_stars": stars,
                            "trade_size":       TRADE_SIZE,
                            "rsi_above_50":     False,
                            "rsi_above_70":     False,
                            "is_5star":         False,
                        }
                        hold_days = 0


        # Close any position still open at end of period
        if position is not None:
            last     = df.loc[bt_idx[-1]]
            exit_p   = last["Close"]
            entry    = position["entry_price"]
            pos_size   = position["trade_size"]
            pnl_pct    = (exit_p - entry) / entry
            pnl_dollar = pnl_pct * pos_size - pos_size * COMMISSION * 2
            all_trades.append({
                "ticker":           ticker,
                "signal":           position["direction"],
                "entry_date":       position["entry_date"].strftime("%Y-%m-%d"),
                "entry_price":      round(entry, 2),
                "stop":             round(position["stop"], 2),
                "target":           round(position["target"], 2),
                "exit_date":        bt_idx[-1].strftime("%Y-%m-%d"),
                "exit_price":       round(exit_p, 2),
                "exit_reason":      "End of Period",
                "hold_days":        hold_days,
                "pnl_pct":          pnl_pct,
                "pnl_dollar":       pnl_dollar,
                "strategies":       position["strategies"],
                "confidence_stars": position["confidence_stars"],
                "trade_size":       pos_size,
                "peak_gain":        (position["trail_high"] - entry) / entry,
                "vix_high":         vix_elevated.get(position["entry_date"], False),
            })

    except Exception as e:
        print(f"  {ticker}: ERROR — {e}")

# ── Aggregate stats ────────────────────────────────────────────────────────────
total   = len(all_trades)
wins    = [t for t in all_trades if t["pnl_dollar"] > 0]
losses  = [t for t in all_trades if t["pnl_dollar"] <= 0]
total_pnl  = sum(t["pnl_dollar"] for t in all_trades)
win_rate   = len(wins) / total * 100 if total else 0
avg_win    = sum(t["pnl_dollar"] for t in wins)   / len(wins)   if wins   else 0
avg_loss   = sum(t["pnl_dollar"] for t in losses) / len(losses) if losses else 0
gross_win  = sum(t["pnl_dollar"] for t in wins)
gross_loss = abs(sum(t["pnl_dollar"] for t in losses))
pf         = gross_win / gross_loss if gross_loss else 0

by_reason = {}
for t in all_trades:
    r = t["exit_reason"]
    by_reason.setdefault(r, {"count": 0, "pnl": 0, "wins": 0})
    by_reason[r]["count"] += 1
    by_reason[r]["pnl"]   += t["pnl_dollar"]
    if t["pnl_dollar"] > 0:
        by_reason[r]["wins"] += 1

by_ticker = {}
for t in all_trades:
    tk = t["ticker"]
    by_ticker.setdefault(tk, {"trades": 0, "pnl": 0, "wins": 0})
    by_ticker[tk]["trades"] += 1
    by_ticker[tk]["pnl"]    += t["pnl_dollar"]
    if t["pnl_dollar"] > 0:
        by_ticker[tk]["wins"] += 1

# Confidence tier breakdown (2★–4★)
by_confidence = {}
for t in all_trades:
    stars = t.get("confidence_stars", 2)
    by_confidence.setdefault(stars, {"count": 0, "pnl": 0, "wins": 0, "gross_win": 0, "gross_loss": 0})
    by_confidence[stars]["count"] += 1
    by_confidence[stars]["pnl"]   += t["pnl_dollar"]
    if t["pnl_dollar"] > 0:
        by_confidence[stars]["wins"]      += 1
        by_confidence[stars]["gross_win"] += t["pnl_dollar"]
    else:
        by_confidence[stars]["gross_loss"] += abs(t["pnl_dollar"])

# Per-condition win rate (each trade can match multiple conditions)
CONDITION_KEYS = [
    ("RSI Oversold",    lambda t: "Oversold Dip"    in t["strategies"]),
    ("MACD Cross",      lambda t: "MACD Momentum"   in t["strategies"]),
    ("MA50 Bounce",     lambda t: "MA50 Bounce"      in t["strategies"]),
    ("Volume Spike",    lambda t: "Volume Spike"     in t["strategies"]),
]
by_condition = {}
for label, test in CONDITION_KEYS:
    subset = [t for t in all_trades if test(t)]
    cw = [t for t in subset if t["pnl_dollar"] > 0]
    cl = [t for t in subset if t["pnl_dollar"] <= 0]
    gw = sum(t["pnl_dollar"] for t in cw)
    gl = abs(sum(t["pnl_dollar"] for t in cl)) or 1
    by_condition[label] = {
        "count":    len(subset),
        "wins":     len(cw),
        "pnl":      sum(t["pnl_dollar"] for t in subset),
        "pf":       round(gw / gl, 2) if cw else 0,
    }

# ── VIX regime comparison ────────────────────────────────────────────────────
def _vix_stats(trades):
    if not trades:
        return {"count": 0, "win_rate": 0.0, "pf": 0.0, "pnl": 0.0}
    wins   = [t["pnl_dollar"] for t in trades if t["pnl_dollar"] > 0]
    losses = [t["pnl_dollar"] for t in trades if t["pnl_dollar"] <= 0]
    gw = sum(wins)
    gl = abs(sum(losses)) or 1
    return {
        "count":    len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "pf":       round(gw / gl, 2),
        "pnl":      sum(t["pnl_dollar"] for t in trades),
    }

vix_normal_trades = [t for t in all_trades if not t.get("vix_high", False)]
vix_high_trades   = [t for t in all_trades if     t.get("vix_high", False)]
vix_normal_stats  = _vix_stats(vix_normal_trades)
vix_high_stats    = _vix_stats(vix_high_trades)

# ── Compound Growth Simulation ─────────────────────────────────────────────────
# Sequential: sort trades by exit date, apply each trade's P&L % to 20% of
# the running balance. Simulates $10,000 starting account compounding over time.
compound_balance = float(STARTING_BALANCE)
compound_trades_sorted = sorted(all_trades, key=lambda t: t["exit_date"])
by_year_compound = {}
for t in compound_trades_sorted:
    year     = t["exit_date"][:4]
    pos_size = compound_balance * POSITION_PCT
    trade_pnl = t["pnl_pct"] * pos_size - pos_size * COMMISSION * 2
    compound_balance += trade_pnl
    by_year_compound.setdefault(year, {"trades": 0, "pnl": 0.0, "start_balance": 0.0})
    if by_year_compound[year]["trades"] == 0:
        # capture balance at start of first trade this year
        by_year_compound[year]["start_balance"] = compound_balance - trade_pnl
    by_year_compound[year]["trades"] += 1
    by_year_compound[year]["pnl"]    += trade_pnl
    by_year_compound[year]["end_balance"] = compound_balance
total_compound_gain   = compound_balance - STARTING_BALANCE
total_compound_return = total_compound_gain / STARTING_BALANCE * 100

# ── Write report ───────────────────────────────────────────────────────────────
DIV  = "=" * 80
DIV2 = "-" * 80
_filter_parts = [f"RSI{RSI_ENTRY_MAX}"]
if WEEKLY_TREND_FILTER:  _filter_parts.append("WTF")
if RS_SPY_FILTER:        _filter_parts.append("RS")
if EARNINGS_FILTER:      _filter_parts.append("EF")
if MACD_REVERSAL_EXIT:        _filter_parts.append("MRE")
if STALE_CUT_DAYS > 0:        _filter_parts.append(f"SC{STALE_CUT_DAYS}G{int(STALE_CUT_MIN_GAIN*100)}")
if PROFIT_LOCK_TRIGGER > 0:   _filter_parts.append(f"PL{int(PROFIT_LOCK_TRIGGER*100)}F{int(PROFIT_LOCK_FLOOR*100)}")
if RSI_OVERBOUGHT_EXIT:       _filter_parts.append("OB70")
if RSI_ONLY_MODE:             _filter_parts.append(f"RSIOnly{RSI_ENTRY_MAX}x{RSI_ONLY_EXIT}")
_5star_tag = f"5ST{RSI_5STAR_ENTRY}x{RSI_5STAR_EXIT}" + (f"D{RSI_5STAR_DELAY}" if RSI_5STAR_DELAY > 0 else "") + (f"F{int(FLOOR_5STAR*100)}" if FLOOR_5STAR > 0 else "") + (f"MH{MAX_HOLD_5STAR}" if MAX_HOLD_5STAR != MAX_HOLD else "") + ("_SPREG" if SPY_REGIME_5STAR else "")
_universe_tag = f"RAND{RANDOM_POOL_SIZE}s{RANDOM_SEED}" if USE_RANDOM_POOL else ("SP500" if USE_SP500 else "WL")
if VIX_FILTER: _filter_parts.append(f"VIX{VIX_FILTER_MAX}")
RUN_TAG  = f"TP{TP_PCT_3STAR*100:g}-{TP_PCT_4STAR*100:g}-{TP_PCT_5STAR*100:g}pct_SL{ATR_MULT}_MH{MAX_HOLD}d_TT{TRAIL_TRIGGER*100:g}pct_TP{TRAIL_PCT*100:g}pct_BE{BREAKEVEN_TRIGGER*100:g}pct_{_5star_tag}_{'_'.join(_filter_parts)}_{_universe_tag}"
OUT_FILE = os.path.join(OUT_DIR, f"backtest_{START_DATE}_{END_DATE}_{RUN_TAG}.txt")
lines = []

lines += [
    DIV,
    "  TO THE MOON — Technical Backtest Report",
    f"  Period      : {START_DATE}  →  {END_DATE}",
    f"  Tickers     : {len(TICKERS)} {'random non-S&P500 (seed=' + str(RANDOM_SEED) + ')' if USE_RANDOM_POOL else 'S&P 500' if USE_SP500 else 'watchlist'} stocks",
    f"  Trade Size  : ${TRADE_SIZE:,} assumed per signal",
    f"  Stop-Loss   : {ATR_MULT}× ATR14 from entry",
    f"  Trailing Stop: {TRAIL_TRIGGER*100:.0f}% gain trigger → trails {TRAIL_PCT*100:.0f}% below running high",
    f"  Breakeven Stop: move stop to entry at +{BREAKEVEN_TRIGGER*100:.0f}% gain" if BREAKEVEN_TRIGGER > 0 else "  Breakeven Stop: disabled",
    f"  MACD Reversal Exit: {'ON — exit when histogram crosses below 0 (profitable trades only)' if MACD_REVERSAL_EXIT else 'OFF'}",
    f"  Stale Cut: {'ON — exit at day ' + str(STALE_CUT_DAYS) + ' if gain < ' + str(int(STALE_CUT_MIN_GAIN*100)) + '%' if STALE_CUT_DAYS > 0 else 'OFF'}",
    f"  Profit Lock: {'ON — floor stop at +' + str(int(PROFIT_LOCK_FLOOR*100)) + '% once gain reaches +' + str(int(PROFIT_LOCK_TRIGGER*100)) + '%' if PROFIT_LOCK_TRIGGER > 0 else 'OFF'}",
    f"  RSI Overbought Exit: {'ON — exit when RSI crosses back below 70 (profitable trades only)' if RSI_OVERBOUGHT_EXIT else 'OFF'}",
    f"  RSI-Only Mode: {'ON — entry on RSI < ' + str(RSI_ENTRY_MAX) + ' only; exit when RSI > ' + str(RSI_ONLY_EXIT) if RSI_ONLY_MODE else 'OFF'}",
    f"  Take-Profit : 3★ = {TP_PCT_3STAR*100:.0f}%  |  4★ = {TP_PCT_4STAR*100:.0f}%  |  5★ = {TP_PCT_5STAR*100:.0f}%  (tiered by confidence)",
    f"  5★ Strategy : RSI < {RSI_5STAR_ENTRY}, {RSI_5STAR_DELAY}-bar entry delay, no stop-loss, no stale cut; exit on RSI > {RSI_5STAR_EXIT} or TP or Max Hold",
    f"  5★ SPY Gate : {'ON — entries require SPY above MA200 for 2 consecutive bars' if SPY_REGIME_5STAR else 'OFF'}",
    f"  VIX Filter  : {'ON — BUY entries blocked when VIX > ' + str(VIX_FILTER_MAX) if VIX_FILTER else 'OFF (tagging only — see VIX Regime Comparison)'}",
    f"  Min Signals : 3-of-4 conditions required",
    f"  RSI Entry   : < {RSI_ENTRY_MAX} (baseline was < 40; wider = catches more pullbacks)",
    f"  Weekly Trend: {'ON — price must be > 100-day MA at entry' if WEEKLY_TREND_FILTER else 'OFF'}",
    f"  Rel Strength: {'ON — stock 5-day return must beat SPY' if RS_SPY_FILTER else 'OFF'}",
    f"  Earnings    : {'ON — skip signal if earnings within 5 calendar days' if EARNINGS_FILTER else 'OFF'}",
    f"  Entry Style : MA50 Pullback Bounce (enters on dip recovery, not continuation)",
    f"  Max Hold    : {MAX_HOLD} trading days (3★/4★)  |  {MAX_HOLD_5STAR} trading days (5★)",
    f"  Commission  : {COMMISSION * 100:.1f}% per side",
    f"  Generated   : {datetime.today().strftime('%Y-%m-%d')}",
    DIV, "",
]

lines += [
    "OVERALL PERFORMANCE",
    DIV2,
    f"  Total Trades    : {total}",
    f"  Winning Trades  : {len(wins)}",
    f"  Losing Trades   : {len(losses)}",
    f"  Win Rate        : {win_rate:.1f}%",
    f"  Avg Win         : ${avg_win:+,.2f}",
    f"  Avg Loss        : ${avg_loss:+,.2f}",
    f"  Profit Factor   : {pf:.2f}",
    f"  Total P&L       : ${total_pnl:+,.2f}",
    "",
]

lines += ["COMPOUND GROWTH SIMULATION", DIV2,
          f"  Starting Balance : ${STARTING_BALANCE:,.0f}",
          f"  Position Size    : {POSITION_PCT*100:.0f}% of account per trade (compounds as balance grows)",
          f"  Method           : Sequential by exit date — each trade closes before next is sized",
          ""]
lines.append(f"  {'Year':<6}  {'Trades':>6}  {'Year P&L':>12}  {'End Balance':>14}  {'Year Return':>12}")
lines.append(f"  {'-'*6}  {'-'*6}  {'-'*12}  {'-'*14}  {'-'*12}")
for year in sorted(by_year_compound.keys()):
    d   = by_year_compound[year]
    yr  = d["pnl"] / d["start_balance"] * 100 if d["start_balance"] else 0
    lines.append(f"  {year:<6}  {d['trades']:>6}  ${d['pnl']:>+11,.2f}  ${d['end_balance']:>13,.2f}  {yr:>+11.1f}%")
lines.append(f"  {'-'*6}  {'-'*6}  {'-'*12}  {'-'*14}  {'-'*12}")
lines.append(f"  {'TOTAL':<6}  {total:>6}  ${total_compound_gain:>+11,.2f}  ${compound_balance:>13,.2f}  {total_compound_return:>+11.1f}%")
lines.append("")

lines += ["EXIT REASON BREAKDOWN", DIV2]
for reason, d in sorted(by_reason.items(), key=lambda x: -x[1]["count"]):
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    lines.append(f"  {reason:<22}: {d['count']:>3} trades   Win%: {wr:>5.1f}%   P&L: ${d['pnl']:+,.2f}")
lines.append("")

# ── Entry quality analysis ──────────────────────────────────────────────────
stop_losses = [t for t in all_trades if t["exit_reason"] == "Stop-Loss"]
sl_went_positive = [t for t in stop_losses if t["peak_gain"] > 0]
all_losers = [t for t in all_trades if t["pnl_dollar"] <= 0]
losers_went_positive = [t for t in all_losers if t["peak_gain"] > 0]
avg_peak_sl   = sum(t["peak_gain"] for t in stop_losses) / len(stop_losses) * 100 if stop_losses else 0
avg_peak_wins = sum(t["peak_gain"] for t in wins) / len(wins) * 100 if wins else 0
lines += [
    "ENTRY QUALITY ANALYSIS (Peak Gain During Hold)",
    DIV2,
    f"  Stop-loss trades that went profitable first : {len(sl_went_positive):>3} / {len(stop_losses)}  ({len(sl_went_positive)/len(stop_losses)*100:.1f}%)" if stop_losses else "",
    f"  All losing trades that went profitable first: {len(losers_went_positive):>3} / {len(all_losers)}  ({len(losers_went_positive)/len(all_losers)*100:.1f}%)" if all_losers else "",
    f"  Avg peak gain on stop-loss trades           : {avg_peak_sl:+.2f}%",
    f"  Avg peak gain on winning trades             : {avg_peak_wins:+.2f}%",
    "",
]

lines += ["PER-TICKER SUMMARY", DIV2,
          f"  {'Ticker':<8}  {'Trades':>6}  {'Wins':>5}  {'Win%':>6}  {'Total P&L':>12}"]
lines.append(f"  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*12}")
for tk, d in sorted(by_ticker.items(), key=lambda x: -x[1]["pnl"]):
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    lines.append(f"  {tk:<8}  {d['trades']:>6}  {d['wins']:>5}  {wr:>5.1f}%  ${d['pnl']:>+11,.2f}")
lines.append("")

lines += ["CONFIDENCE TIER BREAKDOWN", DIV2,
          f"  {'Stars':<8}  {'Trades':>6}  {'Wins':>5}  {'Win%':>6}  {'PF':>5}  {'Total P&L':>12}"]
lines.append(f"  {'-'*8}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*12}")
for stars in sorted(by_confidence.keys()):
    d  = by_confidence[stars]
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    pf_val = round(d["gross_win"] / d["gross_loss"], 2) if d["gross_loss"] else 0
    lines.append(f"  {stars}★{'':<6}  {d['count']:>6}  {d['wins']:>5}  {wr:>5.1f}%  {pf_val:>5.2f}  ${d['pnl']:>+11,.2f}")
lines.append("")

lines += ["VIX REGIME COMPARISON", DIV2,
          f"  VIX threshold : > {VIX_FILTER_MAX} = HIGH FEAR  |  VIX filter active: {'YES — high-VIX BUYs were blocked' if VIX_FILTER else 'NO — all trades shown; high-VIX tagged for analysis'}",
          f"  {'Regime':<22}  {'Trades':>6}  {'Win%':>6}  {'PF':>5}  {'Total P&L':>12}",
          f"  {'-'*22}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*12}",
          f"  {'Normal VIX (≤ ' + str(VIX_FILTER_MAX) + ')':<22}  {vix_normal_stats['count']:>6}  {vix_normal_stats['win_rate']:>5.1f}%  {vix_normal_stats['pf']:>5.2f}  ${vix_normal_stats['pnl']:>+11,.2f}",
          f"  {'High VIX (> ' + str(VIX_FILTER_MAX) + ')':<22}  {vix_high_stats['count']:>6}  {vix_high_stats['win_rate']:>5.1f}%  {vix_high_stats['pf']:>5.2f}  ${vix_high_stats['pnl']:>+11,.2f}",
          f"  {'─'*22}  {'─'*6}  {'─'*6}  {'─'*5}  {'─'*12}",
          f"  {'ALL TRADES':<22}  {total:>6}  {win_rate:>5.1f}%  {pf:>5.2f}  ${total_pnl:>+11,.2f}",
          ""]

lines += ["CONDITION WIN RATE ANALYSIS", DIV2,
          "  (Each trade can match multiple conditions — shows which signals add the most value)",
          f"  {'Condition':<16}  {'Trades':>6}  {'Wins':>5}  {'Win%':>6}  {'PF':>5}  {'Total P&L':>12}"]
lines.append(f"  {'-'*16}  {'-'*6}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*12}")
for label, d in by_condition.items():
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    lines.append(f"  {label:<16}  {d['count']:>6}  {d['wins']:>5}  {wr:>5.1f}%  {d['pf']:>5.2f}  ${d['pnl']:>+11,.2f}")
lines.append("")

lines += ["FULL TRADE LOG", DIV2, ""]
for idx, t in enumerate(all_trades, 1):
    result = "WIN " if t["pnl_dollar"] > 0 else "LOSS"
    stars  = t.get("confidence_stars", "?")
    lines.append(f"  [{idx:>3}]  {t['ticker']:<6}  {t['signal']}  |  {result}  |  {t['exit_reason']}  ({stars}★)")
    lines.append(f"         Entry    : {t['entry_date']}  @  ${t['entry_price']:.2f}")
    lines.append(f"         Exit     : {t['exit_date']}  @  ${t['exit_price']:.2f}  ({t['hold_days']}d held)")
    lines.append(f"         Stop     : ${t['stop']:.2f}   Target: ${t['target']:.2f}")
    lines.append(f"         P&L      : {t['pnl_pct']:+.2%}  /  ${t['pnl_dollar']:+,.2f}   Peak: {t['peak_gain']:+.2%}")
    lines.append(f"         Signals  : {t['strategies']}")
    lines.append("")

lines += [DIV, "  End of Backtest Report", DIV]

with open(OUT_FILE, "w") as f:
    f.write("\n".join(lines))

print(f"\n✅  Saved: {OUT_FILE}")
print(f"   {total} trades | Win rate: {win_rate:.1f}% | Profit factor: {pf:.2f} | Total P&L: ${total_pnl:+,.2f}")

# ── CSV export for Google Sheets ───────────────────────────────────────────────
import csv

CSV_FILE = os.path.join(OUT_DIR, f"backtest_{START_DATE}_{END_DATE}_{RUN_TAG}.csv")

# Sheet 1 rows: trade log
with open(CSV_FILE, "w", newline="") as f:
    writer = csv.writer(f)

    # Summary block at top
    writer.writerow(["TO THE MOON — Technical Backtest Results"])
    writer.writerow(["Period", f"{START_DATE} → {END_DATE}"])
    writer.writerow(["Tickers", f"{len(TICKERS)} {'S&P 500' if USE_SP500 else 'watchlist'} stocks"])
    writer.writerow(["Trade Size", f"${TRADE_SIZE:,}"])
    writer.writerow(["Stop-Loss", f"{ATR_MULT}x ATR14"])
    writer.writerow(["Trailing Stop", f"{TRAIL_TRIGGER*100:.0f}% gain trigger, trails {TRAIL_PCT*100:.0f}% below running high"])
    writer.writerow(["Take-Profit", f"3★ = {TP_PCT_3STAR*100:.0f}%  |  4★ = {TP_PCT_4STAR*100:.0f}%  |  5★ = {TP_PCT_5STAR*100:.0f}%  (tiered by confidence)"])
    writer.writerow(["5★ Strategy", f"RSI < {RSI_5STAR_ENTRY}, no stop-loss, exit on RSI > {RSI_5STAR_EXIT} / TP / Max Hold ({MAX_HOLD_5STAR}d)"])
    writer.writerow(["Min Signals", "3-of-4 conditions required"])
    writer.writerow(["Max Hold", f"{MAX_HOLD} trading days"])
    writer.writerow(["Commission", f"{COMMISSION*100:.1f}% per side"])
    writer.writerow(["Generated", datetime.today().strftime("%Y-%m-%d")])
    writer.writerow([])
    writer.writerow(["OVERALL PERFORMANCE"])
    writer.writerow(["Total Trades", total])
    writer.writerow(["Winning Trades", len(wins)])
    writer.writerow(["Losing Trades", len(losses)])
    writer.writerow(["Win Rate", f"{win_rate:.1f}%"])
    writer.writerow(["Avg Win", f"${avg_win:+,.2f}"])
    writer.writerow(["Avg Loss", f"${avg_loss:+,.2f}"])
    writer.writerow(["Profit Factor", f"{pf:.2f}"])
    writer.writerow(["Total P&L", f"${total_pnl:+,.2f}"])
    writer.writerow([])

    # Compound growth simulation
    writer.writerow(["COMPOUND GROWTH SIMULATION"])
    writer.writerow(["Starting Balance", f"${STARTING_BALANCE:,.0f}"])
    writer.writerow(["Position Size", f"{POSITION_PCT*100:.0f}% of account per trade"])
    writer.writerow(["Year", "Trades", "Year P&L", "End Balance", "Year Return %"])
    for year in sorted(by_year_compound.keys()):
        d  = by_year_compound[year]
        yr = d["pnl"] / d["start_balance"] * 100 if d["start_balance"] else 0
        writer.writerow([year, d["trades"], f"${d['pnl']:+,.2f}", f"${d['end_balance']:,.2f}", f"{yr:+.1f}%"])
    writer.writerow(["TOTAL", total, f"${total_compound_gain:+,.2f}", f"${compound_balance:,.2f}", f"{total_compound_return:+.1f}%"])
    writer.writerow([])

    # Exit reason breakdown
    writer.writerow(["EXIT REASON BREAKDOWN"])
    writer.writerow(["Exit Reason", "Trades", "Wins", "Win %", "Total P&L"])
    for reason, d in sorted(by_reason.items(), key=lambda x: -x[1]["count"]):
        wr = d["wins"] / d["count"] * 100 if d["count"] else 0
        writer.writerow([reason, d["count"], d["wins"], f"{wr:.1f}%", f"${d['pnl']:+,.2f}"])
    writer.writerow([])

    # Per-ticker summary
    writer.writerow(["PER-TICKER SUMMARY"])
    writer.writerow(["Ticker", "Trades", "Wins", "Win %", "Total P&L"])
    for tk, d in sorted(by_ticker.items(), key=lambda x: -x[1]["pnl"]):
        wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        writer.writerow([tk, d["trades"], d["wins"], f"{wr:.1f}%", f"${d['pnl']:+,.2f}"])
    writer.writerow([])

    # Confidence tier breakdown
    writer.writerow(["CONFIDENCE TIER BREAKDOWN"])
    writer.writerow(["Stars", "Trades", "Wins", "Win %", "PF", "Total P&L"])
    for stars in sorted(by_confidence.keys()):
        d  = by_confidence[stars]
        wr = d["wins"] / d["count"] * 100 if d["count"] else 0
        pf_val = round(d["gross_win"] / d["gross_loss"], 2) if d["gross_loss"] else 0
        writer.writerow([f"{stars}★", d["count"], d["wins"], f"{wr:.1f}%", pf_val, f"${d['pnl']:+,.2f}"])
    writer.writerow([])

    # Per-condition analysis
    writer.writerow(["CONDITION WIN RATE ANALYSIS"])
    writer.writerow(["Condition", "Trades", "Wins", "Win %", "PF", "Total P&L"])
    for label, d in by_condition.items():
        wr = d["wins"] / d["count"] * 100 if d["count"] else 0
        writer.writerow([label, d["count"], d["wins"], f"{wr:.1f}%", d["pf"], f"${d['pnl']:+,.2f}"])
    writer.writerow([])

    # Full trade log
    writer.writerow(["FULL TRADE LOG"])
    writer.writerow(["#", "Ticker", "Signal", "Stars", "Result", "Entry Date", "Entry Price",
                     "Stop Loss", "Target", "Exit Date", "Exit Price",
                     "Days Held", "Exit Reason", "P&L %", "P&L $", "Strategies"])
    for idx, t in enumerate(all_trades, 1):
        result = "WIN" if t["pnl_dollar"] > 0 else "LOSS"
        writer.writerow([
            idx,
            t["ticker"],
            t["signal"],
            t.get("confidence_stars", ""),
            result,
            t["entry_date"],
            t["entry_price"],
            t["stop"],
            t["target"],
            t["exit_date"],
            t["exit_price"],
            t["hold_days"],
            t["exit_reason"],
            f"{t['pnl_pct']:+.2%}",
            round(t["pnl_dollar"], 2),
            t["strategies"],
        ])

print(f"✅  CSV:   {CSV_FILE}")
