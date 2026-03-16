"""
backtest_model.py
Pilot: replay historical entry signals through the full Claude model to measure
whether confidence-score filtering improves profit factor vs pure rules.

Workflow:
  1. Run rules-based backtest on PILOT_TICKERS (2024-01-01 → 2026-01-01)
  2. For each entry signal, slice data up to that date → compute_indicators → Claude API
  3. Record model confidence (1-5 stars) and whether model agrees (BUY/SELL)
  4. Compare P&L at each confidence threshold (≥3, ≥4, ≥5)

Run:
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 backtest_model.py
"""

import os
import json
import time
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime

from indicators import compute_indicators
from signal_engine import get_signal as model_get_signal

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(__file__)
OUT_DIR      = os.path.join(BASE_DIR, "backtest results")
os.makedirs(OUT_DIR, exist_ok=True)

WL_FILE  = os.path.join(BASE_DIR, "watchlist.json")
with open(WL_FILE) as _f:
    PILOT_TICKERS = json.load(_f)

START_DATE    = "2022-01-01"
END_DATE      = "2024-01-01"
TRADE_SIZE    = 1_000
COMMISSION    = 0.001
ATR_MULT      = 1.5
TP_PCT        = 0.095
MAX_HOLD      = 10

OUT_FILE = os.path.join(OUT_DIR, f"backtest_model_{START_DATE}_{END_DATE}.txt")
CSV_FILE = os.path.join(OUT_DIR, f"backtest_model_{START_DATE}_{END_DATE}.csv")

# ── Indicator helpers (self-contained, matches backtest.py) ───────────────────
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
    df["ma200"]     = df["Close"].rolling(200).mean()
    df["atr14"]     = _atr(df["High"], df["Low"], df["Close"])
    return df

def rules_signal(df, i):
    """Pure rules-based signal (mirrors backtest.py get_signal)."""
    if i < 2:
        return None
    r  = df.iloc[i]
    p  = df.iloc[i - 1]
    pp = df.iloc[i - 2]
    for col in ("rsi", "macd_hist", "ma50", "ma200", "atr14"):
        if pd.isna(r[col]):
            return None
    golden = r["ma50"] > r["ma200"]
    death  = r["ma50"] < r["ma200"]
    buy_conds = [
        r["rsi"] < 35,
        r["macd_hist"] > 0 and p["macd_hist"] <= 0,
        golden and r["Close"] > r["ma200"],
    ]
    sell_conds = [
        r["rsi"] > 65,
        r["macd_hist"] < 0 and p["macd_hist"] >= 0,
        death,
    ]
    buys  = sum(bool(c) for c in buy_conds)
    sells = sum(bool(c) for c in sell_conds)
    two_green = r["Close"] > p["Close"] and p["Close"] > pp["Close"]
    two_red   = r["Close"] < p["Close"] and p["Close"] < pp["Close"]
    bull_regime = r["Close"] > r["ma200"]
    bear_regime = r["Close"] < r["ma200"]
    if buys >= 2 and sells == 0 and two_green and bull_regime:
        return "BUY"
    if sells >= 2 and buys == 0 and two_red and bear_regime:
        return "SELL"
    return None

