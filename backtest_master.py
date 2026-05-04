"""
backtest_master.py — The Ultimate All-Weather 7-Engine Combined Backtest
=======================================================================
Runs all engines independently on a shared account:
1 - CORE SWING:        Oversold pullbacks in Bull Regimes on S&P 500
2 - INDEX FADE:        Fading RSI > 80 via Inverse ETFs
3 - LEVERAGED BOUNCE:  Catching pure Capitulation via 3x ETFs
4 - REGIME MOMENTUM:   SPY trend-following via UPRO
5 - SECTOR HUNTER:     Oversold dips in hot sectors
6 - RANGE REVERSION:   BB mean-reversion in sideways/chop markets
7 - DOUBLE BOTTOM:     W2 anticipation off confirmed W1 swing lows

Period: 2020-01-01 → 2026-01-01
"""

import io
import os
import re
import numpy as np
import pandas as pd
import yfinance as yf
import urllib.request
from datetime import datetime
from scipy.signal import argrelextrema

# ── Paths ───────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
OUT_DIR  = os.path.join(BASE_DIR, "backtest results")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Date range ─────────────────────────────────────────────────────────────────
START_DATE = "2020-01-01"
END_DATE   = "2026-01-01"

# ── Shared position sizing ──────────────────────────────────────────────────────
TRADE_SIZE           = 2_000
TRADE_SIZE_INCREMENT = 300
STARTING_BALANCE     = 10_000
COMMISSION           = 0.001   # 0.1% per side

FIVE_STAR_SIZE_MULT  = 1.30   # 5★ and 6★ trades are sized 30% larger

def _trade_size(date):
    return round(TRADE_SIZE + TRADE_SIZE_INCREMENT * max(0, date.year - int(START_DATE[:4])))

def _trade_size_5star(date):
    return round(_trade_size(date) * FIVE_STAR_SIZE_MULT)

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 1 — SWING config
# ═══════════════════════════════════════════════════════════════════════════════
SW_RSI_ENTRY_MAX       = 40
SW_RSI_5STAR_ENTRY     = 25
SW_RSI_5STAR_EXIT      = 72
SW_ATR_MULT            = 2.0
SW_TP_PCT_3STAR        = 0.15
SW_TP_PCT_4STAR        = 0.20
SW_TP_PCT_5STAR        = 0.25
SW_TP_PCT_5STAR_MAX    = 0.25
SW_FLOOR_5STAR         = 0.10    
SW_TRAIL_TRIGGER       = 0.07
SW_TRAIL_PCT           = 0.08
SW_STALE_CUT_DAYS      = 12
SW_MAX_HOLD            = 30
SW_MAX_HOLD_5STAR      = 35
SW_IBS_MAX_ENTRY_5STAR = 0.25   
SW_IBS_MAX_ENTRY_4STAR = 0.30   
SW_IBS_MIN_EXIT        = 0.90
SW_VOL_SPIKE_MIN       = 1.5
SW_COOLDOWN_BARS       = 5
SP500_LIMIT            = 500

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 2 — INDEX FADE config
# ═══════════════════════════════════════════════════════════════════════════════
IX_RSI_OB         = 80
IX_RSI_OB_EXTREME = 85
IX_STOP_PCT       = 0.15
IX_TP_BASE        = 0.15
IX_TP_EXTREME     = 0.25
IX_RSI_MEAN_EXIT  = 50
IX_STALE_CUT_DAYS = 20
IX_MAX_HOLD       = 30
IX_COOLDOWN_BARS  = 3

IX_GROUPS = [
    {"name": "SP500",         "signals": ["SPY", "UPRO"],  "levered": ["UPRO"],  "execution": "SPXU"},
    {"name": "NASDAQ",        "signals": ["QQQ", "TQQQ"],  "levered": ["TQQQ"],  "execution": "SQQQ"},
    {"name": "SP500_BOUNCE",  "signals": ["SPXU"],          "levered": ["SPXU"],  "execution": "UPRO"},
    {"name": "NASDAQ_BOUNCE", "signals": ["SQQQ"],          "levered": ["SQQQ"],  "execution": "TQQQ"},
]

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 3 — LEVERAGED SHOCK-BOUNCE config
# ═══════════════════════════════════════════════════════════════════════════════
LEV_UNDERLYING_MAP = {
    "UPRO": "SPY",
    "TQQQ": "QQQ",
    "TSLL": "TSLA",
    "NVDL": "NVDA"
}
LEV_TP_PCT             = 0.35    
LEV_STOP_MIN_PCT       = 0.15    
LEV_RSI_OB_EXIT        = 70      
LEV_MAX_HOLD           = 35      
LEV_TRAIL_PCT          = 0.10

LEV_4STAR_TP_PCT       = 0.10
LEV_4STAR_STOP_PCT     = 0.09
LEV_4STAR_MAX_HOLD     = 12

LEV_5STAR_BREAKOUT_TRAIL_PCT = 0.15
LEV_5STAR_BREAKOUT_MAX_HOLD  = 30

LEV_MB_RSI_MIN         = 40     
LEV_MB_RSI_MAX         = 60     

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 4 — SPY REGIME MOMENTUM (UPRO) config
# ═══════════════════════════════════════════════════════════════════════════════
RG_RSI_PULLBACK       = 52
RG_UPRO_TP_PCT        = 0.25   # Strategy A TP
RG_UPRO_TRAIL_TRIGGER = 0.17
RG_UPRO_TRAIL_PCT     = 0.10
RG_UPRO_FLOOR_PCT     = 0.12
RG_UPRO_MAX_HOLD      = 90
RG_BULL_TP_PCT        = 0.35   # Strategy B TP (Churn)
RG_BULL_SL_PCT        = 0.40
RG_BULL_MAX_DAYS      = 30     # 30-day Churn

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 5 — SECTOR HUNTER config
# ═══════════════════════════════════════════════════════════════════════════════
E5_TOP_N_SECTORS        = 3      # Hot sectors to qualify (top N by blended RS rank)
E5_RS_WINDOW_SHORT      = 20     # Short RS lookback (trading days)
E5_RS_WINDOW_LONG       = 60     # Long RS lookback (trading days)
E5_RS_SLOPE_WINDOW      = 5      # Days for RS slope trajectory check
E5_ENTRY_RSI_MAX        = 40     # Max RSI for oversold entry
E5_MAX_HOLD             = 30     # Max holding period (trading days)
E5_TP_PCT               = 0.25   # Take-profit: +25%
E5_SL_PCT               = 0.12   # Hard stop-loss: -12%
E5_TRAIL_TRIGGER        = 0.08   # Trailing stop activates after +8% gain
E5_TRAIL_PCT            = 0.04   # Trail 4% below peak (locks in ~4% min profit)
E5_STALE_CUT_DAYS       = 15     # Exit if still negative after this many hold days
E5_LADDER_TRIGGER       = 0.03   # Ladder buy when winning position pulls back near MA20
E5_LADDER_RSI_MAX       = 50     # Max RSI for ladder buy confirmation
E5_COOLDOWN_BARS        = 3      # Cooldown bars after a stop-loss exit
E5_STOCK_LOSS_STREAK    = 2      # Consecutive losses on same stock before cooldown
E5_STOCK_LOSS_COOLDOWN  = 5      # Cooldown bars after hitting stock loss streak
E5_GLOBAL_LOSS_STREAK   = 3      # Consecutive losses across ALL trades before engine-wide pause
E5_GLOBAL_COOLDOWN_DAYS = 7      # Business days to block all new entries after global streak

E5_EXCLUDED_SECTORS = {
    "Communication Services",
    "Consumer Staples",
    "Materials",
    "Energy",
    "Utilities",
}

E5_SECTOR_STARS = {
    "Health Care":            5,   # Blue Diamond — 1.3× position size
    "Industrials":            5,   # Blue Diamond — 1.3× position size
    "Real Estate":            5,   # Blue Diamond — 1.3× position size
    "Financials":             4,
    "Information Technology": 4,
    "Consumer Discretionary": 4,
}

E5_SECTOR_ETF_MAP = {
    "Information Technology": "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials":            "XLI",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
}
E5_SECTOR_ETFS = list(E5_SECTOR_ETF_MAP.values())

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 6 — RANGE REVERSION config
# ═══════════════════════════════════════════════════════════════════════════════
E6_BB_PERIOD           = 20
E6_BB_STD              = 2.0
E6_VOL_SPIKE_MIN       = 1.5
E6_ADX_MIN             = 10
E6_ADX_MAX             = 22
E6_ADX_PERIOD          = 14
E6_RSI_MIN             = 35
E6_RSI_MAX             = 65
E6_CHOP_BARS           = 7
E6_RANGE_BARS          = 10
E6_DIP_LOOKBACK        = 10
E6_DIP_MIN_PCT         = 0.02
E6_DIP_AVOID_LOW       = 0.05
E6_DIP_AVOID_HIGH      = 0.09
E6_DEEP_REBOUND_PCT    = 0.08
E6_STRONG_RECOVER_PCT  = 0.05
E6_SHALLOW_RECOVER_PCT = 0.08
E6_STOP_PCT            = 0.06
E6_TP_PCT              = 0.10
E6_STALE_CUT_DAYS      = 12
E6_MAX_HOLD            = 25
E6_COOLDOWN_BARS       = 1
E6_CONSEC_LOSS_LIMIT   = 5
E6_CONSEC_LOSS_LIMIT2  = 3
E6_LOSS_COOLDOWN_DAYS  = 15
E6_LOSS_COOLDOWN_DAYS2 = 7
E6_TRAIL_ACTIVATE_PCT  = 0.08
E6_TRAIL_DISTANCE_PCT  = 0.04
TIER1_SIZE_MULT        = 1.50
TIER2_SIZE_MULT        = 1.00
TIER3_SIZE_MULT        = 0.80

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 7 — DOUBLE BOTTOM W2 ANTICIPATION config
# ═══════════════════════════════════════════════════════════════════════════════
E7_LOOKBACK        = 150
E7_ORDER_W1        = 10
E7_MIN_SEP         = 15
E7_ZONE_PCT        = 0.05
E7_ZONE_CANCEL_PCT = 0.04
E7_TOUCH_PCT       = 0.01
E7_CONFIRM_BARS    = 5
E7_NECKLINE_MIN    = 0.03
E7_DEPTH_WINDOW    = 30
E7_DEPTH_MIN       = 0.05
E7_VELOCITY_MIN    = 0.005
E7_VELOCITY_MAX    = 0.015
E7_BAR_BODY_MIN    = 0.006
E7_BAR_BODY_STRONG = 0.010
E7_VOL_MULT        = 1.5
E7_VOL_MULT_RELAX  = 1.2
E7_STOP_PCT        = 0.06
E7_TRAIL_PCT       = 0.05
E7_STALL_GAIN_PCT  = 0.07
E7_STALL_BARS      = 7
E7_STALL_TRAIL_PCT = 0.03
E7_STALE_DAYS      = 35


# ── Load S&P 500 tickers ────────────────────────────────────────────────────────
print("Loading S&P 500 tickers and sector data from Wikipedia...")
try:
    _req = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 (compatible; backtest/1.0)"},
    )
    with urllib.request.urlopen(_req) as _resp:
        _html = _resp.read()
    _table = pd.read_html(io.BytesIO(_html))[0]
    _table["Symbol"] = _table["Symbol"].str.replace(".", "-", regex=False)
    SW_TICKERS = _table["Symbol"].tolist()[:SP500_LIMIT]
    E5_TICKER_SECTOR = dict(zip(_table["Symbol"], _table["GICS Sector"]))
    print(f"  Loaded {len(SW_TICKERS)} S&P 500 tickers across {_table['GICS Sector'].nunique()} sectors")
except Exception:
    print("Warning: could not fetch S&P 500. Using basic fallback.")
    SW_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN"]  # Basic fallback
    E5_TICKER_SECTOR = {}

