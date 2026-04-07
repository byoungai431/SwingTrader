"""
backtest_master.py — The Ultimate All-Weather 3-Track Combined Backtest
=======================================================================
Runs all three engines independently on a shared account:
1 - CORE SWING: Oversold pullbacks in Bull Regimes on S&P 500
2 - INDEX FADE: Fading RSI > 80 via Inverse ETFs
3 - LEVERAGED BOUNCE: Catching pure Capitulation via 3x ETFs

Period: 2010-01-01 → 2026-01-01
"""

import io
import os
import numpy as np
import pandas as pd
import yfinance as yf
import urllib.request
from datetime import datetime

# ── Paths ───────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
OUT_DIR  = os.path.join(BASE_DIR, "backtest results")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Date range ─────────────────────────────────────────────────────────────────
START_DATE = "2010-01-01"
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





# ── Load S&P 500 tickers ────────────────────────────────────────────────────────
print("Loading S&P 500 tickers from Wikipedia...")
try:
    _req = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 (compatible; backtest/1.0)"},
    )
    with urllib.request.urlopen(_req) as _resp:
        _html = _resp.read()
    _table     = pd.read_html(io.BytesIO(_html))[0]
    SW_TICKERS = [t.replace(".", "-") for t in _table["Symbol"].tolist()][:SP500_LIMIT]
    print(f"  Loaded {len(SW_TICKERS)} S&P 500 tickers")
except Exception:
    print("Warning: could not fetch S&P 500. Using basic fallback.")
    SW_TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN"] # Basic fallback

# ── Combined ticker list ────────────────────────────────────────────────────────
IX_ETFs = set()
for g in IX_GROUPS:
    IX_ETFs.update(g["signals"])
    IX_ETFs.add(g["execution"])

LEV_ETFs = set(LEV_UNDERLYING_MAP.keys()) | set(LEV_UNDERLYING_MAP.values())

ALL_TICKERS = sorted(set(SW_TICKERS) | IX_ETFs | LEV_ETFs)

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

START_TS = pd.Timestamp(START_DATE)

# ── Compute true relative strength vs SPY for Engine 4 ─────────────────────────
if "SPY" in ticker_dfs:
    spy_ret63 = ticker_dfs["SPY"]["ret63"]
    for ticker in SW_TICKERS:
        if ticker not in ticker_dfs: continue
        stk_ret63 = ticker_dfs[ticker]["ret63"]
        spy_aligned = spy_ret63.reindex(stk_ret63.index)
        ticker_dfs[ticker]["rs_spy"] = stk_ret63 / spy_aligned.replace(0, float("nan"))

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

cs, ss, xs, ls, rs = _stats(df_all), _stats(df_sw), _stats(df_ix), _stats(df_lv), _stats(df_rg)

# ── Per-star breakdown (across all trades that have confidence_stars) ──────────
star_stats = {}
for stars in sorted(df_all["confidence_stars"].dropna().unique()):
    df_star = df_all[df_all["confidence_stars"] == stars]
    star_stats[int(stars)] = _stats(df_star)

by_year = {}
for _, t in df_all.iterrows():
    yr = t["entry_date"][:4]
    if yr not in by_year: by_year[yr] = {"pnl": 0, "trades": 0, "wins": 0, "sw_pnl": 0, "ix_pnl": 0, "lv_pnl": 0, "rg_pnl": 0}
    by_year[yr]["pnl"] += t["pnl_dollar"]
    by_year[yr]["trades"] += 1
    by_year[yr]["wins"] += 1 if t["pnl_dollar"] > 0 else 0
    if t["strategy_type"] == "SWING": by_year[yr]["sw_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "INDEX": by_year[yr]["ix_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "LEVERAGED": by_year[yr]["lv_pnl"] += t["pnl_dollar"]
    elif t["strategy_type"] == "REGIME":   by_year[yr]["rg_pnl"] += t["pnl_dollar"]

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
    "  MASTER 3-TRACK UNIFIED BACKTEST REPORT",
    f"  Period: {START_DATE}  →  {END_DATE}",
    f"{'-'*100}",
    f"  Total Trades    : {cs['trades']}  (SWING: {ss['trades']} | INDEX: {xs['trades']} | LEVERAGED: {ls['trades']} | REGIME: {rs['trades']})",
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
    f"  STRATEGY    TRADES   WIN%    PF        P&L        AVG WIN     AVG LOSS",
    f"  SWING       {ss['trades']:6d}  {ss['win_rate']:5.1f}%  {ss['pf']:5.2f}    ${ss['total_pnl']:+9,.0f}  ${ss['avg_win']:+9,.0f}  ${ss['avg_loss']:+9,.0f}",
    f"  INDEX FADE  {xs['trades']:6d}  {xs['win_rate']:5.1f}%  {xs['pf']:5.2f}    ${xs['total_pnl']:+9,.0f}  ${xs['avg_win']:+9,.0f}  ${xs['avg_loss']:+9,.0f}",
    f"  LEVERAGED   {ls['trades']:6d}  {ls['win_rate']:5.1f}%  {ls['pf']:5.2f}    ${ls['total_pnl']:+9,.0f}  ${ls['avg_win']:+9,.0f}  ${ls['avg_loss']:+9,.0f}",
    f"  REGIME      {rs['trades']:6d}  {rs['win_rate']:5.1f}%  {rs['pf']:5.2f}    ${rs['total_pnl']:+9,.0f}  ${rs['avg_win']:+9,.0f}  ${rs['avg_loss']:+9,.0f}",
    f"  COMBINED    {cs['trades']:6d}  {cs['win_rate']:5.1f}%  {cs['pf']:5.2f}    ${cs['total_pnl']:+9,.0f}  ${cs['avg_win']:+9,.0f}  ${cs['avg_loss']:+9,.0f}",
    f"{'-'*100}",
    "\nBY STAR RATING",
    f"{'-'*100}",
    f"  STARS                TRADES   WIN%    PF       P&L        AVG WIN    AVG LOSS   AVG HOLD",
] 
star_labels = {4: "4 stars (silver)", 5: "5 star (gold stars)", 6: "5 star max (blue diamond)"}
for s in sorted(star_stats.keys()):
    label = star_labels.get(s, "★"*s)
    lines.append(
        f"  {label:<18} {star_stats[s]['trades']:6d}  {star_stats[s]['win_rate']:5.1f}%  {star_stats[s]['pf']:5.2f}   ${star_stats[s]['total_pnl']:+9,.0f}  ${star_stats[s]['avg_win']:+8,.0f}  ${star_stats[s]['avg_loss']:+8,.0f}   {star_stats[s]['avg_hold']:4.1f}d"
    )

import re
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
    f"  YEAR   TRADES  WIN%    SWING P&L   INDEX P&L   LEVERAGE P&L   REGIME P&L    TOTAL P&L    BALANCE      RETURN    SPY RETURN",
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
        f" ${r['sw_pnl']:+9,.0f}  ${r['ix_pnl']:+9,.0f}  ${r['lv_pnl']:+10,.0f}  ${r['rg_pnl']:+10,.0f}  "
        f" ${r['yr_pnl']:+9,.0f}  ${r['balance']:10,.0f}   {r['yr_return']:+6.1f}%   (SPY: {spy_ret:+6.1f}%)"
    )

report = "\n".join(lines)
print("\n" + report + "\n")

with open(OUT_FILE, "w") as f:
    f.write(report)
print(f"Saved: {OUT_FILE}")