# ── Download data ──────────────────────────────────────────────────────────────
warmup = (pd.Timestamp(START_DATE) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
print(f"Downloading data for {PILOT_TICKERS} ({warmup} → {END_DATE})...")
raw = yf.download(PILOT_TICKERS, start=warmup, end=END_DATE, progress=True, auto_adjust=True)

START_TS = pd.Timestamp(START_DATE)
all_trades = []

# ── Phase 1: collect all entry signals via rules ───────────────────────────────
print("\nPhase 1: Scanning for entry signals...")
entry_signals = []  # list of (ticker, entry_date_idx, df_full, direction, close, atr)

for ticker in PILOT_TICKERS:
    try:
        lvl = raw.columns.get_level_values(1) if raw.columns.nlevels > 1 else None
        if lvl is not None:
            if ticker not in lvl:
                continue
            df_raw = raw.xs(ticker, axis=1, level=1).copy()
        else:
            df_raw = raw.copy()

        df_raw = df_raw.dropna(subset=["Close"])
        if len(df_raw) < 210:
            continue

        df = build_indicator_df(df_raw)
        bt_idx = df.index[df.index >= START_TS]

        position  = None
        hold_days = 0
        cooldown  = 0

        for date in bt_idx:
            i     = df.index.get_loc(date)
            row   = df.iloc[i]
            close = row["Close"]
            atr   = row["atr14"]

            if cooldown > 0:
                cooldown -= 1

            if position is not None:
                hold_days += 1
                exit_price  = None
                exit_reason = None

                if row["Low"] <= position["stop"]:
                    exit_price  = position["stop"]
                    exit_reason = "Stop-Loss"
                elif row["High"] >= position["target"]:
                    exit_price  = position["target"]
                    exit_reason = "Take-Profit"

                if exit_price is None and hold_days >= MAX_HOLD:
                    exit_price  = close
                    exit_reason = "Max Hold (10d)"

                if exit_price is None:
                    sig = rules_signal(df, i)
                    if sig == "SELL":
                        exit_price  = close
                        exit_reason = "Sell Signal"

                if exit_price is not None:
                    pnl_pct = (exit_price - position["entry"]) / position["entry"]
                    pnl_pct -= 2 * COMMISSION
                    pnl_dollar  = pnl_pct * TRADE_SIZE

                    position["exit_date"]   = date.strftime("%Y-%m-%d")
                    position["exit_price"]  = round(exit_price, 2)
                    position["exit_reason"] = exit_reason
                    position["hold_days"]   = hold_days
                    position["pnl_pct"]     = pnl_pct
                    position["pnl_dollar"]  = pnl_dollar
                    all_trades.append(position)

                    if exit_reason == "Stop-Loss":
                        cooldown = 5
                    position  = None
                    hold_days = 0
                    continue

            if position is None and not pd.isna(atr) and atr > 0 and cooldown == 0:
                sig = rules_signal(df, i)
                if sig == "BUY":
                    entry_signals.append({
                        "ticker":     ticker,
                        "entry_date": date,
                        "df_idx":     i,
                        "df":         df,
                        "direction":  "BUY",
                        "entry":      close,
                        "stop":       close - ATR_MULT * atr,
                        "target":     close * (1 + TP_PCT),
                    })
                    position = entry_signals[-1]
                    hold_days = 0

    except Exception as e:
        print(f"  {ticker}: error — {e}")

# Separate completed trades from open (unfilled exits at period end)
completed = [t for t in all_trades if "exit_date" in t]
print(f"  Found {len(entry_signals)} total entry signals across {PILOT_TICKERS}")
print(f"  {len(completed)} trades completed within backtest window")

# ── Phase 2: call Claude model for each entry signal ──────────────────────────
print(f"\nPhase 2: Calling Claude model for {len(entry_signals)} entry signals...")
print("  (This will take a few minutes — ~3-5s per API call)\n")

for idx, sig in enumerate(entry_signals):
    ticker = sig["ticker"]
    date   = sig["entry_date"]
    i      = sig["df_idx"]
    df     = sig["df"]

    # Slice data up to and including the signal date (no lookahead)
    df_snapshot = df.iloc[:i + 1].copy()

    try:
        ind = compute_indicators(df_snapshot)
        model_result = model_get_signal(ticker, ind)
        sig["model_signal"]     = model_result.get("signal", "ERROR")
        sig["model_confidence"] = model_result.get("confidence_stars", 0)
        sig["model_rationale"]  = model_result.get("rationale", "")
        sig["model_agrees"]     = (model_result.get("signal") == sig["direction"])
        print(f"  [{idx+1}/{len(entry_signals)}] {ticker} {date.strftime('%Y-%m-%d')} "
              f"{sig['direction']} → model: {sig['model_signal']} "
              f"({sig['model_confidence']}★) agree={sig['model_agrees']}")
    except Exception as e:
        sig["model_signal"]     = "ERROR"
        sig["model_confidence"] = 0
        sig["model_agrees"]     = False
        sig["model_rationale"]  = str(e)
        print(f"  [{idx+1}/{len(entry_signals)}] {ticker} ERROR: {e}")

    time.sleep(0.5)   # light throttle

# ── Phase 3: attach model scores to completed trades ──────────────────────────
# Build lookup: (ticker, entry_date_str) → model score
score_lookup = {
    (s["ticker"], s["entry_date"].strftime("%Y-%m-%d")): s
    for s in entry_signals
}

for t in completed:
    key = (t["ticker"], t["entry_date"].strftime("%Y-%m-%d"))
    s   = score_lookup.get(key, {})
    t["model_signal"]     = s.get("model_signal", "N/A")
    t["model_confidence"] = s.get("model_confidence", 0)
    t["model_agrees"]     = s.get("model_agrees", False)
    t["model_rationale"]  = s.get("model_rationale", "")

# ── Phase 4: compare P&L at each confidence threshold ─────────────────────────
def pnl_stats(trades):
    if not trades:
        return {"count": 0, "win_rate": 0, "pf": 0, "total_pnl": 0}
    wins   = [t["pnl_dollar"] for t in trades if t["pnl_dollar"] > 0]
    losses = [t["pnl_dollar"] for t in trades if t["pnl_dollar"] <= 0]
    gross_win  = sum(wins)   if wins   else 0
    gross_loss = abs(sum(losses)) if losses else 1
    return {
        "count":    len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "pf":       round(gross_win / gross_loss, 2) if gross_loss else 0,
        "total_pnl": round(sum(t["pnl_dollar"] for t in trades), 2),
    }

tiers = {
    "All (rules only)":       completed,
    "Model agrees (any)":     [t for t in completed if t["model_agrees"]],
    "Confidence ≥ 3★":        [t for t in completed if t["model_confidence"] >= 3],
    "Confidence ≥ 4★":        [t for t in completed if t["model_confidence"] >= 4],
    "Confidence ≥ 5★":        [t for t in completed if t["model_confidence"] >= 5],
    "Model disagrees":        [t for t in completed if not t["model_agrees"] and t["model_signal"] != "N/A"],
}

# ── Write report ───────────────────────────────────────────────────────────────
DIV  = "=" * 80
DIV2 = "-" * 80
lines = []
lines.append(DIV)
lines.append("  SwingTrader — Model Confidence Backtest (Full)")
lines.append(f"  Tickers  : {len(PILOT_TICKERS)} watchlist stocks")
lines.append(f"  Period   : {START_DATE} → {END_DATE}")
lines.append(f"  Config   : ATR {ATR_MULT}× | TP {TP_PCT*100:.1f}% | MaxHold {MAX_HOLD}d")
lines.append(f"  Generated: {datetime.today().strftime('%Y-%m-%d')}")
lines.append(DIV)
lines.append("")

lines.append("CONFIDENCE FILTER COMPARISON")
lines.append(DIV2)
lines.append(f"  {'Filter':<28} {'Trades':>6}  {'Win%':>6}  {'PF':>5}  {'Total P&L':>12}")
lines.append(DIV2)
for label, subset in tiers.items():
    s = pnl_stats(subset)
    lines.append(f"  {label:<28} {s['count']:>6}  {s['win_rate']:>5.1f}%  {s['pf']:>5.2f}  ${s['total_pnl']:>+11,.2f}")
lines.append("")

lines.append("TRADE LOG (with model scores)")
lines.append(DIV2)
lines.append("")
for t in sorted(completed, key=lambda x: x["entry_date"]):
    agree_str = "AGREE" if t["model_agrees"] else "DISAGREE"
    lines.append(f"  {t['ticker']}  {t['direction']}  {t['entry_date'].strftime('%Y-%m-%d')} → {t['exit_date']}")
    lines.append(f"    Rules signal  : {t['direction']}")
    lines.append(f"    Model signal  : {t['model_signal']} ({t['model_confidence']}★)  [{agree_str}]")
    lines.append(f"    Model rationale: {t['model_rationale']}")
    lines.append(f"    Exit          : {t['exit_reason']}  ({t['hold_days']}d)")
    pnl_str = f"{t['pnl_pct']:+.2%}  /  ${t['pnl_dollar']:+,.2f}"
    lines.append(f"    P&L           : {pnl_str}")
    lines.append("")

lines.append(DIV)
lines.append("  End of Report")
lines.append(DIV)

with open(OUT_FILE, "w") as f:
    f.write("\n".join(lines))

# ── Write CSV ──────────────────────────────────────────────────────────────────
import csv
with open(CSV_FILE, "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow([
        "ticker", "direction", "entry_date", "entry_price",
        "exit_date", "exit_price", "exit_reason", "hold_days",
        "pnl_pct", "pnl_dollar",
        "model_signal", "model_confidence", "model_agrees", "model_rationale",
    ])
    for t in sorted(completed, key=lambda x: x["entry_date"]):
        writer.writerow([
            t["ticker"], t["direction"], t["entry_date"].strftime("%Y-%m-%d"), round(t["entry"], 2),
            t["exit_date"], t["exit_price"], t["exit_reason"], t["hold_days"],
            f"{t['pnl_pct']:+.4f}", round(t["pnl_dollar"], 2),
            t["model_signal"], t["model_confidence"], t["model_agrees"], t["model_rationale"],
        ])

print(f"\n✅  Report: {OUT_FILE}")
print(f"✅  CSV   : {CSV_FILE}")
print(f"\n{'='*50}")
print("CONFIDENCE FILTER RESULTS:")
print(f"{'='*50}")
for label, subset in tiers.items():
    s = pnl_stats(subset)
    print(f"  {label:<28} {s['count']:>4} trades | {s['win_rate']:>5.1f}% WR | PF {s['pf']:.2f} | ${s['total_pnl']:+,.2f}")