# ── Combined ticker list ────────────────────────────────────────────────────────
IX_ETFs = set()
for g in IX_GROUPS:
    IX_ETFs.update(g["signals"])
    IX_ETFs.add(g["execution"])

LEV_ETFs = set(LEV_UNDERLYING_MAP.keys()) | set(LEV_UNDERLYING_MAP.values())

ALL_TICKERS = sorted(set(SW_TICKERS) | IX_ETFs | LEV_ETFs | set(E5_SECTOR_ETFS) | {"SPY"})

# ── Indicator helpers ──────────────────────────────────────────────────────────
def _rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _sma_rsi(close, period=14):
    delta = close.diff()
    gain  = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _macd_hist(close, fast=12, slow=26, sig=9):
    ema_f  = close.ewm(span=fast, adjust=False).mean()
    ema_s  = close.ewm(span=slow, adjust=False).mean()
    macd   = ema_f - ema_s
    signal = macd.ewm(span=sig, adjust=False).mean()
    return macd - signal

def _bollinger(close, period=20, std_dev=2):
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    return mid + std_dev * std, mid, mid - std_dev * std

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
    df["sma_rsi"]   = _sma_rsi(df["Close"])
    df["macd_hist"] = _macd_hist(df["Close"])
    df["ma20"]      = df["Close"].rolling(20).mean()
    df["ma50"]      = df["Close"].rolling(50).mean()
    df["ma200"]     = df["Close"].rolling(200).mean()
    df["high20"]    = df["High"].rolling(20).max()
    df["ret63"]     = df["Close"].pct_change(63)
    df["atr14"]     = _atr(df["High"], df["Low"], df["Close"])
    df["vol_avg20"] = df["Volume"].rolling(20).mean()
    df["rel_vol"]   = df["Volume"] / df["vol_avg20"]
    df["ibs"]       = (df["Close"] - df["Low"]) / (df["High"] - df["Low"])
    bb_upper, _, bb_lower = _bollinger(df["Close"])
    df["bb_upper"]  = bb_upper
    df["bb_lower"]  = bb_lower
    return df

def build_e6_indicator_df(df):
    """Adds Engine-6-specific columns (ADX, chop/range streaks, dip metrics) to base indicator df."""
    d = df.copy()

    # ADX
    hi, lo, cl = d["High"], d["Low"], d["Close"]
    tr        = pd.concat([(hi - lo), (hi - cl.shift()).abs(), (lo - cl.shift()).abs()], axis=1).max(axis=1)
    atr_e6    = tr.ewm(com=E6_ADX_PERIOD - 1, adjust=False).mean()
    up        = hi.diff().clip(lower=0)
    dn        = (-lo.diff()).clip(lower=0)
    plus_dm   = up.where(up > dn, 0)
    minus_dm  = dn.where(dn > up, 0)
    plus_di   = 100 * plus_dm.ewm(com=E6_ADX_PERIOD - 1, adjust=False).mean() / atr_e6.replace(0, np.nan)
    minus_di  = 100 * minus_dm.ewm(com=E6_ADX_PERIOD - 1, adjust=False).mean() / atr_e6.replace(0, np.nan)
    dx        = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    d["adx"]  = dx.ewm(com=E6_ADX_PERIOD - 1, adjust=False).mean()

    # ADX consecutive bars below threshold
    below_adx       = (d["adx"] < E6_ADX_MAX).astype(int)
    d["adx_consec"] = below_adx.groupby((below_adx != below_adx.shift()).cumsum()).cumcount() + 1
    d["adx_consec"] = d["adx_consec"] * below_adx

    # 20-day range (low side)
    low20    = d["Low"].rolling(20).min()
    in_range = ((d["Close"] < d["high20"]) & (d["Close"] > low20)).astype(int)
    consec_r = in_range.groupby((in_range != in_range.shift()).cumsum()).cumcount() + 1
    d["range_consec"] = consec_r * in_range

    # Recent dip below lower BB
    below_flag           = (d["Close"] < d["bb_lower"]).astype(int)
    d["recent_below_bb"] = below_flag.shift(1).rolling(E6_DIP_LOOKBACK).max()

    # Max dip depth below lower BB in lookback window
    dip_pct          = ((d["bb_lower"] - d["Close"]) / d["bb_lower"]).clip(lower=0)
    d["max_dip_pct"] = dip_pct.shift(1).rolling(E6_DIP_LOOKBACK).max()

    # Lowest close in lookback window
    d["min_close_lookback"] = d["Close"].shift(1).rolling(E6_DIP_LOOKBACK).min()

    return d


# ── Swing signal helpers ───────────────────────────────────────────────────────
def sw_get_signal(r, p, pp, i):
    if i < 2: return None
    if any(pd.isna(r[c]) for c in ("rsi", "macd_hist", "ma50", "ma200", "atr14")): return None
    
    bull_regime = r["Close"] > r["ma200"]
    buy_conds = [
        r["rsi"] < SW_RSI_ENTRY_MAX,
        r["macd_hist"] > 0 and p["macd_hist"] <= 0,
        r["Close"] > r["ma50"] and p["Close"] <= p["ma50"] * 1.01,
        not pd.isna(r.get("rel_vol", float("nan"))) and r["rel_vol"] >= SW_VOL_SPIKE_MIN,
    ]
    buys      = sum(bool(c) for c in buy_conds)
    two_green = r["Close"] > p["Close"] and p["Close"] > pp["Close"]
    return "BUY" if buys >= 3 and two_green and bull_regime else None

def sw_confidence(r, p, recent_rsis, i):
    # Deep Oversold gets 5★ Blue Diamond (internally 6)
    if r["rsi"] < SW_RSI_5STAR_ENTRY: return 6
    
    # Everything else is exactly 4 stars
    return 4

def sw_strategy_labels(r, p, i):
    labels = []
    if r["rsi"] < SW_RSI_5STAR_ENTRY: labels.append(f"Deep Oversold 5★ (RSI {r['rsi']:.1f})")
    elif r["rsi"] < SW_RSI_ENTRY_MAX: labels.append(f"Oversold Dip (RSI {r['rsi']:.1f})")
    if r["macd_hist"] > 0 and p["macd_hist"] <= 0: labels.append("MACD Momentum Cross")
    if r["Close"] > r["ma50"] and p["Close"] <= p["ma50"] * 1.01: labels.append("MA50 Bounce")
    if not pd.isna(r.get("rel_vol", float("nan"))) and r["rel_vol"] >= SW_VOL_SPIKE_MIN:
        labels.append(f"Volume Spike ({r['rel_vol']:.1f}x)")
    return ", ".join(labels)

