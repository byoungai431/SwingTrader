"""
backtest_chop.py — Engine 6: Range Reversion (Bollinger Band Mean-Reversion)
=============================================================================
Targets sideways/range-bound market conditions missed by trend-following engines.

Strategy:
- Regime gate: SPY ADX(14) < 25 for 10+ consecutive bars (confirmed chop)
- Stock filter: Stock ADX(14) < 25 for 10+ bars + no 20-day high/low breakout for 10+ bars
- Regime gate 2: SPY 50-day SMA gate — blocks entries after 2 consecutive closes below 50 SMA, lifts after 2 consecutive closes above
- Entry:        2-bar bounce — 2 bars ago <= lower BB, prev bar > lower BB, current > lower BB + rel_vol > 1.5x
- Take profit:  +10% above entry
- Hard stop:    -6% below entry (fixed floor)
- Stale cut:    12 trading days

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

# ── Date range ──────────────────────────────────────────────────────────────────
START_DATE = "2010-01-01"
END_DATE   = "2026-01-01"

# ── Position sizing ─────────────────────────────────────────────────────────────
TRADE_SIZE           = 2_000
TRADE_SIZE_INCREMENT = 300
STARTING_BALANCE     = 10_000
COMMISSION           = 0.001   # 0.1% per side
TIER1_SIZE_MULT      = 1.50    # High conviction
TIER2_SIZE_MULT      = 1.00    # Standard
TIER3_SIZE_MULT      = 0.80    # Marginal

def _trade_size(date):
    return round(TRADE_SIZE + TRADE_SIZE_INCREMENT * max(0, date.year - int(START_DATE[:4])))

def _trade_size_for_tier(date, tier):
    mult = {1: TIER1_SIZE_MULT, 2: TIER2_SIZE_MULT, 3: TIER3_SIZE_MULT}.get(tier, 1.0)
    return round(_trade_size(date) * mult)

# ── Engine 6 config ─────────────────────────────────────────────────────────────
E6_BB_PERIOD      = 20
E6_BB_STD         = 2.0
E6_VOL_SPIKE_MIN  = 1.5     # Volume confirmation on entry bar
E6_ADX_MIN        = 10      # Min ADX — exclude ultra-flat stocks
E6_ADX_MAX        = 22
E6_ADX_PERIOD     = 14
E6_RSI_MIN        = 35      # Min RSI at entry — exclude already-beaten-down stocks
E6_CHOP_BARS      = 7       # Consecutive bars ADX must be below threshold
E6_RANGE_BARS     = 10      # Consecutive bars with no 20-day breakout
E6_DIP_LOOKBACK        = 10      # Days to look back for a recent dip below lower BB
E6_DIP_MIN_PCT         = 0.02   # Dip must be at least 2% below lower BB
E6_DIP_AVOID_LOW       = 0.05   # Avoid dips in the 5–15% range (ambiguous zone)
E6_DIP_AVOID_HIGH      = 0.09   # Re-allow dips >= 9% (deep capitulation path)
E6_DEEP_REBOUND_PCT    = 0.08   # For 9%+ dips: only need 8% bounce from lowest close
E6_STRONG_RECOVER_PCT  = 0.05   # Standard recovery: Close >= lower_BB * 1.05
E6_SHALLOW_RECOVER_PCT = 0.08   # Shallow dip (< 2%): require Close >= lower_BB * 1.08
E6_RSI_MAX        = 65      # Max RSI at entry — stock must not have recovered too far
E6_STOP_PCT       = 0.06    # Hard stop: -6% below entry
E6_TP_PCT         = 0.10    # Take profit: +10% above entry
E6_STALE_CUT_DAYS      = 12
E6_MAX_HOLD            = 25
E6_COOLDOWN_BARS       = 1       # Per-ticker cooldown after a stop (bars)
E6_CONSEC_LOSS_LIMIT   = 5       # Consecutive losses before 1st portfolio freeze
E6_CONSEC_LOSS_LIMIT2  = 3       # Consecutive losses to re-trigger freeze (no win since last cooldown)
E6_LOSS_COOLDOWN_DAYS  = 15      # Days to freeze (1st trigger)
E6_LOSS_COOLDOWN_DAYS2 = 7       # Days to freeze (subsequent triggers)
E6_TRAIL_ACTIVATE_PCT  = 0.08    # Activate trailing stop once gain >= 8%
E6_TRAIL_DISTANCE_PCT  = 0.04    # Trail sits 4% below rolling peak
SP500_LIMIT            = 500

# ── Load S&P 500 tickers ────────────────────────────────────────────────────────
print("Loading S&P 500 tickers from Wikipedia...")
try:
    _req = urllib.request.Request(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 (compatible; backtest/1.0)"},
    )
    with urllib.request.urlopen(_req) as _resp:
        _html = _resp.read()
    _table = pd.read_html(io.BytesIO(_html))[0]
    _table["Symbol"] = _table["Symbol"].str.replace(".", "-", regex=False)
    TICKERS = _table["Symbol"].tolist()[:SP500_LIMIT]
    print(f"  Loaded {len(TICKERS)} tickers")
except Exception:
    print("Warning: could not fetch S&P 500. Using fallback.")
    TICKERS = ["AAPL", "MSFT", "GOOGL", "AMZN", "JPM", "UNH", "HD", "PG"]

ALL_TICKERS = sorted(set(TICKERS) | {"SPY"})

# ── Indicator helpers ───────────────────────────────────────────────────────────
def _rsi(close, period=14):
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _bollinger(close, period=20, std_dev=2):
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    return mid + std_dev * std, mid, mid - std_dev * std

def _atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

def _adx(high, low, close, period=14):
    up_move  = high.diff()
    dn_move  = -low.diff()
    plus_dm  = pd.Series(np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0), index=close.index)
    minus_dm = pd.Series(np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0), index=close.index)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_s    = tr.ewm(com=period - 1, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / atr_s
    dx       = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(com=period - 1, adjust=False).mean()

def build_indicators(df):
    """Pre-compute all indicators for a single ticker. Returns enriched DataFrame."""
    d = df.copy()
    d["rsi"]      = _rsi(d["Close"])
    d["atr14"]    = _atr(d["High"], d["Low"], d["Close"])
    d["adx"]      = _adx(d["High"], d["Low"], d["Close"], E6_ADX_PERIOD)
    d["vol_avg20"] = d["Volume"].rolling(20).mean()
    d["rel_vol"]   = d["Volume"] / d["vol_avg20"]
    d["high20"] = d["High"].rolling(20).max()
    d["low20"]  = d["Low"].rolling(20).min()
    bb_upper, bb_mid, bb_lower = _bollinger(d["Close"], E6_BB_PERIOD, E6_BB_STD)
    d["bb_upper"] = bb_upper
    d["bb_mid"]   = bb_mid
    d["bb_lower"] = bb_lower
    d["ma50"]     = d["Close"].rolling(50).mean()

    # Pre-compute consecutive bars below ADX threshold (rolling count)
    adx_low         = (d["adx"] < E6_ADX_MAX).astype(int)
    # consecutive count: resets to 0 on any bar above threshold
    consec = adx_low.groupby((adx_low != adx_low.shift()).cumsum()).cumcount() + 1
    d["adx_consec"] = consec * adx_low  # 0 on bars where ADX >= threshold

    # Pre-compute consecutive bars inside 20-day range (no breakout)
    in_range        = ((d["Close"] < d["high20"]) & (d["Close"] > d["low20"])).astype(int)
    consec_r        = in_range.groupby((in_range != in_range.shift()).cumsum()).cumcount() + 1
    d["range_consec"] = consec_r * in_range

    # Recent dip below lower BB (1 if any close in last N bars was below BB)
    below_flag           = (d["Close"] < d["bb_lower"]).astype(int)
    d["recent_below_bb"] = below_flag.shift(1).rolling(E6_DIP_LOOKBACK).max()

    # Max dip depth below lower BB in lookback window (as % below band, positive = below)
    dip_pct              = ((d["bb_lower"] - d["Close"]) / d["bb_lower"]).clip(lower=0)
    d["max_dip_pct"]     = dip_pct.shift(1).rolling(E6_DIP_LOOKBACK).max()

    # Lowest close in lookback window (for deep-dip rebound entry)
    d["min_close_lookback"] = d["Close"].shift(1).rolling(E6_DIP_LOOKBACK).min()

    return d

# ── Main backtest ────────────────────────────────────────────────────────────────
def run_backtest():
    print(f"\nDownloading price data ({START_DATE} → {END_DATE})...")
    raw = yf.download(
        ALL_TICKERS,
        start=START_DATE,
        end=END_DATE,
        auto_adjust=True,
        progress=True,
    )

    # ── Pre-compute indicators for all tickers ──────────────────────────────────
    print("\nPre-computing indicators for all tickers...")
    indicators = {}   # ticker → DataFrame with indicators
    for ticker in ALL_TICKERS:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw.xs(ticker, axis=1, level=1).dropna(how="all")
            else:
                df = raw.dropna(how="all")
            if len(df) < E6_BB_PERIOD + E6_ADX_PERIOD + 5:
                continue
            indicators[ticker] = build_indicators(df)
        except Exception:
            continue
    print(f"  Indicators ready for {len(indicators)} tickers")

    # ── SPY chop regime + bull gate ─────────────────────────────────────────────
    spy_df      = indicators.get("SPY")
    spy_regime  = {}   # date → bool (ADX chop)
    if spy_df is not None:
        spy_regime = (spy_df["adx_consec"] >= E6_CHOP_BARS).to_dict()

    # ── Backtest loop ────────────────────────────────────────────────────────────
    trades              = []
    balance             = STARTING_BALANCE
    open_pos            = {}    # ticker → position dict
    cooldown            = {}    # ticker → bars remaining (per-ticker stop cooldown)
    consec_losses       = 0     # portfolio-level consecutive loss counter
    global_cooldown     = 0     # bars remaining in portfolio freeze
    cooldown_triggers   = 0     # how many times the freeze was triggered
    consec_cooldowns    = 0     # back-to-back cooldown counter (resets on any win)
    spy_below_50_streak = 0     # consecutive days SPY closed below 50 SMA
    spy_above_50_streak = 0     # consecutive days SPY closed above 50 SMA
    spy_gated           = False # True once 2-bar below-50 streak activates

    all_dates = spy_df.index.tolist() if spy_df is not None else []

    print(f"\nRunning backtest over {len(all_dates)} trading days...")
    for i, date in enumerate(all_dates):
        if i < max(E6_BB_PERIOD, E6_ADX_PERIOD, 20) + E6_CHOP_BARS:
            continue

        spy_chop = spy_regime.get(date, False)

        # SPY 50 SMA gate — activates after 2 consecutive closes below, lifts after 2 above
        if spy_df is not None and date in spy_df.index:
            _spy_r = spy_df.loc[date]
            _ma50  = _spy_r.get("ma50", float("nan"))
            if not pd.isna(_ma50):
                if float(_spy_r["Close"]) < float(_ma50):
                    spy_below_50_streak += 1
                    spy_above_50_streak  = 0
                    if spy_below_50_streak >= 2:
                        spy_gated = True
                else:
                    spy_above_50_streak += 1
                    spy_below_50_streak  = 0
                    if spy_above_50_streak >= 2:
                        spy_gated = False

        # ── Manage open positions ───────────────────────────────────────────────
        to_close = []
        for ticker, pos in open_pos.items():
            idf = indicators.get(ticker)
            if idf is None or date not in idf.index:
                continue
            row = idf.loc[date]
            if pd.isna(row["Close"]):
                continue

            pos["hold"] += 1
            exit_price  = None
            exit_reason = None

            # Update rolling peak and activate/advance trailing stop
            pos["peak_price"] = max(pos["peak_price"], row["High"])
            if not pos["trail_active"]:
                if pos["peak_price"] >= pos["entry_price"] * (1 + E6_TRAIL_ACTIVATE_PCT):
                    pos["trail_active"] = True
            if pos["trail_active"]:
                trail_stop = round(pos["peak_price"] * (1 - E6_TRAIL_DISTANCE_PCT), 4)
                pos["stop"] = max(pos["stop"], trail_stop)   # ratchet up, never down

            if row["Low"] <= pos["stop"]:
                exit_price  = pos["stop"]
                exit_reason = "TRAIL" if pos["trail_active"] else "STOP"
            elif not pd.isna(pos["target"]) and row["Close"] >= pos["target"]:
                exit_price  = row["Close"]
                exit_reason = "TARGET"
            elif pos["hold"] >= E6_STALE_CUT_DAYS:
                exit_price  = row["Close"]
                exit_reason = "STALE"
            elif pos["hold"] >= E6_MAX_HOLD:
                exit_price  = row["Close"]
                exit_reason = "MAXHOLD"

            if exit_price is not None:
                pnl = (exit_price - pos["entry_price"]) / pos["entry_price"] * pos["size"]
                pnl -= pos["size"] * COMMISSION * 2
                balance += pnl
                trades.append({
                    "ticker":             ticker,
                    "entry_date":         pos["entry_date"],
                    "exit_date":          date,
                    "entry_price":        pos["entry_price"],
                    "exit_price":         exit_price,
                    "size":               pos["size"],
                    "pnl":                pnl,
                    "tier":               pos["tier"],
                    "exit_reason":        exit_reason,
                    "hold_days":          pos["hold"],
                    "year":               pos["entry_date"].year,
                    "entry_path":         pos["entry_path"],
                    "entry_rel_vol":      pos["entry_rel_vol"],
                    "entry_pct_vs_bb":    pos["entry_pct_vs_bb"],
                    "entry_max_dip_pct":  pos["entry_max_dip_pct"],
                    "entry_stock_adx":    pos["entry_stock_adx"],
                    "entry_rsi":          pos["entry_rsi"],
                })
                to_close.append(ticker)
                if exit_reason == "STOP":
                    cooldown[ticker] = E6_COOLDOWN_BARS
                # Update portfolio-level consecutive loss tracker
                if pnl < 0:
                    consec_losses += 1
                    limit = E6_CONSEC_LOSS_LIMIT2 if consec_cooldowns >= 1 else E6_CONSEC_LOSS_LIMIT
                    if consec_losses >= limit:
                        consec_cooldowns += 1
                        global_cooldown   = E6_LOSS_COOLDOWN_DAYS2 if consec_cooldowns >= 2 else E6_LOSS_COOLDOWN_DAYS
                        consec_losses     = 0
                        cooldown_triggers += 1
                elif pnl > 0:
                    consec_losses    = 0
                    consec_cooldowns = 0   # win resets escalation streak

        for t in to_close:
            del open_pos[t]

        for t in list(cooldown.keys()):
            cooldown[t] -= 1
            if cooldown[t] <= 0:
                del cooldown[t]

        if global_cooldown > 0:
            global_cooldown -= 1

        # ── New entries (SPY chop + SPY 50 SMA gate) ────────────────────────────
        if not spy_chop or spy_gated:
            continue
        if global_cooldown > 0:
            continue

        for ticker in TICKERS:
            if ticker in open_pos or ticker in cooldown:
                continue

            idf = indicators.get(ticker)
            if idf is None or date not in idf.index:
                continue

            row = idf.loc[date]
            if any(pd.isna(row[c]) for c in ("bb_lower", "adx", "adx_consec",
                                              "range_consec", "rel_vol", "recent_below_bb",
                                              "max_dip_pct", "min_close_lookback")):
                continue

            # Stock must also be in its own sideways range
            if row["adx_consec"] < E6_CHOP_BARS:
                continue
            if row["range_consec"] < E6_RANGE_BARS:
                continue

            # ADX floor: exclude ultra-flat stocks
            if row["adx"] < E6_ADX_MIN:
                continue

            # RSI band: exclude oversold and overbought stocks
            if row["rsi"] < E6_RSI_MIN or row["rsi"] > E6_RSI_MAX:
                continue

            # Entry signal: recent dip below BB + bounce confirmation + volume
            if row["recent_below_bb"] != 1:
                continue
            # Three valid entry paths (any one qualifies):
            #   A: dip 2–5% below band  + Close >= lower_BB
            #   B: dip 9%+              + 8% bounce from lowest close (no band touch needed)
            #   C: any dip              + Close >= lower_BB * 1.05 (or 1.07 for shallow <2% dips)
            dip   = row["max_dip_pct"]
            close = row["Close"]
            bb_lo = row["bb_lower"]
            path_a = (E6_DIP_MIN_PCT <= dip < E6_DIP_AVOID_LOW) and (close >= bb_lo)
            path_b = (dip >= E6_DIP_AVOID_HIGH) and (close >= row["min_close_lookback"] * (1 + E6_DEEP_REBOUND_PCT))
            recover_req = E6_SHALLOW_RECOVER_PCT if dip < E6_DIP_MIN_PCT else E6_STRONG_RECOVER_PCT
            path_c = (dip > 0) and (close >= bb_lo * (1 + recover_req))
            if not (path_a or path_b or path_c):
                continue
            # Deep dip gate: 7–9% zone must close 2%+ above band (Path B at 9%+ is exempt)
            if 0.07 <= dip < E6_DIP_AVOID_HIGH and close < bb_lo * 1.02:
                continue
            if row["rel_vol"] < E6_VOL_SPIKE_MIN:
                continue

            entry_path  = "C" if path_c and not path_a else ("B" if path_b else "A")
            adx_val  = float(row["adx"])
            rsi_val  = float(row["rsi"])
            vol_val  = float(row["rel_vol"])
            recov_pct = (float(close) / float(bb_lo) - 1) * 100

            # Tier 1: high conviction (any one qualifies)
            is_tier1 = (
                (5.0 <= recov_pct < 8.0)                          or  # Recovery 5–8%
                (35 <= rsi_val < 45)                              or  # RSI 35–45
                (15 <= adx_val < 18)                              or  # ADX 15–18
                path_a                                            or  # Path A (dip 2–5%)
                (10 <= adx_val < 15)                              or  # ADX 10–15
                (15 <= adx_val < 18 and 50 <= rsi_val < 65)          # ADX15-18 + RSI50-65
            )
            # Tier 2: good quality (any one qualifies, if not T1)
            is_tier2 = (not is_tier1) and (
                (1.5 <= vol_val < 2.0)                            or  # Vol 1.5–2x
                (55 <= rsi_val < 65)                              or  # RSI 55–65
                (2.0 <= vol_val < 3.0)                            or  # Vol 2–3x
                (18 <= adx_val < 22 and 35 <= rsi_val < 50)          # ADX18-22 + RSI35-50
            )
            tier = 1 if is_tier1 else (2 if is_tier2 else 3)

            entry_price = row["Close"]
            stop_price  = round(entry_price * (1 - E6_STOP_PCT), 4)   # -6% hard floor
            target      = round(entry_price * (1 + E6_TP_PCT), 4)     # +10% take profit
            size        = _trade_size_for_tier(date, tier)

            open_pos[ticker] = {
                "entry_price":        entry_price,
                "stop":               stop_price,
                "target":             target,
                "entry_date":         date,
                "tier":               tier,
                "size":               size,
                "hold":               0,
                "peak_price":         entry_price,
                "trail_active":       False,
                # Entry snapshots for attribution analysis
                "entry_path":         entry_path,
                "entry_rel_vol":      round(float(row["rel_vol"]), 2),
                "entry_pct_vs_bb":    round((float(row["Close"]) / float(row["bb_lower"]) - 1) * 100, 2),
                "entry_max_dip_pct":  round(float(row["max_dip_pct"]) * 100, 2),
                "entry_stock_adx":    round(float(row["adx"]), 1),
                "entry_rsi":          round(float(row["rsi"]), 1),
            }

    # Force-close remaining open positions at last date
    last_date = all_dates[-1] if all_dates else None
    if last_date:
        for ticker, pos in open_pos.items():
            idf = indicators.get(ticker)
            close_px = pos["entry_price"]
            if idf is not None and last_date in idf.index:
                v = idf.loc[last_date, "Close"]
                if not pd.isna(v):
                    close_px = v
            pnl = (close_px - pos["entry_price"]) / pos["entry_price"] * pos["size"]
            pnl -= pos["size"] * COMMISSION * 2
            balance += pnl
            trades.append({
                "ticker":             ticker,
                "entry_date":         pos["entry_date"],
                "exit_date":          last_date,
                "entry_price":        pos["entry_price"],
                "exit_price":         close_px,
                "size":               pos["size"],
                "pnl":                pnl,
                "tier":               pos["tier"],
                "exit_reason":        "FORCED",
                "hold_days":          pos["hold"],
                "year":               pos["entry_date"].year,
                "entry_path":         pos["entry_path"],
                "entry_rel_vol":      pos["entry_rel_vol"],
                "entry_pct_vs_bb":    pos["entry_pct_vs_bb"],
                "entry_max_dip_pct":  pos["entry_max_dip_pct"],
                "entry_stock_adx":    pos["entry_stock_adx"],
                "entry_rsi":          pos["entry_rsi"],
            })

    return trades, balance, cooldown_triggers

# ── Reporting ────────────────────────────────────────────────────────────────────
def report(trades, final_balance, cooldown_triggers=0):
    if not trades:
        print("\nNo trades generated.")
        return

    df        = pd.DataFrame(trades)
    total     = len(df)
    wins      = (df["pnl"] > 0).sum()
    losses    = (df["pnl"] <= 0).sum()
    wr        = wins / total * 100
    total_pnl = df["pnl"].sum()
    gross_win  = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss = abs(df.loc[df["pnl"] <= 0, "pnl"].sum())
    pf        = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_win   = df.loc[df["pnl"] > 0, "pnl"].mean() if wins > 0 else 0
    avg_loss  = df.loc[df["pnl"] <= 0, "pnl"].mean() if losses > 0 else 0
    avg_hold  = df["hold_days"].mean()
    n_years   = int(END_DATE[:4]) - int(START_DATE[:4])
    cagr      = ((final_balance / STARTING_BALANCE) ** (1 / n_years) - 1) * 100 if n_years > 0 else 0

    sep = "=" * 80
    print(f"\n{sep}")
    print(f"  ENGINE 6 — RANGE REVERSION BACKTEST REPORT")
    print(f"  Period: {START_DATE}  →  {END_DATE}")
    print(f"{'-' * 80}")
    print(f"  Total Trades   : {total}  (T1: {(df['tier']==1).sum()} | T2: {(df['tier']==2).sum()} | T3: {(df['tier']==3).sum()})")
    print(f"  Win Rate       : {wr:.1f}%  ({wins}W / {losses}L)")
    print(f"  Total P&L      : ${total_pnl:+,.2f}")
    print(f"  Profit Factor  : {pf:.2f}")
    print(f"  Avg Win        : ${avg_win:+,.0f}   Avg Loss: ${avg_loss:+,.0f}")
    print(f"  Avg Hold       : {avg_hold:.1f} days")
    print(f"  CAGR           : {cagr:+.2f}%  (${STARTING_BALANCE:,.0f} → ${final_balance:,.0f})")
    print(f"  Loss Cooldowns : {cooldown_triggers}x triggered  (1st: {E6_CONSEC_LOSS_LIMIT} losses → {E6_LOSS_COOLDOWN_DAYS}d / subsequent: {E6_CONSEC_LOSS_LIMIT2} losses → {E6_LOSS_COOLDOWN_DAYS2}d)")
    print(f"{'-' * 80}")

    print(f"\n  {'YEAR':>4}  {'TRADES':>6}  {'WIN%':>5}  {'P&L':>12}  EXIT REASONS")
    print(f"  {'-' * 62}")
    bal = STARTING_BALANCE
    for yr in sorted(df["year"].unique()):
        yd    = df[df["year"] == yr]
        yw    = (yd["pnl"] > 0).sum()
        ywr   = yw / len(yd) * 100 if len(yd) > 0 else 0
        ypnl  = yd["pnl"].sum()
        bal  += ypnl
        exits = yd["exit_reason"].value_counts().to_dict()
        exit_str = "  ".join(f"{k}:{v}" for k, v in sorted(exits.items()))
        print(f"  {yr}  {len(yd):>6}  {ywr:>4.0f}%  {ypnl:>+12,.0f}  {exit_str}")

    print(f"\n  BY TIER")
    print(f"  {'-' * 62}")
    for tier in sorted(df["tier"].unique()):
        td   = df[df["tier"] == tier]
        tw   = (td["pnl"] > 0).sum()
        twr  = tw / len(td) * 100
        tpnl = td["pnl"].sum()
        tgw  = td.loc[td["pnl"] > 0, "pnl"].sum()
        tgl  = abs(td.loc[td["pnl"] <= 0, "pnl"].sum())
        tpf  = tgw / tgl if tgl > 0 else float("inf")
        label = {1: "Tier 1 (High Conv.)", 2: "Tier 2 (Standard)", 3: "Tier 3 (Marginal)"}.get(tier, f"Tier {tier}")
        print(f"  {label:<28}  {len(td):>4} trades  {twr:>4.0f}% WR  PF {tpf:.2f}  P&L ${tpnl:+,.0f}")

    print(f"\n  BY EXIT REASON")
    print(f"  {'-' * 62}")
    for reason, cnt in df["exit_reason"].value_counts().items():
        rd   = df[df["exit_reason"] == reason]
        rpnl = rd["pnl"].sum()
        rwr  = (rd["pnl"] > 0).sum() / cnt * 100
        print(f"  {reason:<10}  {cnt:>4} trades  {rwr:>4.0f}% WR  P&L ${rpnl:+,.0f}")

    # ── Attribution breakdown ────────────────────────────────────────────────────
    def _bucket_report(label, col, buckets):
        """buckets: list of (label, mask_fn) tuples"""
        print(f"\n  BY {label}")
        print(f"  {'-' * 72}")
        print(f"  {'BUCKET':<18}  {'TRADES':>6}  {'WIN%':>5}  {'PF':>5}  {'AVG WIN':>9}  {'AVG LOSS':>9}  {'P&L':>12}")
        for name, mask in buckets:
            sub = df[df[col].apply(mask)] if col else df[mask(df)]
            if len(sub) == 0:
                continue
            sw  = (sub["pnl"] > 0).sum()
            swr = sw / len(sub) * 100
            sgw = sub.loc[sub["pnl"] > 0, "pnl"].sum()
            sgl = abs(sub.loc[sub["pnl"] <= 0, "pnl"].sum())
            spf = sgw / sgl if sgl > 0 else float("inf")
            saw = sub.loc[sub["pnl"] > 0, "pnl"].mean() if sw > 0 else 0
            sal = sub.loc[sub["pnl"] <= 0, "pnl"].mean() if (len(sub) - sw) > 0 else 0
            spl = sub["pnl"].sum()
            print(f"  {name:<18}  {len(sub):>6}  {swr:>4.0f}%  {spf:>5.2f}  {saw:>+9,.0f}  {sal:>+9,.0f}  {spl:>+12,.0f}")

    _bucket_report("ENTRY PATH", "entry_path", [
        ("A: dip 2-5%, bb touch", lambda v: v == "A"),
        ("B: dip 15%+, rebound",  lambda v: v == "B"),
        ("C: any dip, +5% recov", lambda v: v == "C"),
    ])

    _bucket_report("MAX DIP BELOW LOWER BB (lookback)", "entry_max_dip_pct", [
        ("0 – 2%",   lambda v: v < 2.0),
        ("2 – 5%",   lambda v: 2.0 <= v < 5.0),
        ("5 – 10%",  lambda v: 5.0 <= v < 10.0),
        ("10%+",     lambda v: v >= 10.0),
    ])

    _bucket_report("RECOVERY AT ENTRY (above lower BB)", "entry_pct_vs_bb", [
        ("+5 to +7%",  lambda v: 5.0 <= v < 7.0),
        ("+7 to +10%", lambda v: 7.0 <= v < 10.0),
        ("+10 to +15%",lambda v: 10.0 <= v < 15.0),
        ("+15%+",      lambda v: v >= 15.0),
    ])

    _bucket_report("VOLUME (rel_vol at entry)", "entry_rel_vol", [
        ("1.5 – 2.0x",  lambda v: 1.5 <= v < 2.0),
        ("2.0 – 3.0x",  lambda v: 2.0 <= v < 3.0),
        ("3.0 – 5.0x",  lambda v: 3.0 <= v < 5.0),
        ("5.0x+",       lambda v: v >= 5.0),
    ])


    _bucket_report("STOCK ADX at entry", "entry_stock_adx", [
        ("0 – 10",  lambda v: v < 10),
        ("10 – 15", lambda v: 10 <= v < 15),
        ("15 – 18", lambda v: 15 <= v < 18),
        ("18 – 22", lambda v: 18 <= v < 22),
    ])

    _bucket_report("RSI at entry", "entry_rsi", [
        ("<30  (oversold)",  lambda v: v < 30),
        ("30 – 40",          lambda v: 30 <= v < 40),
        ("40 – 50",          lambda v: 40 <= v < 50),
        ("50 – 60",          lambda v: 50 <= v < 60),
        ("60+",              lambda v: v >= 60),
    ])

    print(f"\n{sep}")

    # ── Save ────────────────────────────────────────────────────────────────────
    label    = (f"engine6_chop_{START_DATE}_{END_DATE}"
                f"_BB{E6_BB_PERIOD}x{E6_BB_STD}_ADX{E6_ADX_MAX}"
                f"_dip2-5_dip15plus_chop{E6_CHOP_BARS}b_range{E6_RANGE_BARS}b"
                f"_SL{int(E6_STOP_PCT*100)}pct_TP{int(E6_TP_PCT*100)}pct")
    csv_path = os.path.join(OUT_DIR, f"{label}.csv")
    txt_path = os.path.join(OUT_DIR, f"{label}.txt")
    df.to_csv(csv_path, index=False)
    with open(txt_path, "w") as f:
        f.write(f"ENGINE 6 — RANGE REVERSION BACKTEST\n")
        f.write(f"Period: {START_DATE} → {END_DATE}\n")
        f.write(f"Trades: {total}  WR: {wr:.1f}%  PF: {pf:.2f}  "
                f"P&L: ${total_pnl:+,.2f}  CAGR: {cagr:+.2f}%\n\n")
        f.write(df.to_string(index=False))
    print(f"\nSaved: {csv_path}")
    print(f"Saved: {txt_path}")


if __name__ == "__main__":
    trades, final_balance, cooldown_triggers = run_backtest()
    report(trades, final_balance, cooldown_triggers)
