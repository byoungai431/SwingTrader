"""
Chart Analyzer — Pattern Backtest Validation
Walk-forward backtest over 2020-2026 on top-100 S&P 500 tickers.
PF gate: ≥ 1.25 per pattern.

Run:  python -m chart_analyzer.backtest
"""

from __future__ import annotations


import numpy as np
import pandas as pd
import yfinance as yf
from scipy.signal import argrelextrema
from datetime import datetime

from chart_analyzer.history import write_backtest_stats

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

BACKTEST_START = "2020-01-01"
BACKTEST_END   = "2026-01-01"
DATE_RANGE     = f"{BACKTEST_START} → {BACKTEST_END}"
PF_GATE        = 1.25
MAX_HOLD       = 40        # bars
COMMISSION     = 0.001     # 0.1% per side
COOLDOWN       = 40        # bars between signals on same ticker
BATCH_SIZE     = 50


# ─────────────────────────────────────────────────────────────────────────────
# Shared backtest simulation
# ─────────────────────────────────────────────────────────────────────────────

def _simulate_trades(
    signal_indices: list,
    df: pd.DataFrame,
    stop_col: str = "stop",
    target_col: str = "target",
) -> list:
    """
    Given a list of (bar_index, stop_price, target_price) tuples from detection,
    simulate trades with priority: stop (vs daily low) → target (vs daily high)
    → max-hold exit at close.  Applies COOLDOWN bars between signals.
    Returns list of trade dicts.
    """
    trades = []
    last_exit_bar = -COOLDOWN - 1

    close  = df["Close"].values
    high   = df["High"].values
    low    = df["Low"].values
    n      = len(df)

    for entry_bar, stop, target in signal_indices:
        if entry_bar - last_exit_bar < COOLDOWN:
            continue
        if entry_bar + 1 >= n:
            continue

        entry_price = close[entry_bar] * (1 + COMMISSION)
        exit_price  = None
        exit_bar    = None
        outcome     = None

        for bar in range(entry_bar + 1, min(entry_bar + MAX_HOLD + 1, n)):
            if low[bar] <= stop:
                exit_price = stop * (1 - COMMISSION)
                exit_bar   = bar
                outcome    = "stop"
                break
            if high[bar] >= target:
                exit_price = target * (1 - COMMISSION)
                exit_bar   = bar
                outcome    = "target"
                break
        else:
            exit_bar   = min(entry_bar + MAX_HOLD, n - 1)
            exit_price = close[exit_bar] * (1 - COMMISSION)
            outcome    = "maxhold"

        pnl = (exit_price - entry_price) / entry_price
        trades.append(
            {
                "entry_bar":   entry_bar,
                "exit_bar":    exit_bar,
                "entry_price": entry_price,
                "exit_price":  exit_price,
                "stop":        stop,
                "target":      target,
                "pnl":         pnl,
                "outcome":     outcome,
                "bars_held":   exit_bar - entry_bar,
            }
        )
        last_exit_bar = exit_bar

    return trades