# ── Download all data ──────────────────────────────────────────────────────────
warmup = (pd.Timestamp(START_DATE) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
print(f"\nDownloading {len(ALL_TICKERS)} tickers ({warmup} → {END_DATE})...")
raw = yf.download(ALL_TICKERS, start=warmup, end=END_DATE, progress=False, auto_adjust=True)
print()

print("Building indicators...")
ticker_dfs = {}
for ticker in ALL_TICKERS:
    try:
        if raw.columns.nlevels > 1:
            df = raw.xs(ticker, axis=1, level=1).copy()
        else:
            df = raw.copy()
        df = df.dropna(subset=["Close"])
        if len(df) < 210: continue
        ticker_dfs[ticker] = build_indicator_df(df)
    except Exception as e:
        continue

print(f"  {len(ticker_dfs)} tickers with sufficient history\n")

# ── Engine 6: pre-compute chop/dip indicators ──────────────────────────────────
print("Building Engine 6 indicators (ADX, chop streaks, dip metrics)...")
e6_ticker_dfs = {}
for _t in SW_TICKERS:
    if _t not in ticker_dfs: continue
    try:
        e6_ticker_dfs[_t] = build_e6_indicator_df(ticker_dfs[_t])
    except Exception:
        continue
print(f"  {len(e6_ticker_dfs)} tickers with E6 indicators\n")

# Build SPY E6 indicators separately for ADX regime gate

START_TS = pd.Timestamp(START_DATE)

# ── Compute true relative strength vs SPY for Engine 4 ─────────────────────────
if "SPY" in ticker_dfs:
    spy_ret63 = ticker_dfs["SPY"]["ret63"]
    for ticker in SW_TICKERS:
        if ticker not in ticker_dfs: continue
        stk_ret63 = ticker_dfs[ticker]["ret63"]
        spy_aligned = spy_ret63.reindex(stk_ret63.index)
        ticker_dfs[ticker]["rs_spy"] = stk_ret63 / spy_aligned.replace(0, float("nan"))

# ── Engine 5: compute daily hot-sector set ───────────────────────────────────
print("Computing sector RS rankings (Engine 5)...")
_spy_close = ticker_dfs["SPY"]["Close"] if "SPY" in ticker_dfs else None
_e5_rs_data = {}
for _etf in E5_SECTOR_ETFS:
    if _etf not in ticker_dfs or _spy_close is None: continue
    _sec_close = ticker_dfs[_etf]["Close"]
    _spy_aligned = _spy_close.reindex(_sec_close.index)
    _e5_rs_data[_etf] = _sec_close / _spy_aligned.replace(0, np.nan)

hot_sectors_by_date = {}
if _e5_rs_data:
    _rs_df     = pd.DataFrame(_e5_rs_data)
    _rs_short  = _rs_df.pct_change(E5_RS_WINDOW_SHORT)
    _rs_long   = _rs_df.pct_change(E5_RS_WINDOW_LONG)
    _avg_rank  = (_rs_short.rank(axis=1, ascending=False) + _rs_long.rank(axis=1, ascending=False)) / 2.0
    _rs_slope  = _rs_df.diff(E5_RS_SLOPE_WINDOW)
    for _date, _row_rank in _avg_rank.iterrows():
        if _date < START_TS: continue
        _row_slope = _rs_slope.loc[_date]
        _hot = set()
        for _etf in E5_SECTOR_ETFS:
            if _etf not in _row_rank.index: continue
            if pd.isna(_row_rank[_etf]) or pd.isna(_row_slope[_etf]): continue
            if _row_rank[_etf] <= E5_TOP_N_SECTORS and _row_slope[_etf] > 0:
                _hot.add(_etf)
        hot_sectors_by_date[_date] = _hot
    print(f"  Rankings computed for {len(hot_sectors_by_date)} trading days\n")

all_trades = []

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 1 — SWING BACKTEST LOOP (Optimized Dict Lookup)
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 1: SWING strategy...")
swing_trades = []
for ticker in SW_TICKERS:
    if ticker not in ticker_dfs: continue
    df     = ticker_dfs[ticker]
    dates  = df.index.tolist()
    if not any(d >= START_TS for d in dates): continue
    
    records = df.to_dict('records')
    recent_rsi_cache = df['rsi'].values

    position, hold_days, cooldown = None, 0, 0
    for i in range(len(records)):
        date = dates[i]
        if date < START_TS: continue
        row = records[i]
        close, high, low, atr = float(row["Close"]), float(row["High"]), float(row["Low"]), row["atr14"]

        if cooldown > 0: cooldown -= 1

        if position is not None:
            hold_days += 1
            exit_price, exit_reason = None, None

            if not pd.isna(row["rsi"]) and row["rsi"] > 50: position["rsi_above_50"] = True
            if high > position["trail_high"]: position["trail_high"] = high

            if not position["is_5star"]:
                gain_pct = (position["trail_high"] - position["entry_price"]) / position["entry_price"]
                if gain_pct >= SW_TRAIL_TRIGGER:
                    trail_stop = position["trail_high"] * (1 - SW_TRAIL_PCT)
                    if trail_stop > position["stop"]: position["stop"] = trail_stop

            current_gain = (close - position["entry_price"]) / position["entry_price"]

            if not position["is_5star"] and low <= position["stop"]:
                exit_price, exit_reason = position["stop"], "Stop-Loss"
            elif high >= position["target"]:
                exit_price, exit_reason = position["target"], "Take-Profit"

            if exit_price is None and not position["is_5star"] and position["rsi_above_50"] and current_gain > 0 and not pd.isna(row["rsi"]) and row["rsi"] < 50 and i > 0 and not pd.isna(records[i - 1]["rsi"]) and records[i - 1]["rsi"] >= 50:
                exit_price, exit_reason = close, "RSI Momentum Exit"

            if exit_price is None and position["is_5star"] and not pd.isna(row["rsi"]) and row["rsi"] > SW_RSI_5STAR_EXIT:
                exit_price, exit_reason = close, f"RSI Exit 5★ (>{SW_RSI_5STAR_EXIT})"
            if exit_price is None and position["is_5star"] and current_gain > 0 and not pd.isna(row["ibs"]) and row["ibs"] > SW_IBS_MIN_EXIT:
                exit_price, exit_reason = close, f"IBS Exit (>{SW_IBS_MIN_EXIT})"
            if exit_price is None and position["is_5star"] and SW_FLOOR_5STAR > 0 and current_gain <= -SW_FLOOR_5STAR:
                exit_price, exit_reason = close, f"Floor Stop 5★ (-{int(SW_FLOOR_5STAR*100)}%)"

            if exit_price is None and not position["is_5star"] and SW_STALE_CUT_DAYS > 0 and hold_days >= SW_STALE_CUT_DAYS and current_gain < 0:
                exit_price, exit_reason = close, f"Stale Cut ({SW_STALE_CUT_DAYS}d)"

            max_hold_lim = SW_MAX_HOLD_5STAR if position["is_5star"] else SW_MAX_HOLD
            if exit_price is None and hold_days >= max_hold_lim:
                exit_price, exit_reason = close, f"Max Hold ({max_hold_lim}d)"

            if exit_price is not None:
                entry, pos_size = position["entry_price"], position["trade_size"]
                pnl_pct = (exit_price - entry) / entry
                pnl_dollar = pnl_pct * pos_size - pos_size * COMMISSION * 2
                swing_trades.append({
                    "strategy_type":    "SWING",
                    "ticker":           ticker,
                    "entry_date":       position["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price":      round(entry, 2),
                    "exit_date":        date.strftime("%Y-%m-%d"),
                    "exit_price":       round(exit_price, 2),
                    "exit_reason":      exit_reason,
                    "hold_days":        hold_days,
                    "pnl_pct":          pnl_pct,
                    "pnl_dollar":       pnl_dollar,
                    "strategies":       position["strategies"],
                    "confidence_stars": position["confidence_stars"],
                    "trade_size":       pos_size,
                    "is_5star":         position["is_5star"],
                })
                if exit_reason == "Stop-Loss": cooldown = SW_COOLDOWN_BARS
                position, hold_days = None, 0
                continue

        if position is None and not pd.isna(atr) and atr > 0 and cooldown == 0:
            bull_regime_now = not pd.isna(row["ma200"]) and row["Close"] > row["ma200"]
            p_row = records[i-1] if i > 0 else row
            pp_row = records[i-2] if i > 1 else p_row
            
            ibs_block = (i == 0 or pd.isna(p_row["ibs"]) or p_row["ibs"] >= SW_IBS_MAX_ENTRY_5STAR)
            if not pd.isna(row["rsi"]) and row["rsi"] < SW_RSI_5STAR_ENTRY and bull_regime_now and not ibs_block:
                bb_lower_val = row.get("bb_lower", float("nan"))
                is_5star_max = not pd.isna(bb_lower_val) and close <= float(bb_lower_val)
                stars  = 6
                tp_pct = SW_TP_PCT_5STAR_MAX if is_5star_max else SW_TP_PCT_5STAR
                strat  = sw_strategy_labels(row, p_row, i) if i >= 1 else "Deep Oversold 5★ Blue Diamond"
                if is_5star_max: strat = (strat + ", BB Lower Band") if strat else "BB Lower Band"
                position = {
                    "entry_date": date, "entry_price": close, "stop": 0, "target": close * (1 + tp_pct),
                    "trail_high": close, "strategies": strat, "confidence_stars": stars, "trade_size": _trade_size_5star(date),
                    "rsi_above_50": False, "is_5star": True,
                }
                hold_days = 0
            else:
                if sw_get_signal(row, p_row, pp_row, i) == "BUY":
                    prev_ibs = p_row["ibs"] if i > 0 else float("nan")
                    if not (pd.isna(prev_ibs) or prev_ibs >= SW_IBS_MAX_ENTRY_4STAR):
                        recent_rsis_list = recent_rsi_cache[max(0, i-2):i+1]
                        stars  = sw_confidence(row, p_row, recent_rsis_list, i)
                        if stars == 3: stars = 4
                        tp_pct = SW_TP_PCT_4STAR if stars == 4 else SW_TP_PCT_3STAR
                        position = {
                            "entry_date": date, "entry_price": close, "stop": close - SW_ATR_MULT * float(atr),
                            "target": close * (1 + tp_pct), "trail_high": close, "strategies": sw_strategy_labels(row, p_row, i),
                            "confidence_stars": stars, "trade_size": _trade_size(date), "rsi_above_50": False, "is_5star": False,
                        }
                        hold_days = 0
all_trades.extend(swing_trades)


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 2 — INDEX FADE BACKTEST LOOP (Optimized Dict Lookup)
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 2: INDEX FADE strategy...")
index_trades = []
IX_SIG_MAP = {}
for g in IX_GROUPS:
    for sig in g["signals"]: IX_SIG_MAP[sig] = {"execution": g["execution"], "group": g["name"], "is_levered": sig in g["levered"]}

ix_date_idx = None
for g in IX_GROUPS:
    if g["execution"] in ticker_dfs:
        idx = ticker_dfs[g["execution"]].index[ticker_dfs[g["execution"]].index >= START_TS]
        ix_date_idx = idx if ix_date_idx is None else ix_date_idx.intersection(idx)

if ix_date_idx is not None and len(ix_date_idx) > 0:
    ix_state = {sig: {"position": None, "hold_days": 0, "cooldown": 0} for sig in IX_SIG_MAP}
    
    # Pre-cache dicts mappings
    d_cache = {t: ticker_dfs[t].to_dict('index') for t in ticker_dfs if t in IX_ETFs}

    for date in ix_date_idx:
        for sig_ticker, meta in IX_SIG_MAP.items():
            exec_etf = meta["execution"]
            state    = ix_state[sig_ticker]
            if exec_etf not in d_cache or date not in d_cache[exec_etf]: continue

            exec_row = d_cache[exec_etf][date]
            if pd.isna(exec_row["Close"]): continue
            exec_close, exec_high, exec_low = float(exec_row["Close"]), float(exec_row["High"]), float(exec_row["Low"])
            if state["cooldown"] > 0: state["cooldown"] -= 1

            if state["position"] is not None:
                state["hold_days"] += 1
                pos, entry = state["position"], state["position"]["entry_price"]
                current_gain = (exec_close - entry) / entry
                exit_price, exit_reason = None, None

                if exec_low <= pos["stop"]: exit_price, exit_reason = pos["stop"], "Stop-Loss"
                elif exec_high >= pos["target"]: exit_price, exit_reason = pos["target"], "Take-Profit"
                else:
                    if sig_ticker in d_cache and date in d_cache[sig_ticker]:
                        sig_rsi = d_cache[sig_ticker][date]["rsi"]
                        if not pd.isna(sig_rsi) and float(sig_rsi) < IX_RSI_MEAN_EXIT and current_gain > 0:
                            exit_price, exit_reason = exec_close, f"RSI Mean Exit (<{IX_RSI_MEAN_EXIT})"
                    if exit_price is None and IX_STALE_CUT_DAYS > 0 and state["hold_days"] >= IX_STALE_CUT_DAYS and current_gain <= 0:
                        exit_price, exit_reason = exec_close, f"Stale Cut ({IX_STALE_CUT_DAYS}d)"
                    if exit_price is None and state["hold_days"] >= IX_MAX_HOLD:
                        exit_price, exit_reason = exec_close, f"Max Hold ({IX_MAX_HOLD}d)"

                if exit_price is not None:
                    pos_size = pos["trade_size"]
                    pnl_pct = (exit_price - entry) / entry
                    pnl_dollar = pnl_pct * pos_size - pos_size * COMMISSION * 2
                    index_trades.append({
                        "strategy_type":    "INDEX",
                        "ticker":           exec_etf,
                        "entry_date":       pos["entry_date"].strftime("%Y-%m-%d"),
                        "entry_price":      round(entry, 2),
                        "exit_date":        date.strftime("%Y-%m-%d"),
                        "exit_price":       round(exit_price, 2),
                        "exit_reason":      exit_reason,
                        "hold_days":        state["hold_days"],
                        "pnl_pct":          pnl_pct,
                        "pnl_dollar":       pnl_dollar,
                        "strategies":       pos["strategies"],
                        "confidence_stars": pos["confidence_stars"],
                        "trade_size":       pos_size,
                        "is_5star":         pos["is_5star"],
                    })
                    if exit_reason == "Stop-Loss": state["cooldown"] = IX_COOLDOWN_BARS
                    state["position"], state["hold_days"] = None, 0
                    continue

            if state["position"] is None and state["cooldown"] == 0 and sig_ticker in d_cache and date in d_cache[sig_ticker]:
                sig_row = d_cache[sig_ticker][date]
                if not pd.isna(sig_row["rsi"]) and not pd.isna(sig_row["bb_upper"]):
                    rsi_val, bb_up = float(sig_row["rsi"]), float(sig_row["bb_upper"])
                    if rsi_val >= IX_RSI_OB and float(sig_row["Close"]) >= bb_up:
                        stars = 6  # Blue Diamond — RSI 80+ AND upper BB on 3x ETF is max conviction
                        _high_conv = meta["is_levered"] or rsi_val >= IX_RSI_OB_EXTREME
                        state["position"] = {
                            "entry_date": date, "entry_price": exec_close, "stop": exec_close * (1 - IX_STOP_PCT), "target": exec_close * (1 + (IX_TP_EXTREME if _high_conv else IX_TP_BASE)),
                            "strategies": f"{sig_ticker} RSI {rsi_val:.1f} + upper BB", "confidence_stars": 6, "trade_size": _trade_size_5star(date), "is_5star": True,
                        }
                        state["hold_days"] = 0
all_trades.extend(index_trades)


# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 3 — LEVERAGED SHOCK BOUNCE BACKTEST LOOP
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 3: LEVERAGED SHOCK-BOUNCE strategy...")
lev_trades = []

for exec_ticker, base_ticker in LEV_UNDERLYING_MAP.items():
    if exec_ticker not in ticker_dfs or base_ticker not in ticker_dfs: continue
    e_df, b_df = ticker_dfs[exec_ticker], ticker_dfs[base_ticker]

    e_dates = e_df.index.tolist()
    e_recs  = e_df.to_dict('records')
    b_dates = b_df.index.tolist()
    b_recs  = b_df.to_dict('records')
    
    b_date_to_i = {d: i for i, d in enumerate(b_dates)}

    position, hold_days, cooldown = None, 0, 0
    for e_i in range(len(e_recs)):
        date = e_dates[e_i]
        if date < START_TS: continue
        if date not in b_date_to_i: continue
        b_i = b_date_to_i[date]

        e_row, b_row = e_recs[e_i], b_recs[b_i]
        close, high, low, atr = float(e_row["Close"]), float(e_row["High"]), float(e_row["Low"]), float(e_row["atr14"])

        if cooldown > 0: cooldown -= 1

        if position is not None:
            hold_days += 1
            exit_price, exit_reason = None, None
            if high > position["trail_high"]: position["trail_high"] = high

            gain_pct = (position["trail_high"] - position["entry_price"]) / position["entry_price"]
            if position["confidence_stars"] >= 5 and gain_pct >= LEV_TP_PCT:
                trail_stop = position["trail_high"] * (1 - LEV_TRAIL_PCT)
                if trail_stop > position["stop"]: position["stop"] = trail_stop

            current_gain = (close - position["entry_price"]) / position["entry_price"]

            if position.get("is_breakout", False):
                trail_stop = position["trail_high"] * (1 - LEV_5STAR_BREAKOUT_TRAIL_PCT)
                if trail_stop > position["stop"]: position["stop"] = trail_stop
                if low <= position["stop"]:
                    exit_price, exit_reason = position["stop"], f"Trailing Stop (-{int(LEV_5STAR_BREAKOUT_TRAIL_PCT*100)}%)"
                elif hold_days >= LEV_5STAR_BREAKOUT_MAX_HOLD:
                    exit_price, exit_reason = close, f"Max Hold ({LEV_5STAR_BREAKOUT_MAX_HOLD}d)"
            elif position["confidence_stars"] >= 5:
                if low <= position["stop"]:
                    if gain_pct >= LEV_TP_PCT: exit_price, exit_reason = position["stop"], "Trailing Stop Cash-Out (>35%)"
                    else: exit_price, exit_reason = position["stop"], f"Stop-Loss (-{int(LEV_STOP_MIN_PCT*100)}%)"
                elif not pd.isna(e_row["rsi"]) and e_row["rsi"] > LEV_RSI_OB_EXIT:
                    exit_price, exit_reason = close, f"RSI Overbought (> {LEV_RSI_OB_EXIT})"
                elif hold_days >= LEV_MAX_HOLD:
                    exit_price, exit_reason = close, f"Max Hold ({LEV_MAX_HOLD}d)"
            else:
                if low <= position["stop"]:
                    exit_price, exit_reason = position["stop"], f"Stop-Loss (-{int(LEV_4STAR_STOP_PCT*100)}%)"
                elif high >= position["target"]:
                    exit_price, exit_reason = position["target"], f"Take-Profit (+{int(LEV_4STAR_TP_PCT*100)}%)"
                elif hold_days >= LEV_4STAR_MAX_HOLD:
                    exit_price, exit_reason = close, f"Max Hold ({LEV_4STAR_MAX_HOLD}d)"

            if exit_price is not None:
                entry, pos_size = position["entry_price"], position["trade_size"]
                pnl_pct = (exit_price - entry) / entry
                pnl_dollar = pnl_pct * pos_size - pos_size * COMMISSION * 2
                lev_trades.append({
                    "strategy_type":    "LEVERAGED",
                    "ticker":           exec_ticker,
                    "entry_date":       position["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price":      round(entry, 2),
                    "exit_date":        date.strftime("%Y-%m-%d"),
                    "exit_price":       round(exit_price, 2),
                    "exit_reason":      exit_reason,
                    "hold_days":        hold_days,
                    "pnl_pct":          pnl_pct,
                    "pnl_dollar":       pnl_dollar,
                    "strategies":       position["strategies"],
                    "confidence_stars": position["confidence_stars"],
                    "trade_size":       pos_size,
                    "is_5star":         position["is_5star"],
                })
                if exit_reason.startswith("Stop"): cooldown = SW_COOLDOWN_BARS
                position, hold_days = None, 0
                continue

        if position is None and not pd.isna(atr) and atr > 0 and cooldown == 0:
            ibs_block = (b_i == 0 or pd.isna(b_recs[b_i - 1]["ibs"]) or b_recs[b_i - 1]["ibs"] >= SW_IBS_MAX_ENTRY_5STAR)
            if not pd.isna(b_row["rsi"]) and b_row["rsi"] < SW_RSI_5STAR_ENTRY and not ibs_block:
                bb_lower_val = b_row.get("bb_lower", float("nan"))
                is_5star_max = not pd.isna(bb_lower_val) and b_row["Close"] <= float(bb_lower_val)
                stars = 6
                strat = f"Deep Oversold 5★ Blue Diamond ({base_ticker})"
                
                position = {
                    "entry_date": date, "entry_price": close, "stop": 0, "target": close * (1 + LEV_TP_PCT),
                    "trail_high": close, "strategies": strat, "confidence_stars": stars, "trade_size": _trade_size_5star(date),
                    "is_5star": True,
                }
                hold_days = 0
            else:
                p_b_row = b_recs[b_i-1] if b_i > 0 else b_row
                if not pd.isna(b_row["rsi"]) and not pd.isna(b_row["rel_vol"]) and b_row["rsi"] <= 30 and b_row["rel_vol"] >= SW_VOL_SPIKE_MIN:
                    prev_ibs = b_recs[b_i - 1]["ibs"] if b_i > 0 else float("nan")
                    if not pd.isna(prev_ibs) and prev_ibs < SW_IBS_MAX_ENTRY_4STAR:
                        atr_stop = close - SW_ATR_MULT * atr
                        pct_stop = close * (1 - LEV_STOP_MIN_PCT)
                        final_stop = min(atr_stop, pct_stop)
                        position = {
                            "entry_date": date, "entry_price": close, "stop": final_stop, "target": close * (1 + LEV_TP_PCT),
                            "trail_high": close, "strategies": f"Volume Capitulation Sweep 5★ Blue Diamond ({base_ticker})", "confidence_stars": 6,
                            "trade_size": _trade_size_5star(date), "is_5star": True,
                        }
                        hold_days = 0
                else:
                    if not pd.isna(b_row["ma200"]) and b_row["Close"] > b_row["ma200"]:
                        if not pd.isna(b_row["ma20"]) and not pd.isna(b_row["ma50"]):
                            if position is None:
                                if b_row["Close"] >= (b_row["high20"] * 0.99):
                                    if not pd.isna(b_row.get("rel_vol")) and b_row["rel_vol"] >= 1.2:
                                        position = {
                                            "entry_date":       date,
                                            "entry_price":      close,
                                            "stop":             close * (1 - LEV_5STAR_BREAKOUT_TRAIL_PCT),
                                            "target":           close * 2.0, 
                                            "trail_high":       close,
                                            "strategies":       f"Momentum Breakout 5★ ({base_ticker})",
                                            "confidence_stars": 5,
                                            "trade_size":       _trade_size_5star(date),
                                            "is_5star":         True,
                                            "is_breakout":      True,
                                        }
                                        hold_days = 0

all_trades.extend(lev_trades)

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 4 — SPY REGIME MOMENTUM (UPRO) LOOP
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 4: SPY REGIME strategy...")
regime_trades = []
if "SPY" in ticker_dfs and "UPRO" in ticker_dfs:
    spy_df  = ticker_dfs["SPY"]
    upro_df = ticker_dfs["UPRO"]
    m_cal   = spy_df.index[spy_df.index >= START_TS]
    
    spy_df["sma_rsi"] = _sma_rsi(spy_df["Close"])          # Engine 4 needs SMA RSI
    spy_df["ret20"]   = spy_df["Close"].pct_change(20)     # 20d momentum for confirmed bull
    
    spy_fast  = spy_df.to_dict('index')
    upro_fast = upro_df.to_dict('index')
    
    # Strategy A positions
    open_pos = {} # ticker -> pos_dict
    cooldown_until = None
    
    # Strategy B position
    bull_hold_pos = None
    bull_hold_cooldown_until = None
    ma50_breach_count = 0
    
    for date in m_cal:
        s_row = spy_fast.get(date)
        if s_row is None: continue
        
        # Check MA50 breach (5-day rule)
        if s_row['Close'] < s_row['ma50']:
            ma50_breach_count += 1
        else:
            ma50_breach_count = 0
        regime_exit_signal = (ma50_breach_count >= 5)

        # Check regime for entries
        confirmed_bull = False
        _ret20 = s_row.get("ret20", float("nan"))
        if not pd.isna(s_row["ma50"]) and not pd.isna(s_row["ma200"]) and not pd.isna(s_row.get("sma_rsi")) and not pd.isna(_ret20):
            if s_row["Close"] > s_row["ma50"] and s_row["ma50"] > s_row["ma200"]:  # golden cross
                if 50 <= s_row["sma_rsi"] <= 72 and _ret20 >= 0.02:               # momentum filter
                    confirmed_bull = True
        
        # Strategy A: Update open positions
        to_close = []
        for tkr, pos in open_pos.items():
            if date not in upro_fast: continue
            u_row    = upro_fast[date]
            u_close  = float(u_row["Close"])
            u_low    = float(u_row["Low"])
            
            pos["days_held"] += 1
            entry = pos["entry_price"]
            
            if u_close > pos["peak_price"]: pos["peak_price"] = u_close
            
            # Trailing stop trigger
            if (u_close - entry) / entry >= RG_UPRO_TRAIL_TRIGGER:
                pos["trail_active"] = True
            
            exit_price, exit_reason = None, None
            
            # Take profit
            if (u_close - entry) / entry >= RG_UPRO_TP_PCT:
                exit_price, exit_reason = u_close, "Take-Profit"
            # Trailing stop
            elif pos["trail_active"]:
                trail_stop = pos["peak_price"] * (1 - RG_UPRO_TRAIL_PCT)
                if u_close <= trail_stop:
                    exit_price, exit_reason = u_close, "Trailing-Stop"
            # Hard floor
            elif u_low <= entry * (1 - RG_UPRO_FLOOR_PCT):
                exit_price, exit_reason = entry * (1 - RG_UPRO_FLOOR_PCT), "Floor-Stop"
            # MA50 Breach exit (Consistent with standalone)
            elif regime_exit_signal:
                exit_price, exit_reason = u_close, "Regime-Exit"
            # Max hold
            elif pos["days_held"] >= RG_UPRO_MAX_HOLD:
                exit_price, exit_reason = u_close, "Max-Hold"
            
            if exit_price is not None:
                pnl_pct = (exit_price - entry) / entry
                size    = pos["trade_size"]
                regime_trades.append({
                    "strategy_type":    "REGIME",
                    "ticker":           tkr,
                    "entry_date":       pos["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price":      round(entry, 2),
                    "exit_date":        date.strftime("%Y-%m-%d"),
                    "exit_price":       round(exit_price, 2),
                    "exit_reason":      exit_reason,
                    "hold_days":        pos["days_held"],
                    "pnl_pct":          pnl_pct,
                    "pnl_dollar":       size * pnl_pct - 2 * COMMISSION * size,
                    "strategies":       "UPRO Pullback (Strategy A)",
                    "confidence_stars": 5,
                    "trade_size":       size,
                    "is_5star":         True,
                })
                to_close.append(tkr)
                cooldown_until = date + pd.Timedelta(days=2)
        
        for tkr in to_close: del open_pos[tkr]
        
        # Strategy B: Update Bull Hold
        if bull_hold_pos is not None:
            if date in upro_fast:
                u_row   = upro_fast[date]
                u_close = float(u_row["Close"])
                u_low   = float(u_row["Low"])
                
                bh_entry = bull_hold_pos["entry_price"]
                bull_hold_pos["days_held"] += 1
                
                if u_close > bull_hold_pos["peak_price"]: bull_hold_pos["peak_price"] = u_close
                if (u_close - bh_entry) / bh_entry >= RG_UPRO_TRAIL_TRIGGER:
                    bull_hold_pos["trail_active"] = True
                
                bh_exit_price, bh_exit_reason = None, None
                
                # Take profit (Churn)
                if (u_close - bh_entry) / bh_entry >= RG_BULL_TP_PCT:
                    bh_exit_price, bh_exit_reason = u_close, "BH-Target"
                # Trailing stop
                elif bull_hold_pos["trail_active"]:
                    ts = bull_hold_pos["peak_price"] * (1 - RG_UPRO_TRAIL_PCT)
                    if u_close <= ts:
                        bh_exit_price, bh_exit_reason = u_close, "BH-Trail"
                # Stop loss
                elif u_low <= bh_entry * (1 - RG_BULL_SL_PCT):
                    bh_exit_price, bh_exit_reason = bh_entry * (1 - RG_BULL_SL_PCT), "BH-Stop"
                # Regime exit
                elif regime_exit_signal:
                    bh_exit_price, bh_exit_reason = u_close, "BH-Regime"
                # Max hold (Churn velocity)
                elif bull_hold_pos["days_held"] >= RG_BULL_MAX_DAYS:
                    bh_exit_price, bh_exit_reason = u_close, "BH-Churn"
                
                if bh_exit_price is not None:
                    pnl_pct = (bh_exit_price - bh_entry) / bh_entry
                    size    = bull_hold_pos["trade_size"]
                    regime_trades.append({
                        "strategy_type":    "REGIME",
                        "ticker":           "UPRO",
                        "entry_date":       bull_hold_pos["entry_date"].strftime("%Y-%m-%d"),
                        "entry_price":      round(bh_entry, 2),
                        "exit_date":        date.strftime("%Y-%m-%d"),
                        "exit_price":       round(bh_exit_price, 2),
                        "exit_reason":      bh_exit_reason,
                        "hold_days":        bull_hold_pos["days_held"],
                        "pnl_pct":          pnl_pct,
                        "pnl_dollar":       size * pnl_pct - 2 * COMMISSION * size,
                        "strategies":       "UPRO Bull Hold (Strategy B)",
                        "confidence_stars": 5,
                        "trade_size":       size,
                        "is_5star":         True,
                    })
                    bull_hold_pos = None
                    bull_hold_cooldown_until = date + pd.Timedelta(days=1)

        # Entries
        if confirmed_bull and date in upro_fast:
            # Strategy A: Pullback
            if not open_pos and (cooldown_until is None or date > cooldown_until):
                spy_idx = spy_df.index.get_loc(date)
                if spy_idx >= 10:
                    # Use isolated SMA RSI for the pullback filter too
                    rsi_window = spy_df["sma_rsi"].iloc[spy_idx-10:spy_idx]
                    if rsi_window.min() <= RG_RSI_PULLBACK:
                        u_close = float(upro_fast[date]["Close"])
                        open_pos["UPRO"] = {
                            "entry_date": date, "entry_price": u_close, "peak_price": u_close,
                            "days_held": 0, "trail_active": False, "trade_size": _trade_size_5star(date)
                        }
            
            # Strategy B: Bull Hold Entry (same pullback filter as Strategy A)
            if bull_hold_pos is None and (bull_hold_cooldown_until is None or date > bull_hold_cooldown_until):
                spy_idx_bh = spy_df.index.get_loc(date)
                if spy_idx_bh >= 10:
                    rsi_window_bh = spy_df["sma_rsi"].iloc[spy_idx_bh - 10: spy_idx_bh]
                    if rsi_window_bh.min() <= RG_RSI_PULLBACK:
                        u_close = float(upro_fast[date]["Close"])
                        bull_hold_pos = {
                            "entry_date": date, "entry_price": u_close, "peak_price": u_close,
                            "days_held": 0, "trail_active": False, "trade_size": _trade_size_5star(date)
                        }

all_trades.extend(regime_trades)

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 5 — SECTOR HUNTER LOOP
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 5: SECTOR HUNTER strategy...")
sector_trades = []

for _e5_ticker in SW_TICKERS:
    if _e5_ticker not in ticker_dfs: continue
    _e5_sector = E5_TICKER_SECTOR.get(_e5_ticker)
    if not _e5_sector or _e5_sector not in E5_SECTOR_ETF_MAP: continue
    _e5_etf = E5_SECTOR_ETF_MAP[_e5_sector]

    _e5_df      = ticker_dfs[_e5_ticker]
    _e5_dates   = _e5_df.index.tolist()
    _e5_records = _e5_df.to_dict("records")

    _e5_pos, _e5_hold, _e5_cool, _e5_consec = None, 0, 0, 0

    for _e5_i, _e5_date in enumerate(_e5_dates):
        if _e5_date < START_TS: continue

        _e5_row   = _e5_records[_e5_i]
        _e5_close = float(_e5_row["Close"])
        _e5_high  = float(_e5_row["High"])
        _e5_low   = float(_e5_row["Low"])

        if _e5_cool > 0: _e5_cool -= 1

        # ── Exit logic ─────────────────────────────────────────────────────
        if _e5_pos is not None:
            _e5_hold += 1
            if _e5_close > _e5_pos["trail_high"]: _e5_pos["trail_high"] = _e5_close
            _e5_entry     = _e5_pos["entry_price"]
            _e5_peak_gain = (_e5_pos["trail_high"] - _e5_entry) / _e5_entry
            _e5_trail_on  = _e5_peak_gain >= E5_TRAIL_TRIGGER
            _e5_trail_stp = _e5_pos["trail_high"] * (1 - E5_TRAIL_PCT) if _e5_trail_on else None

            _e5_xp, _e5_xr = None, None
            if not _e5_trail_on and _e5_low <= _e5_entry * (1 - E5_SL_PCT):
                _e5_xp, _e5_xr = _e5_entry * (1 - E5_SL_PCT), f"Stop-Loss (-{int(E5_SL_PCT*100)}%)"
            elif _e5_high >= _e5_entry * (1 + E5_TP_PCT):
                _e5_xp, _e5_xr = _e5_entry * (1 + E5_TP_PCT), f"Take-Profit (+{int(E5_TP_PCT*100)}%)"
            elif _e5_trail_on and _e5_hold > 1 and _e5_close <= _e5_trail_stp:
                _e5_xp, _e5_xr = _e5_close, "Trailing Stop"
            elif E5_STALE_CUT_DAYS > 0 and _e5_hold >= E5_STALE_CUT_DAYS and _e5_close < _e5_entry * 0.97:
                _e5_xp, _e5_xr = _e5_close, f"Stale Cut ({E5_STALE_CUT_DAYS}d)"
            elif _e5_hold >= E5_MAX_HOLD:
                _e5_xp, _e5_xr = _e5_close, f"Max Hold ({E5_MAX_HOLD}d)"

            if _e5_xp is not None:
                _e5_pnl_pct = (_e5_xp - _e5_entry) / _e5_entry
                _e5_size    = _e5_pos["trade_size"]
                _e5_pnl_dol = _e5_pnl_pct * _e5_size - _e5_size * COMMISSION * 2
                _e5_stars   = _e5_pos["confidence_stars"]
                sector_trades.append({
                    "strategy_type":    "SECTOR",
                    "ticker":           _e5_ticker,
                    "entry_date":       _e5_pos["entry_date"].strftime("%Y-%m-%d"),
                    "entry_price":      round(_e5_entry, 2),
                    "exit_date":        _e5_date.strftime("%Y-%m-%d"),
                    "exit_price":       round(_e5_xp, 2),
                    "exit_reason":      _e5_xr,
                    "hold_days":        _e5_hold,
                    "pnl_pct":          round(_e5_pnl_pct, 4),
                    "pnl_dollar":       round(_e5_pnl_dol, 2),
                    "strategies":       f"Sector Hunter ({_e5_sector})",
                    "confidence_stars": _e5_stars,
                    "trade_size":       _e5_size,
                    "is_5star":         _e5_stars >= 5,
                })
                if "Stop-Loss" in _e5_xr: _e5_cool = E5_COOLDOWN_BARS
                if _e5_pnl_dol < 0:
                    _e5_consec += 1
                    if _e5_consec >= E5_STOCK_LOSS_STREAK:
                        _e5_cool = max(_e5_cool, E5_STOCK_LOSS_COOLDOWN)
                        _e5_consec = 0
                else:
                    _e5_consec = 0
                _e5_pos, _e5_hold = None, 0
                continue

        # ── Ladder buy (add to winner on MA20 pullback) ────────────────────
        if _e5_pos is not None and not _e5_pos.get("ladder_done"):
            _ma20 = _e5_row.get("ma20", float("nan"))
            _rsi  = _e5_row.get("rsi",  float("nan"))
            if (not pd.isna(_ma20) and not pd.isna(_rsi)
                    and _e5_close > _e5_pos["entry_price"]
                    and _e5_close <= float(_ma20) * (1 + E5_LADDER_TRIGGER)
                    and _e5_close >= float(_ma20)
                    and _rsi < E5_LADDER_RSI_MAX):
                _add = _trade_size(_e5_date)
                _old_sz, _old_ep = _e5_pos["trade_size"], _e5_pos["entry_price"]
                _e5_pos["entry_price"] = (_old_ep * _old_sz + _e5_close * _add) / (_old_sz + _add)
                _e5_pos["trade_size"]  = _old_sz + _add
                _e5_pos["ladder_done"] = True

        # ── Entry logic ────────────────────────────────────────────────────
        if _e5_pos is not None or _e5_cool > 0: continue
        if _e5_i < 2: continue
        if _e5_sector in E5_EXCLUDED_SECTORS: continue

        # Regime gate: SPY above 200-day MA
        if "SPY" in ticker_dfs and _e5_date in ticker_dfs["SPY"].index:
            _spy_rec = ticker_dfs["SPY"].loc[_e5_date]
            _spy_ma200 = _spy_rec.get("ma200", float("nan"))
            if pd.isna(_spy_ma200) or float(_spy_rec["Close"]) < float(_spy_ma200):
                continue

        # Layer 1+2: sector must be hot today
        if _e5_etf not in hot_sectors_by_date.get(_e5_date, set()): continue

        # Valid indicators
        if any(pd.isna(_e5_row.get(c, float("nan"))) for c in ("rsi", "macd_hist", "ma20", "ma50")): continue

        # Layer 3a: near MA20/MA50 support (5% buffer)
        if _e5_close < float(_e5_row["ma20"]) * 0.95: continue
        if _e5_close < float(_e5_row["ma50"]) * 0.95: continue

        # Layer 3b: oversold
        if _e5_row["rsi"] >= E5_ENTRY_RSI_MAX: continue

        # Layer 3c: two consecutive green closes
        _e5_prev  = _e5_records[_e5_i - 1]
        _e5_prev2 = _e5_records[_e5_i - 2]
        if not (_e5_close > _e5_prev["Close"] and _e5_prev["Close"] > _e5_prev2["Close"]): continue

        _e5_s    = E5_SECTOR_STARS.get(_e5_sector, 4)
        _e5_mult = 1.3 if _e5_s >= 5 else 1.0
        _e5_pos  = {
            "entry_date":       _e5_date,
            "entry_price":      _e5_close,
            "trail_high":       _e5_close,
            "trade_size":       round(_trade_size(_e5_date) * _e5_mult),
            "confidence_stars": _e5_s,
            "ladder_done":      False,
        }
        _e5_hold = 0

# ── Global loss-streak cooldown (second pass, chronologically accurate) ───────
if E5_GLOBAL_LOSS_STREAK > 0 and sector_trades:
    _e5_sorted        = sorted(sector_trades, key=lambda t: t["exit_date"])
    _e5_g_consec      = 0
    _e5_g_cooldown    = pd.Timestamp.min
    _e5_kept          = []
    for _t in _e5_sorted:
        _entry_dt = pd.Timestamp(_t["entry_date"])
        _exit_dt  = pd.Timestamp(_t["exit_date"])
        if _entry_dt <= _e5_g_cooldown: continue
        _e5_kept.append(_t)
        if _t["pnl_dollar"] < 0:
            _e5_g_consec += 1
            if _e5_g_consec >= E5_GLOBAL_LOSS_STREAK:
                _e5_g_cooldown = _exit_dt + pd.offsets.BDay(E5_GLOBAL_COOLDOWN_DAYS)
                _e5_g_consec = 0
        else:
            _e5_g_consec = 0
    _e5_removed = len(sector_trades) - len(_e5_kept)
    if _e5_removed:
        print(f"  Global cooldown filter: removed {_e5_removed} trades entered during loss-streak pauses")
    sector_trades = _e5_kept

all_trades.extend(sector_trades)

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 6 — RANGE REVERSION LOOP
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 6: RANGE REVERSION strategy...")
chop_trades = []

# Build chronological date list across all E6 tickers
_e6_date_set = set()
for _t in e6_ticker_dfs:
    _e6_date_set.update(e6_ticker_dfs[_t].index)
e6_all_dates = sorted(d for d in _e6_date_set if d >= START_TS)

e6_open_pos = {}   # {ticker: pos_dict}
e6_cooldown = {}   # {ticker: bars_remaining}

# Portfolio-level loss freeze state
e6_consec_losses      = 0
e6_no_win_since_freeze = False
e6_freeze_until        = None
e6_cool_triggers       = 0
e6_spy_below_50_streak = 0   # consecutive days SPY closed below 50 SMA
e6_spy_above_50_streak = 0   # consecutive days SPY closed above 50 SMA (for deactivation)
e6_spy_gated           = False  # True once streak >= 2

_e6_tier_mult = {1: TIER1_SIZE_MULT, 2: TIER2_SIZE_MULT, 3: TIER3_SIZE_MULT}

for _e6_date in e6_all_dates:
    # Decrement per-ticker cooldowns
    for _tk in list(e6_cooldown):
        if e6_cooldown[_tk] > 0:
            e6_cooldown[_tk] -= 1

    # ── Exit phase ──────────────────────────────────────────────────────────
    _to_close = []
    for _tk, _pos in e6_open_pos.items():
        _idf = e6_ticker_dfs.get(_tk)
        if _idf is None or _e6_date not in _idf.index:
            continue
        _row   = _idf.loc[_e6_date]
        _close = float(_row["Close"])
        _high  = float(_row["High"])
        _low   = float(_row["Low"])

        _pos["hold"] += 1
        if _close > _pos["peak_price"]:
            _pos["peak_price"] = _close
        if not _pos["trail_active"]:
            if (_pos["peak_price"] - _pos["entry_price"]) / _pos["entry_price"] >= E6_TRAIL_ACTIVATE_PCT:
                _pos["trail_active"] = True

        _xp, _xr = None, None
        if _low <= _pos["stop"]:
            _xp, _xr = _pos["stop"], "Stop-Loss"
        elif _high >= _pos["target"]:
            _xp, _xr = _pos["target"], "Take-Profit"
        elif _pos["trail_active"]:
            _trail = _pos["peak_price"] * (1 - E6_TRAIL_DISTANCE_PCT)
            if _low <= _trail:
                _xp, _xr = _trail, "Trailing Stop"
        if _xp is None and _pos["hold"] >= E6_STALE_CUT_DAYS:
            _xp, _xr = _close, f"Stale Cut ({E6_STALE_CUT_DAYS}d)"
        if _xp is None and _pos["hold"] >= E6_MAX_HOLD:
            _xp, _xr = _close, f"Max Hold ({E6_MAX_HOLD}d)"

        if _xp is not None:
            _pnl_pct = (_xp - _pos["entry_price"]) / _pos["entry_price"]
            _pnl_dol = _pnl_pct * _pos["size"] - 2 * COMMISSION * _pos["size"]
            chop_trades.append({
                "strategy_type":    "CHOP",
                "ticker":           _tk,
                "entry_date":       _pos["entry_date"].strftime("%Y-%m-%d"),
                "entry_price":      round(_pos["entry_price"], 2),
                "exit_date":        _e6_date.strftime("%Y-%m-%d"),
                "exit_price":       round(_xp, 2),
                "exit_reason":      _xr,
                "hold_days":        _pos["hold"],
                "pnl_pct":          round(_pnl_pct, 4),
                "pnl_dollar":       round(_pnl_dol, 2),
                "strategies":       f"Range Reversion E6 (T{_pos['tier']})",
                "confidence_stars": None,
                "trade_size":       _pos["size"],
                "is_5star":         False,
            })
            _to_close.append(_tk)
            if "Stop" in _xr:
                e6_cooldown[_tk] = E6_COOLDOWN_BARS
            # Portfolio loss tracking
            if _pnl_dol < 0:
                e6_consec_losses += 1
                limit = E6_CONSEC_LOSS_LIMIT2 if e6_no_win_since_freeze else E6_CONSEC_LOSS_LIMIT
                days  = E6_LOSS_COOLDOWN_DAYS2 if e6_no_win_since_freeze else E6_LOSS_COOLDOWN_DAYS
                if e6_consec_losses >= limit:
                    e6_freeze_until        = _e6_date + pd.Timedelta(days=days)
                    e6_no_win_since_freeze = True
                    e6_consec_losses       = 0
                    e6_cool_triggers      += 1
            else:
                e6_consec_losses       = 0
                e6_no_win_since_freeze = False

    for _tk in _to_close:
        del e6_open_pos[_tk]

    # ── Entry phase ─────────────────────────────────────────────────────────
    if e6_freeze_until is not None and _e6_date <= e6_freeze_until:
        continue

    # SPY 50 SMA gate — activates after 2 consecutive closes below 50 SMA, lifts after 2 consecutive closes above
    if "SPY" in ticker_dfs and _e6_date in ticker_dfs["SPY"].index:
        _spy_r  = ticker_dfs["SPY"].loc[_e6_date]
        _ma50   = _spy_r.get("ma50", float("nan"))
        if not pd.isna(_ma50):
            if float(_spy_r["Close"]) < float(_ma50):
                e6_spy_below_50_streak += 1
                e6_spy_above_50_streak  = 0
                if e6_spy_below_50_streak >= 2:
                    e6_spy_gated = True
            else:
                e6_spy_above_50_streak += 1
                e6_spy_below_50_streak  = 0
                if e6_spy_above_50_streak >= 2:
                    e6_spy_gated = False
    if e6_spy_gated:
        continue

    for _tk in SW_TICKERS:
        if _tk in e6_open_pos or e6_cooldown.get(_tk, 0) > 0:
            continue
        _idf = e6_ticker_dfs.get(_tk)
        if _idf is None or _e6_date not in _idf.index:
            continue
        _row = _idf.loc[_e6_date]
        _req = ("bb_lower", "adx", "adx_consec", "range_consec", "rel_vol",
                "recent_below_bb", "max_dip_pct", "min_close_lookback", "rsi")
        if any(pd.isna(_row.get(c, float("nan"))) for c in _req):
            continue

        if _row["adx_consec"] < E6_CHOP_BARS:   continue
        if _row["range_consec"] < E6_RANGE_BARS: continue
        if _row["adx"] < E6_ADX_MIN or _row["adx"] > E6_ADX_MAX: continue
        if _row["rsi"] < E6_RSI_MIN or _row["rsi"] > E6_RSI_MAX: continue
        if _row["recent_below_bb"] != 1: continue

        _dip   = float(_row["max_dip_pct"])
        _close = float(_row["Close"])
        _bb_lo = float(_row["bb_lower"])
        _min_c = float(_row["min_close_lookback"])

        _path_a = (E6_DIP_MIN_PCT <= _dip < E6_DIP_AVOID_LOW) and (_close >= _bb_lo)
        _path_b = (_dip >= E6_DIP_AVOID_HIGH) and (_close >= _min_c * (1 + E6_DEEP_REBOUND_PCT))
        _rec_req = E6_SHALLOW_RECOVER_PCT if _dip < E6_DIP_MIN_PCT else E6_STRONG_RECOVER_PCT
        _path_c = (_dip > 0) and (_close >= _bb_lo * (1 + _rec_req))

        if not (_path_a or _path_b or _path_c): continue
        if 0.07 <= _dip < E6_DIP_AVOID_HIGH and _close < _bb_lo * 1.02: continue
        if _row["rel_vol"] < E6_VOL_SPIKE_MIN: continue

        _adx_v   = float(_row["adx"])
        _rsi_v   = float(_row["rsi"])
        _vol_v   = float(_row["rel_vol"])
        _recov   = (_close / _bb_lo - 1) * 100

        _is_t1 = (
            (5.0 <= _recov < 8.0)                          or
            (35 <= _rsi_v < 45)                            or
            (15 <= _adx_v < 18)                            or
            _path_a                                        or
            (10 <= _adx_v < 15)                            or
            (15 <= _adx_v < 18 and 50 <= _rsi_v < 65)
        )
        _is_t2 = (not _is_t1) and (
            (1.5 <= _vol_v < 2.0)                          or
            (55 <= _rsi_v < 65)                            or
            (2.0 <= _vol_v < 3.0)                          or
            (18 <= _adx_v < 22 and 35 <= _rsi_v < 50)
        )
        _tier = 1 if _is_t1 else (2 if _is_t2 else 3)
        _size = round(_trade_size(_e6_date) * _e6_tier_mult[_tier])

        e6_open_pos[_tk] = {
            "entry_price":  _close,
            "stop":         round(_close * (1 - E6_STOP_PCT), 4),
            "target":       round(_close * (1 + E6_TP_PCT), 4),
            "entry_date":   _e6_date,
            "tier":         _tier,
            "size":         _size,
            "hold":         0,
            "peak_price":   _close,
            "trail_active": False,
        }

all_trades.extend(chop_trades)
print(f"  Engine 6 complete: {len(chop_trades)} trades ({e6_cool_triggers} portfolio freezes)\n")

# ═══════════════════════════════════════════════════════════════════════════════
# ENGINE 7 — DOUBLE BOTTOM W2 ANTICIPATION LOOP
# ═══════════════════════════════════════════════════════════════════════════════
print("Running ENGINE 7: DOUBLE BOTTOM (W2 Anticipation)...")
e7_trades    = []
e7_open_pos  = {}
e7_pending   = {}
e7_watching  = {}
e7_entered   = set()

_e7_spy_ma50  = ticker_dfs["SPY"]["ma50"] if "SPY" in ticker_dfs else pd.Series(dtype=float)
_e7_dates     = [d for d in ticker_dfs["SPY"].index if d >= START_TS] if "SPY" in ticker_dfs else []

for _e7_date in _e7_dates:
    if _e7_date not in _e7_spy_ma50.index or pd.isna(_e7_spy_ma50.loc[_e7_date]):
        continue
    _e7_spy_close   = float(ticker_dfs["SPY"].loc[_e7_date, "Close"])
    _e7_spy_above50 = _e7_spy_close > float(_e7_spy_ma50.loc[_e7_date])

    # ── Fill pending entries at today's open ──────────────────────────────
    _e7_filled = []
    for _tk, _pend in e7_pending.items():
        if _tk in e7_open_pos:
            _e7_filled.append(_tk); continue
        if _tk not in ticker_dfs or _e7_date not in ticker_dfs[_tk].index:
            _e7_filled.append(_tk); continue
        _op = float(ticker_dfs[_tk].loc[_e7_date, "Open"])
        e7_open_pos[_tk] = {
            "entry_date":   _e7_date,
            "entry_price":  _op,
            "pivot_price":  _pend["pivot_price"],
            "neckline":     _pend["neckline"],
            "w1_depth":     _pend["w1_depth"],
            "w1_velocity":  _pend["w1_velocity"],
            "size":         _pend["size"],
            "hold_days":    0,
            "peak_price":   _op,
            "trail_active": False,
        }
        _e7_filled.append(_tk)
    for _tk in _e7_filled:
        e7_pending.pop(_tk, None)

    # ── Confirming bar check for watched setups ────────────────────────────
    _e7_confirmed = []
    for _tk, _watch in e7_watching.items():
        if _tk in e7_open_pos or _tk in e7_pending:
            _e7_confirmed.append(_tk); continue
        if _tk not in ticker_dfs or _e7_date not in ticker_dfs[_tk].index:
            _e7_confirmed.append(_tk); continue
        _row   = ticker_dfs[_tk].loc[_e7_date]
        _cl    = float(_row["Close"]); _op = float(_row["Open"])
        _lo    = float(_row["Low"]);   _vo = float(_row.get("Volume", 0) or 0)
        _vavg  = float(_row["vol_avg20"]) if not pd.isna(_row.get("vol_avg20", float("nan"))) else 0

        if _lo < _watch["w2_price"] * (1 - E7_STOP_PCT):
            _e7_confirmed.append(_tk); continue
        if _cl > _watch["w2_price"] * (1 + E7_ZONE_CANCEL_PCT):
            _e7_confirmed.append(_tk); continue
        if _lo <= _watch["w2_price"] * (1 + E7_TOUCH_PCT):
            _watch["touched"] = True

        _body       = (_cl - _op) / _op if _op > 0 else 0
        _vol_std    = (_vo >= _vavg * E7_VOL_MULT)       if _vavg > 0 else True
        _vol_relax  = (_vo >= _vavg * E7_VOL_MULT_RELAX) if _vavg > 0 else True
        _std_trig   = _body >= E7_BAR_BODY_MIN   and _vol_std
        _str_candle = _body >= E7_BAR_BODY_STRONG and _vol_relax

        if _watch["touched"] and _cl > _op and _cl > _watch["w2_price"] and (_std_trig or _str_candle):
            e7_pending[_tk] = {
                "pivot_price": _watch["w2_price"],
                "neckline":    _watch["neckline"],
                "w1_depth":    _watch["w1_depth"],
                "w1_velocity": _watch["w1_velocity"],
                "size":        _trade_size(_e7_date),
            }
            _e7_confirmed.append(_tk)
        else:
            _watch["bars_left"] -= 1
            if _watch["bars_left"] <= 0:
                _e7_confirmed.append(_tk)
    for _tk in _e7_confirmed:
        e7_watching.pop(_tk, None)

    # ── Exit open positions ────────────────────────────────────────────────
    _e7_close = []
    for _tk, _pos in e7_open_pos.items():
        if _tk not in ticker_dfs or _e7_date not in ticker_dfs[_tk].index:
            continue
        _row  = ticker_dfs[_tk].loc[_e7_date]
        _cl   = float(_row["Close"]); _hi = float(_row["High"]); _lo = float(_row["Low"])
        _pos["hold_days"] += 1
        _xr, _xp = None, _cl

        _prev_peak = _pos["peak_price"]
        _pos["peak_price"] = max(_pos["peak_price"], _hi)
        _entry = _pos["entry_price"]
        _stop  = _pos["pivot_price"] * (1 - E7_STOP_PCT)

        if _lo <= _stop:
            _xr, _xp = "Stop", _stop
        else:
            if _cl >= _pos["neckline"]:
                _pos["trail_active"] = True
            if _pos["trail_active"]:
                _trail = _pos["peak_price"] * (1 - E7_TRAIL_PCT)
                if _lo <= _trail:
                    _xr, _xp = "Trail Stop", _trail
            if not _xr:
                if _cl >= _entry * (1 + E7_STALL_GAIN_PCT):
                    _pos["hit_gain"] = True
                if _pos.get("hit_gain"):
                    if _pos["peak_price"] > _prev_peak:
                        _pos["bars_no_new_high"] = 0
                    else:
                        _pos["bars_no_new_high"] = _pos.get("bars_no_new_high", 0) + 1
                    if _pos["bars_no_new_high"] >= E7_STALL_BARS:
                        _stall_fl = _pos["peak_price"] * (1 - E7_STALL_TRAIL_PCT)
                        if _lo <= _stall_fl:
                            _xr, _xp = "Stall Trail", _stall_fl
        if not _xr and _pos["hold_days"] >= E7_STALE_DAYS and _cl < _entry:
            _xr, _xp = "Stale", _cl

        if _xr:
            _pnl_pct = (_xp - _entry) / _entry
            _pnl_dol = _pnl_pct * _pos["size"] - 2 * COMMISSION * _pos["size"]
            _e7_d    = _pos.get("w1_depth", 0)
            _e7_v    = _pos.get("w1_velocity", 0)
            _e7_tier1 = (_e7_d >= 0.20) or (0.010 <= _e7_v <= 0.015)
            _e7_stars = 6 if _e7_tier1 else 5
            e7_trades.append({
                "strategy_type":    "DBLBOT",
                "ticker":           _tk,
                "entry_date":       _pos["entry_date"].strftime("%Y-%m-%d"),
                "entry_price":      round(_entry, 2),
                "exit_date":        _e7_date.strftime("%Y-%m-%d"),
                "exit_price":       round(_xp, 2),
                "exit_reason":      _xr,
                "hold_days":        _pos["hold_days"],
                "pnl_pct":          round(_pnl_pct, 4),
                "pnl_dollar":       round(_pnl_dol, 2),
                "strategies":       "Double Bottom W2 (E7)",
                "confidence_stars": _e7_stars,
                "trade_size":       _pos["size"],
                "is_5star":         _e7_tier1,
            })
            _e7_close.append(_tk)
    for _tk in _e7_close:
        del e7_open_pos[_tk]

    # ── Scan for new setups (entry only when SPY > MA50) ──────────────────
    if not _e7_spy_above50:
        continue

    for _tk in SW_TICKERS:
        if _tk not in ticker_dfs or _e7_date not in ticker_dfs[_tk].index:
            continue
        if _tk in e7_open_pos or _tk in e7_pending or _tk in e7_watching:
            continue
        _df  = ticker_dfs[_tk]
        _idx = _df.index.get_loc(_e7_date)
        if _idx < E7_LOOKBACK + E7_ORDER_W1 + 10:
            continue

        _cl      = float(_df.loc[_e7_date, "Close"])
        _slice   = _df.iloc[max(0, _idx - E7_LOOKBACK): _idx]
        _n       = len(_slice)
        _lows    = _slice["Low"].values
        _closes  = _slice["Close"].values
        _highs   = _slice["High"].values

        _mins = argrelextrema(_lows, np.less, order=E7_ORDER_W1)[0]
        _mins = [i for i in _mins if i <= _n - E7_MIN_SEP - 1]

        for _wi in reversed(_mins):
            _w1p = float(_lows[_wi])
            if _cl > _w1p * (1 + E7_ZONE_PCT):   continue
            if _cl < _w1p * (1 - E7_STOP_PCT):   continue

            _neck = float(_closes[_wi:].max())
            if _neck < _w1p * (1 + E7_NECKLINE_MIN): continue

            _pre_hi = _highs[max(0, _wi - E7_DEPTH_WINDOW): _wi]
            if len(_pre_hi) == 0: continue
            _hi_idx  = int(np.argmax(_pre_hi))
            _pr_hi   = float(_pre_hi[_hi_idx])
            _depth   = _pr_hi / _w1p - 1
            _bars_dw = max(1, len(_pre_hi) - _hi_idx)
            _vel     = _depth / _bars_dw

            if _depth < E7_DEPTH_MIN:                        continue
            if _vel < E7_VELOCITY_MIN or _vel > E7_VELOCITY_MAX: continue

            _w1_date = _slice.index[_wi]
            _key = (_tk, _w1_date)
            if _key in e7_entered: continue

            e7_entered.add(_key)
            e7_watching[_tk] = {
                "w2_price":   _w1p,
                "neckline":   _neck,
                "bars_left":  E7_CONFIRM_BARS,
                "key":        _key,
                "touched":    False,
                "w1_depth":   _depth,
                "w1_velocity": _vel,
            }
            break

all_trades.extend(e7_trades)
print(f"  Engine 7 complete: {len(e7_trades)} trades\n")

# ═══════════════════════════════════════════════════════════════════════════════
# COMBINED RESULTS
# ═══════════════════════════════════════════════════════════════════════════════
if not all_trades:
    print("No trades generated.")
    raise SystemExit(0)

df_all = pd.DataFrame(all_trades).sort_values("exit_date").reset_index(drop=True)
df_sw  = df_all[df_all["strategy_type"] == "SWING"]
df_ix  = df_all[df_all["strategy_type"] == "INDEX"]
df_lv  = df_all[df_all["strategy_type"] == "LEVERAGED"]
df_mo  = df_all[df_all["strategy_type"] == "MOMENTUM"]
df_rg  = df_all[df_all["strategy_type"] == "REGIME"]
df_e5  = df_all[df_all["strategy_type"] == "SECTOR"]
df_e6  = df_all[df_all["strategy_type"] == "CHOP"]
df_e7  = df_all[df_all["strategy_type"] == "DBLBOT"]

def _stats(df):
    if len(df) == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0, "total_pnl": 0, "pf": 0, "avg_win": 0, "avg_loss": 0, "avg_hold": 0}
    wins, losses = df[df["pnl_dollar"] > 0], df[df["pnl_dollar"] <= 0]
    gw, gl = wins["pnl_dollar"].sum(), abs(losses["pnl_dollar"].sum())
    return {
        "trades": len(df), "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / len(df) * 100,
        "total_pnl": df["pnl_dollar"].sum(), "pf": gw / gl if gl > 0 else float("inf"),
        "avg_win": wins["pnl_dollar"].mean() if len(wins) > 0 else 0, "avg_loss": losses["pnl_dollar"].mean() if len(losses) > 0 else 0,
        "avg_hold": df["hold_days"].mean(),
    }

cs, ss, xs, ls, rs, e5s, e6s, e7s = _stats(df_all), _stats(df_sw), _stats(df_ix), _stats(df_lv), _stats(df_rg), _stats(df_e5), _stats(df_e6), _stats(df_e7)

# ── Per-star breakdown (across all trades that have confidence_stars) ──────────
star_stats = {}
for stars in sorted(df_all["confidence_stars"].dropna().unique()):
    df_star = df_all[df_all["confidence_stars"] == stars]
    star_stats[int(stars)] = _stats(df_star)

def _infer_tier(t):
    """Map any trade to tier 1/2/3 using confidence_stars (E1-E5) or strategies string (E6)."""
    stars = t.get("confidence_stars")
    if stars == 6:   return 1
    if stars == 5:   return 2
    if stars == 4:   return 3
    if t.get("strategy_type") == "CHOP":
        m = re.search(r'\(T(\d)\)', t.get("strategies", ""))
        return int(m.group(1)) if m else None
    return None

by_year = {}
for _, t in df_all.iterrows():
    yr = t["entry_date"][:4]
    if yr not in by_year:
        by_year[yr] = {"pnl": 0, "trades": 0, "wins": 0,
                       "sw_pnl": 0, "ix_pnl": 0, "lv_pnl": 0, "rg_pnl": 0, "e5_pnl": 0, "e6_pnl": 0, "e7_pnl": 0,
                       "t1_pnl": 0, "t1_trades": 0, "t1_wins": 0,
                       "t2_pnl": 0, "t2_trades": 0, "t2_wins": 0}
    by_year[yr]["pnl"] += t["pnl_dollar"]
    by_year[yr]["trades"] += 1
    by_year[yr]["wins"] += 1 if t["pnl_dollar"] > 0 else 0
    if t["strategy_type"] == "SWING":       by_year[yr]["sw_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "INDEX":     by_year[yr]["ix_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "LEVERAGED": by_year[yr]["lv_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "REGIME":    by_year[yr]["rg_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "SECTOR":    by_year[yr]["e5_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "CHOP":      by_year[yr]["e6_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "DBLBOT":    by_year[yr]["e7_pnl"] += t["pnl_dollar"]
    _t = _infer_tier(t)
    if _t == 1:
        by_year[yr]["t1_pnl"] += t["pnl_dollar"]
        by_year[yr]["t1_trades"] += 1
        by_year[yr]["t1_wins"] += 1 if t["pnl_dollar"] > 0 else 0
    elif _t == 2:
        by_year[yr]["t2_pnl"] += t["pnl_dollar"]
        by_year[yr]["t2_trades"] += 1
        by_year[yr]["t2_wins"] += 1 if t["pnl_dollar"] > 0 else 0

balance = float(STARTING_BALANCE)
sim_rows = []
for yr in sorted(by_year.keys()):
    size = TRADE_SIZE + TRADE_SIZE_INCREMENT * max(0, int(yr) - int(START_DATE[:4]))
    yd = by_year[yr]
    prev_bal = balance
    balance += yd["pnl"]
    sim_rows.append({
        "year": yr, "size": size, "trades": yd["trades"], "wins": yd["wins"], "yr_pnl": yd["pnl"],
        "sw_pnl": yd["sw_pnl"], "ix_pnl": yd["ix_pnl"], "lv_pnl": yd["lv_pnl"], "rg_pnl": yd["rg_pnl"],
        "e5_pnl": yd["e5_pnl"], "e6_pnl": yd["e6_pnl"], "e7_pnl": yd["e7_pnl"],
        "balance": balance, "yr_return": yd["pnl"] / prev_bal * 100 if prev_bal > 0 else 0,
    })

years_count = (pd.Timestamp(END_DATE) - pd.Timestamp(START_DATE)).days / 365.25
cagr = ((balance / STARTING_BALANCE) ** (1 / years_count) - 1) * 100 if years_count > 0 else 0
cumulative = STARTING_BALANCE + df_all["pnl_dollar"].cumsum()
rolling_max = cumulative.cummax()
drawdown_pct = ((cumulative - rolling_max) / rolling_max * 100).min()
drawdown_dol = (cumulative - rolling_max).min()

yearly_returns = [r["yr_return"] for r in sim_rows]
mean_ret = np.mean(yearly_returns) if yearly_returns else 0
std_ret = np.std(yearly_returns, ddof=1) if len(yearly_returns) > 1 else 1
sharpe = mean_ret / std_ret if std_ret > 0 else 0

RUN_TAG = "MASTER_UNIFIED_REPORT"
OUT_FILE = os.path.join(OUT_DIR, f"master_backtest_{START_DATE}_{END_DATE}_{RUN_TAG}.txt")

lines = [
    f"{'='*100}",
    "  MASTER 7-ENGINE UNIFIED BACKTEST REPORT",
    f"  Period: {START_DATE}  →  {END_DATE}",
    f"{'-'*100}",
    f"  Total Trades    : {cs['trades']}  (SWING: {ss['trades']} | INDEX: {xs['trades']} | LEVERAGED: {ls['trades']} | REGIME: {rs['trades']} | SECTOR: {e5s['trades']} | CHOP E6: {e6s['trades']} | DBLBOT E7: {e7s['trades']})",
    f"  Winning Trades  : {cs['wins']}",
    f"  Win Rate        : {cs['win_rate']:.1f}%",
    f"  Total P&L       : ${cs['total_pnl']:+,.2f}",
    f"  Profit Factor   : {cs['pf']:.2f}",
    f"{'-'*100}",
    f"  CAGR            : {cagr:+.2f}%  (${STARTING_BALANCE:,} → ${balance:,.0f} over {years_count:.1f} yrs)",
    f"  Max Drawdown    : {drawdown_pct:.1f}%  (${drawdown_dol:,.2f})",
    f"  Sharpe Ratio    : {sharpe:.2f}  (annualized)",
    f"{'='*100}",
    "\nPER-STRATEGY SUMMARY",
    f"{'-'*100}",
    f"  STRATEGY       TRADES   WIN%    PF        P&L        AVG WIN     AVG LOSS",
    f"  SWING          {ss['trades']:6d}  {ss['win_rate']:5.1f}%  {ss['pf']:5.2f}    ${ss['total_pnl']:+9,.0f}  ${ss['avg_win']:+9,.0f}  ${ss['avg_loss']:+9,.0f}",
    f"  INDEX FADE     {xs['trades']:6d}  {xs['win_rate']:5.1f}%  {xs['pf']:5.2f}    ${xs['total_pnl']:+9,.0f}  ${xs['avg_win']:+9,.0f}  ${xs['avg_loss']:+9,.0f}",
    f"  LEVERAGED      {ls['trades']:6d}  {ls['win_rate']:5.1f}%  {ls['pf']:5.2f}    ${ls['total_pnl']:+9,.0f}  ${ls['avg_win']:+9,.0f}  ${ls['avg_loss']:+9,.0f}",
    f"  REGIME         {rs['trades']:6d}  {rs['win_rate']:5.1f}%  {rs['pf']:5.2f}    ${rs['total_pnl']:+9,.0f}  ${rs['avg_win']:+9,.0f}  ${rs['avg_loss']:+9,.0f}",
    f"  SECTOR E5      {e5s['trades']:6d}  {e5s['win_rate']:5.1f}%  {e5s['pf']:5.2f}    ${e5s['total_pnl']:+9,.0f}  ${e5s['avg_win']:+9,.0f}  ${e5s['avg_loss']:+9,.0f}",
    f"  CHOP E6        {e6s['trades']:6d}  {e6s['win_rate']:5.1f}%  {e6s['pf']:5.2f}    ${e6s['total_pnl']:+9,.0f}  ${e6s['avg_win']:+9,.0f}  ${e6s['avg_loss']:+9,.0f}",
    f"  DBLBOT E7      {e7s['trades']:6d}  {e7s['win_rate']:5.1f}%  {e7s['pf']:5.2f}    ${e7s['total_pnl']:+9,.0f}  ${e7s['avg_win']:+9,.0f}  ${e7s['avg_loss']:+9,.0f}",
    f"  COMBINED       {cs['trades']:6d}  {cs['win_rate']:5.1f}%  {cs['pf']:5.2f}    ${cs['total_pnl']:+9,.0f}  ${cs['avg_win']:+9,.0f}  ${cs['avg_loss']:+9,.0f}",
    f"{'-'*100}",
    "\nBY TIER",
    f"{'-'*100}",
    f"  TIER                      TRADES   WIN%    PF       P&L        AVG WIN    AVG LOSS   AVG HOLD",
]
tier_labels = {6: "Tier 1 · High Conviction", 5: "Tier 2 · Confident", 4: "Tier 3 · Qualified"}
for s in sorted(star_stats.keys(), reverse=True):
    label = tier_labels.get(s, f"conf={s}")
    lines.append(
        f"  {label:<26} {star_stats[s]['trades']:6d}  {star_stats[s]['win_rate']:5.1f}%  {star_stats[s]['pf']:5.2f}   ${star_stats[s]['total_pnl']:+9,.0f}  ${star_stats[s]['avg_win']:+8,.0f}  ${star_stats[s]['avg_loss']:+8,.0f}   {star_stats[s]['avg_hold']:4.1f}d"
    )

trigger_stats = {}
for t in all_trades:
    strat_clean = re.sub(r'\s*\([^)]*\)', '', t["strategies"]).strip()
    if strat_clean not in trigger_stats:
        trigger_stats[strat_clean] = {"trades": 0, "wins": 0, "pnl": 0.0, "gross_losses": 0.0, "gross_wins": 0.0}
    trigger_stats[strat_clean]["trades"] += 1
    if t["pnl_dollar"] > 0: 
        trigger_stats[strat_clean]["wins"] += 1
        trigger_stats[strat_clean]["gross_wins"] += t["pnl_dollar"]
    else:
        trigger_stats[strat_clean]["gross_losses"] += abs(t["pnl_dollar"])
    trigger_stats[strat_clean]["pnl"] += t["pnl_dollar"]

lines += [
    f"{'-'*100}",
    "\nBY TRIGGER LOGIC",
    f"{'-'*100}",
]
for strat, stats in sorted(trigger_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
    wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] > 0 else 0
    pf = stats["gross_wins"] / stats["gross_losses"] if stats["gross_losses"] > 0 else float('inf')
    lines.append(f"  {strat[:40]:<40} | Trades: {stats['trades']:4d} | Win%: {wr:5.1f}% | PF: {pf:4.2f} | P&L: ${stats['pnl']:+9,.0f}")

lines += [
    f"{'-'*100}",
    "\nACCOUNT YEARLY GROWTH (Compounding single shared account)",
    f"{'-'*100}",
    f"  YEAR   TRADES  WIN%    SWING P&L   INDEX P&L   LEVERAGE P&L   REGIME P&L   SECTOR P&L    CHOP P&L   DBLBOT P&L   TOTAL P&L    BALANCE      RETURN    SPY RETURN",
]

for r in sim_rows:
    spy_ret = 0.0
    if "SPY" in ticker_dfs:
        try:
            spy_sub = ticker_dfs["SPY"].loc[str(r['year'])]
            if len(spy_sub) > 0:
                s_o = float(spy_sub.iloc[0]["Close"])
                s_c = float(spy_sub.iloc[-1]["Close"])
                spy_ret = (s_c - s_o) / s_o * 100
        except: pass

    wr = r["wins"] / r["trades"] * 100 if r["trades"] > 0 else 0
    lines.append(
        f"  {r['year']:4s}  {r['trades']:6d}  {wr:3.0f}% "
        f" ${r['sw_pnl']:+9,.0f}  ${r['ix_pnl']:+9,.0f}  ${r['lv_pnl']:+10,.0f}  ${r['rg_pnl']:+10,.0f}  ${r['e5_pnl']:+9,.0f}  ${r['e6_pnl']:+9,.0f}  ${r['e7_pnl']:+9,.0f}  "
        f" ${r['yr_pnl']:+9,.0f}  ${r['balance']:10,.0f}   {r['yr_return']:+6.1f}%   (SPY: {spy_ret:+6.1f}%)"
    )

lines += [
    f"{'-'*100}",
    "\nTIER 1 + TIER 2 ONLY — YEAR BY YEAR  (trades you personally take)",
    f"{'-'*100}",
    f"  YEAR   T1 TRADES  T1 WIN%   T1 P&L     T2 TRADES  T2 WIN%   T2 P&L    COMBINED P&L   SPY RETURN",
]
for r in sim_rows:
    yd = by_year[r["year"]]
    spy_ret = 0.0
    if "SPY" in ticker_dfs:
        try:
            spy_sub = ticker_dfs["SPY"].loc[str(r["year"])]
            if len(spy_sub) > 0:
                s_o = float(spy_sub.iloc[0]["Close"])
                s_c = float(spy_sub.iloc[-1]["Close"])
                spy_ret = (s_c - s_o) / s_o * 100
        except: pass
    t1wr = yd["t1_wins"] / yd["t1_trades"] * 100 if yd["t1_trades"] > 0 else 0
    t2wr = yd["t2_wins"] / yd["t2_trades"] * 100 if yd["t2_trades"] > 0 else 0
    t12_pnl = yd["t1_pnl"] + yd["t2_pnl"]
    lines.append(
        f"  {r['year']:4s}  {yd['t1_trades']:6d}  {t1wr:5.1f}%  ${yd['t1_pnl']:+9,.0f}"
        f"    {yd['t2_trades']:6d}  {t2wr:5.1f}%  ${yd['t2_pnl']:+9,.0f}"
        f"    ${t12_pnl:+10,.0f}   (SPY: {spy_ret:+6.1f}%)"
    )
t1_total = sum(yd["t1_pnl"] for yd in by_year.values())
t2_total = sum(yd["t2_pnl"] for yd in by_year.values())
t1_tr    = sum(yd["t1_trades"] for yd in by_year.values())
t2_tr    = sum(yd["t2_trades"] for yd in by_year.values())
t1_wr    = sum(yd["t1_wins"] for yd in by_year.values()) / t1_tr * 100 if t1_tr else 0
t2_wr    = sum(yd["t2_wins"] for yd in by_year.values()) / t2_tr * 100 if t2_tr else 0
lines.append(f"  {'─'*98}")
lines.append(
    f"  TOTAL  {t1_tr:6d}  {t1_wr:5.1f}%  ${t1_total:+9,.0f}"
    f"    {t2_tr:6d}  {t2_wr:5.1f}%  ${t2_total:+9,.0f}"
    f"    ${t1_total+t2_total:+10,.0f}"
)

# ── CSV export ──────────────────────────────────────────────────────────────────
csv_path = OUT_FILE.replace(".txt", ".csv")
df_all["tier"] = df_all.apply(_infer_tier, axis=1)
df_all.to_csv(csv_path, index=False)
print(f"Saved CSV: {csv_path}")

report = "\n".join(lines)
print("\n" + report + "\n")

with open(OUT_FILE, "w") as f:
    f.write(report)
print(f"Saved: {OUT_FILE}")