def _compute_stats(trades: list, pattern_type: str) -> dict:
    if not trades:
        return {
            "pattern_type":       pattern_type,
            "profit_factor":      0.0,
            "win_rate":           0.0,
            "avg_bars_to_target": 0,
            "sample_size":        0,
            "date_range":         DATE_RANGE,
            "passed":             False,
        }

    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    wins         = [t for t in trades if t["pnl"] > 0]
    win_rate     = len(wins) / len(trades)
    avg_bars     = int(np.mean([t["bars_held"] for t in trades]))

    return {
        "pattern_type":       pattern_type,
        "profit_factor":      round(pf, 3),
        "win_rate":           round(win_rate, 4),
        "avg_bars_to_target": avg_bars,
        "sample_size":        len(trades),
        "date_range":         DATE_RANGE,
        "passed":             pf >= PF_GATE,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pattern 1 — Inverse Head & Shoulders
# ─────────────────────────────────────────────────────────────────────────────

def _detect_inv_hns(close: np.ndarray, high: np.ndarray, low: np.ndarray, volume: np.ndarray) -> list:
    """
    Returns list of (signal_bar, stop, target) tuples.
    Criteria:
    - Three local minima via argrelextrema(order=10)
    - Head is deepest; shoulders within 5% of each other
    - Neckline drawn from two inter-swing peaks
    - Breakout: first bar where close crosses above neckline with vol ≥ 1.2x 20-bar avg
    - Stop: 2% below right shoulder low; Target: neckline + (neckline − head)
    """
    signals = []
    n = len(close)
    vol_ma20 = pd.Series(volume).rolling(20).mean().values

    local_mins = argrelextrema(low, np.less, order=10)[0]
    if len(local_mins) < 3:
        return signals

    for i in range(len(local_mins) - 2):
        ls_idx, h_idx, rs_idx = local_mins[i], local_mins[i + 1], local_mins[i + 2]
        ls_low, h_low, rs_low = low[ls_idx], low[h_idx], low[rs_idx]

        # Head must be deepest
        if not (h_low < ls_low and h_low < rs_low):
            continue

        # Shoulders within 5% of each other
        if abs(ls_low - rs_low) / ls_low > 0.05:
            continue

        # Neckline from peaks between LS→H and H→RS
        mid1_segment = high[ls_idx:h_idx]
        mid2_segment = high[h_idx:rs_idx]
        if len(mid1_segment) == 0 or len(mid2_segment) == 0:
            continue
        peak1_bar = ls_idx + int(np.argmax(mid1_segment))
        peak2_bar = h_idx  + int(np.argmax(mid2_segment))
        neckline  = (high[peak1_bar] + high[peak2_bar]) / 2

        # Scan for breakout after RS
        for bar in range(rs_idx + 1, min(rs_idx + 60, n)):
            if vol_ma20[bar] == 0 or np.isnan(vol_ma20[bar]):
                continue
            if close[bar] > neckline and volume[bar] >= vol_ma20[bar] * 1.2:
                stop   = rs_low * 0.98
                target = neckline + (neckline - h_low)
                if target > close[bar] * 1.01:  # target must be meaningful
                    signals.append((bar, stop, target))
                break  # one signal per pattern instance

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Pattern 2 — Ascending Triangle
# ─────────────────────────────────────────────────────────────────────────────

def _detect_asc_triangle(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    spy_above_ma50: np.ndarray,
) -> list:
    """
    Criteria:
    - Flat resistance: ≥3 local highs within 2.5% of each other
    - Rising lows: ≥3 local lows forming upward slope
    - SPY > MA50 at breakout bar
    - Breakout: close above resistance with vol ≥ 1.3x 20-bar avg
    - Stop: below last rising trendline touch; Target: resistance + (resistance - first low)
    """
    signals = []
    n = len(close)
    vol_ma20 = pd.Series(volume).rolling(20).mean().values

    local_highs = argrelextrema(high, np.greater, order=5)[0]
    local_lows  = argrelextrema(low,  np.less,    order=5)[0]

    if len(local_highs) < 3 or len(local_lows) < 3:
        return signals

    # Slide a 60-bar window across the series
    for end in range(60, n):
        start = max(0, end - 120)

        # Local highs in window
        lh_in = local_highs[(local_highs >= start) & (local_highs < end)]
        if len(lh_in) < 3:
            continue

        # Flat resistance: pick last 3 highs and check they're within 2.5%
        h3 = lh_in[-3:]
        h_vals = high[h3]
        resistance = np.max(h_vals)
        if (resistance - np.min(h_vals)) / resistance > 0.025:
            continue

        # Local lows in window rising
        ll_in = local_lows[(local_lows >= start) & (local_lows < end)]
        if len(ll_in) < 3:
            continue

        ll3 = ll_in[-3:]
        l_vals = low[ll3]
        if not (l_vals[-1] > l_vals[-2] > l_vals[0]):
            continue

        # SPY regime gate
        if not spy_above_ma50[end]:
            continue

        # Breakout bar
        if vol_ma20[end] == 0 or np.isnan(vol_ma20[end]):
            continue
        if close[end] > resistance and volume[end] >= vol_ma20[end] * 1.3:
            stop   = l_vals[-1] * 0.99
            target = resistance + (resistance - l_vals[0])
            if target > close[end] * 1.01:
                signals.append((end, stop, target))

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Pattern 3 — Cup & Handle
# ─────────────────────────────────────────────────────────────────────────────

def _detect_cup_handle(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    spy_above_ma50: np.ndarray,
) -> list:
    """
    Criteria:
    - Left rim: local high
    - Cup drop ≥12%; recovery to within 5% of left rim
    - Handle: 3-15 bars, ≤35% retrace of cup rise, declining volume
    - Breakout: close above right rim with vol ≥ 1.3x 20-bar avg, SPY gate
    - Stop: below handle low; Target: right rim + cup depth
    """
    signals = []
    n = len(close)
    vol_ma20 = pd.Series(volume).rolling(20).mean().values

    local_highs = argrelextrema(high, np.greater, order=8)[0]

    for li, left_rim_idx in enumerate(local_highs[:-1]):
        left_rim = high[left_rim_idx]

        # Cup bottom: deepest low after left rim within 60-100 bars
        cup_end = min(left_rim_idx + 100, n - 1)
        cup_segment_low = low[left_rim_idx:cup_end]
        if len(cup_segment_low) < 10:
            continue
        cup_bottom_offset = int(np.argmin(cup_segment_low))
        cup_bottom_idx = left_rim_idx + cup_bottom_offset
        cup_bottom = low[cup_bottom_idx]

        # Cup depth ≥ 12%
        if (left_rim - cup_bottom) / left_rim < 0.12:
            continue

        # Right rim: recovery to within 5% of left rim
        right_segment = close[cup_bottom_idx:cup_end]
        if len(right_segment) < 5:
            continue
        right_rim_offset = next(
            (j for j, c in enumerate(right_segment) if c >= left_rim * 0.95),
            None,
        )
        if right_rim_offset is None:
            continue
        right_rim_idx = cup_bottom_idx + right_rim_offset
        right_rim = close[right_rim_idx]

        # Handle: 3–15 bars of consolidation after right rim
        handle_end_max = min(right_rim_idx + 15, n - 1)
        for handle_len in range(3, handle_end_max - right_rim_idx + 1):
            handle_end = right_rim_idx + handle_len
            if handle_end >= n:
                break

            handle_low  = np.min(low[right_rim_idx:handle_end])
            handle_high = np.max(high[right_rim_idx:handle_end])
            cup_rise    = right_rim - cup_bottom

            # Handle retrace ≤ 35% of cup rise
            handle_retrace = (right_rim - handle_low) / cup_rise if cup_rise > 0 else 1
            if handle_retrace > 0.35:
                continue

            # Declining volume in handle
            pre_vol  = np.mean(volume[cup_bottom_idx:right_rim_idx]) if right_rim_idx > cup_bottom_idx else 1
            hdl_vol  = np.mean(volume[right_rim_idx:handle_end])
            if hdl_vol >= pre_vol:
                continue

            # Breakout
            bar = handle_end
            if bar >= n:
                break
            if vol_ma20[bar] == 0 or np.isnan(vol_ma20[bar]):
                continue
            if not spy_above_ma50[bar]:
                continue
            if close[bar] > right_rim and volume[bar] >= vol_ma20[bar] * 1.3:
                cup_depth = left_rim - cup_bottom
                stop      = handle_low * 0.99
                target    = right_rim + cup_depth
                if target > close[bar] * 1.01:
                    signals.append((bar, stop, target))
                break  # one signal per left_rim anchor

        # break after first valid signal from this left rim
        if signals and signals[-1][0] >= right_rim_idx:
            continue

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Pattern 4 — Bull Flag
# ─────────────────────────────────────────────────────────────────────────────

def _detect_bull_flag(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    spy_above_ma50: np.ndarray,
) -> list:
    """
    Criteria:
    - Pole: ≥10% gain in ≤8 bars
    - Flag: 3–8 bars tight consolidation (range ≤5% of flag-open), vol dried up ≥40%
    - Breakout: close above flag high with vol ≥ 1.3x 20-bar avg, SPY gate
    - Stop: below flag low; Target: pole length from pole low
    """
    signals = []
    n = len(close)
    vol_ma20 = pd.Series(volume).rolling(20).mean().values

    for pole_start in range(n - 16):
        # Find pole: ≥10% move in ≤8 bars
        for pole_len in range(2, 9):
            pole_end = pole_start + pole_len
            if pole_end >= n:
                break
            pole_gain = (close[pole_end] - close[pole_start]) / close[pole_start]
            if pole_gain < 0.10:
                continue

            pole_low = close[pole_start]

            # Flag: 3–8 bars consolidation after pole
            for flag_len in range(3, 9):
                flag_end = pole_end + flag_len
                if flag_end >= n:
                    break

                flag_segment_high = high[pole_end:flag_end]
                flag_segment_low  = low[pole_end:flag_end]
                flag_high = np.max(flag_segment_high)
                flag_low  = np.min(flag_segment_low)
                flag_range = (flag_high - flag_low) / close[pole_end]

                # Tight consolidation
                if flag_range > 0.05:
                    continue

                # Volume dried up ≥40% vs pole
                pole_vol = np.mean(volume[pole_start:pole_end + 1])
                flag_vol = np.mean(volume[pole_end:flag_end])
                if pole_vol == 0 or flag_vol / pole_vol > 0.60:
                    continue

                # Breakout bar
                bar = flag_end
                if bar >= n:
                    break
                if vol_ma20[bar] == 0 or np.isnan(vol_ma20[bar]):
                    continue
                if not spy_above_ma50[bar]:
                    continue
                if close[bar] > flag_high and volume[bar] >= vol_ma20[bar] * 1.3:
                    pole_length = close[pole_end] - pole_low
                    stop   = flag_low * 0.99
                    target = pole_low + pole_length * 2
                    if target > close[bar] * 1.01:
                        signals.append((bar, stop, target))
                    break  # one signal per pole_start
            else:
                continue
            break  # found pole, move to next pole_start

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Pattern 5 — Falling Wedge
# ─────────────────────────────────────────────────────────────────────────────

def _detect_falling_wedge(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
) -> list:
    """
    Criteria:
    - Descending local highs with linear regression slope < 0
    - Descending local lows with slope < 0, but less steep than highs (converging)
    - ≥3 touches each trendline; wedge compressed to ≤40% initial width
    - Breakout: close above upper trendline with vol ≥ 1.2x 20-bar avg
    - Stop: below recent swing low; Target: top of wedge start
    - No SPY regime gate (reversal pattern)
    """
    signals = []
    n = len(close)
    vol_ma20 = pd.Series(volume).rolling(20).mean().values

    local_highs = argrelextrema(high, np.greater, order=5)[0]
    local_lows  = argrelextrema(low,  np.less,    order=5)[0]

    # Slide 60-bar window
    for end in range(30, n):
        start = max(0, end - 80)

        lh_in = local_highs[(local_highs >= start) & (local_highs < end)]
        ll_in = local_lows[(local_lows  >= start) & (local_lows  < end)]
        if len(lh_in) < 3 or len(ll_in) < 3:
            continue

        # Linear regressions on local highs and lows
        lh_x = lh_in.astype(float)
        lh_y = high[lh_in]
        ll_x = ll_in.astype(float)
        ll_y = low[ll_in]

        slope_h, intercept_h = np.polyfit(lh_x, lh_y, 1)
        slope_l, intercept_l = np.polyfit(ll_x, ll_y, 1)

        # Both trendlines must slope downward
        if slope_h >= 0 or slope_l >= 0:
            continue

        # Lows slope must be less negative than highs (converging wedge)
        if not (slope_l > slope_h):
            continue

        # Width at start vs end
        width_start = (intercept_h + slope_h * start) - (intercept_l + slope_l * start)
        width_end   = (intercept_h + slope_h * end)   - (intercept_l + slope_l * end)
        if width_start <= 0:
            continue
        if width_end / width_start > 0.40:  # must be compressed to ≤40%
            continue

        # Upper trendline value at breakout bar
        upper_tl = intercept_h + slope_h * end
        if vol_ma20[end] == 0 or np.isnan(vol_ma20[end]):
            continue
        if close[end] > upper_tl and volume[end] >= vol_ma20[end] * 1.2:
            wedge_top  = intercept_h + slope_h * start
            stop       = np.min(low[max(0, end - 10):end]) * 0.99
            target     = wedge_top
            if target > close[end] * 1.01:
                signals.append((end, stop, target))

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Main backtest runner
# ─────────────────────────────────────────────────────────────────────────────

def run(tickers: list | None = None):
    """Full walk-forward backtest. Downloads data, detects patterns, simulates trades."""
    from chart_analyzer.universe import get_universe

    if tickers is None:
        tickers = get_universe()

    print("\n" + "=" * 54)
    print("Chart Analyzer — Pattern Backtest Validation")
    print(f"Universe: top-{len(tickers)} S&P 500 | {DATE_RANGE}")
    print("=" * 54)

    # Download SPY for regime gate
    print("Downloading SPY data for regime filter...")
    spy_raw   = yf.download("SPY", start=BACKTEST_START, end=BACKTEST_END,
                            auto_adjust=True, progress=False)
    spy_close = spy_raw["Close"].values
    spy_ma50  = pd.Series(spy_close).rolling(50).mean().values
    spy_above = (spy_close > spy_ma50)
    spy_dates = spy_raw.index

    # Collect per-ticker trade lists
    all_trades: dict[str, list] = {
        "InvHnS":      [],
        "AscTriangle": [],
        "CupHandle":   [],
        "BullFlag":    [],
        "FallingWedge":[],
    }

    n_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_num in range(n_batches):
        batch = tickers[batch_num * BATCH_SIZE : (batch_num + 1) * BATCH_SIZE]
        print(f"  Batch {batch_num + 1}/{n_batches}: {len(batch)} tickers...")
        raw = yf.download(
            batch,
            start=BACKTEST_START,
            end=BACKTEST_END,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw.empty:
            continue

        for ticker in batch:
            try:
                if len(batch) == 1:
                    df = raw.copy()
                else:
                    df = raw.xs(ticker, axis=1, level=1).copy()

                df = df.dropna(subset=["Close", "High", "Low", "Volume"])
                if len(df) < 100:
                    continue

                spy_series  = pd.Series(spy_above, index=spy_dates)
                spy_aligned = spy_series.reindex(df.index, method="ffill").fillna(False).values

                c  = df["Close"].values.astype(float)
                h  = df["High"].values.astype(float)
                lo = df["Low"].values.astype(float)
                v  = df["Volume"].values.astype(float)

                # Detect signals (returns list of (bar_idx, stop, target))
                ihs_signals = _detect_inv_hns(c, h, lo, v)
                asc_signals = _detect_asc_triangle(c, h, lo, v, spy_aligned)
                cup_signals = _detect_cup_handle(c, h, lo, v, spy_aligned)
                flg_signals = _detect_bull_flag(c, h, lo, v, spy_aligned)
                wdg_signals = _detect_falling_wedge(c, h, lo, v)

                # Simulate using real OHLC
                all_trades["InvHnS"]      += _simulate_trades(ihs_signals, df)
                all_trades["AscTriangle"] += _simulate_trades(asc_signals, df)
                all_trades["CupHandle"]   += _simulate_trades(cup_signals, df)
                all_trades["BullFlag"]    += _simulate_trades(flg_signals, df)
                all_trades["FallingWedge"]+= _simulate_trades(wdg_signals, df)

            except Exception:
                continue

    # Compute and print stats
    print("\n" + "=" * 54)
    print("Results:")
    print("=" * 54)

    pattern_labels = {
        "InvHnS":      "Inv Head & Shoulders",
        "AscTriangle": "Ascending Triangle  ",
        "CupHandle":   "Cup & Handle        ",
        "BullFlag":    "Bull Flag           ",
        "FallingWedge":"Falling Wedge       ",
    }

    pattern_stats = []
    for key, label in pattern_labels.items():
        stats = _compute_stats(all_trades[key], key)
        pattern_stats.append(stats)
        gate  = "✅" if stats["passed"] else "❌"
        print(
            f"  {label} | PF {stats['profit_factor']:.2f} {gate}"
            f" | WR {stats['win_rate']*100:.1f}%"
            f" | n={stats['sample_size']:<5}"
            f" | avg {stats['avg_bars_to_target']}d"
        )

    passed = [s for s in pattern_stats if s["passed"]]
    failed = [s["pattern_type"] for s in pattern_stats if not s["passed"]]
    print("=" * 54)
    suffix = f"  FAILED: {', '.join(failed)}" if failed else ""
    print(f"PASSED: {len(passed)}/5{suffix}")
    print("=" * 54)

    write_backtest_stats(pattern_stats)
    print("\nResults written to pattern_backtest_stats table.")
    return pattern_stats


if __name__ == "__main__":
    run()
