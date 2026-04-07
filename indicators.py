import pandas as pd
import ta
import yfinance as yf
from datetime import datetime
from patterns import (detect_double_bottom, detect_inv_head_shoulders,
                      detect_cup_and_handle, detect_head_shoulders,
                      detect_double_top, detect_support_resistance,
                      get_historical_outcomes)


def fetch_earnings_date(ticker: str) -> dict:
    """Return the next earnings date and days until it for a given ticker."""
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return {"earnings_date": None, "days_to_earnings": None}
        dates = cal.get("Earnings Date", []) if isinstance(cal, dict) else []
        today = datetime.now().date()
        future = []
        for d in dates:
            d = d.date() if hasattr(d, "date") else datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
            if d >= today:
                future.append(d)
        if not future:
            return {"earnings_date": None, "days_to_earnings": None}
        nearest = min(future)
        return {"earnings_date": nearest.strftime("%Y-%m-%d"), "days_to_earnings": (nearest - today).days}
    except Exception:
        return {"earnings_date": None, "days_to_earnings": None}


def compute_indicators(df: pd.DataFrame, ticker: str | None = None) -> dict:
    """
    Compute RSI and MACD for a given price DataFrame.
    Returns a dict of the latest indicator values.
    """
    close = df["Close"]

    # RSI (14-period)
    rsi_series = ta.momentum.RSIIndicator(close=close, window=14).rsi()
    rsi = round(rsi_series.iloc[-1], 2)

    # MACD
    macd_obj = ta.trend.MACD(close=close)
    macd_line = macd_obj.macd().iloc[-1]
    signal_line = macd_obj.macd_signal().iloc[-1]
    macd_histogram = macd_obj.macd_diff().iloc[-1]

    # MACD crossover detection
    macd_prev = macd_obj.macd().iloc[-2]
    signal_prev = macd_obj.macd_signal().iloc[-2]
    macd_crossover = "bullish" if (macd_prev < signal_prev and macd_line > signal_line) else \
                     "bearish" if (macd_prev > signal_prev and macd_line < signal_line) else \
                     "none"

    # RSI label
    if rsi < 30:
        rsi_label = "Oversold"
    elif rsi > 70:
        rsi_label = "Overbought"
    else:
        rsi_label = "Neutral"

    # 52-week high / low (use available history, up to 252 trading days)
    lookback = df.tail(252)
    week52_high = round(lookback["High"].max(), 2)
    week52_low = round(lookback["Low"].min(), 2)
    week52_high_date = lookback["High"].idxmax().strftime("%b %d, %Y")
    week52_low_date = lookback["Low"].idxmin().strftime("%b %d, %Y")

    # Moving averages
    ma20 = round(close.rolling(20).mean().iloc[-1], 2) if len(close) >= 20 else None
    ma50 = round(close.rolling(50).mean().iloc[-1], 2) if len(close) >= 50 else None
    ma200 = round(close.rolling(200).mean().iloc[-1], 2) if len(close) >= 200 else None
    high20 = round(df["High"].rolling(20).max().iloc[-1], 2) if len(close) >= 20 else None

    golden_cross = (ma50 is not None and ma200 is not None and ma50 > ma200)

    # ATR (14-period) — used for stop-loss sizing
    atr_series = ta.volatility.AverageTrueRange(
        high=df["High"], low=df["Low"], close=df["Close"], window=14
    ).average_true_range()
    atr14 = round(atr_series.iloc[-1], 2)

    # Volume metrics
    volume = df["Volume"]
    _vol_last = volume.iloc[-1]
    vol_today  = int(_vol_last) if pd.notna(_vol_last) else 0
    _vol_avg_raw = volume.rolling(20).mean().iloc[-1] if len(volume) >= 20 else None
    vol_avg20  = int(_vol_avg_raw) if _vol_avg_raw is not None and pd.notna(_vol_avg_raw) else vol_today
    rel_vol    = round(vol_today / vol_avg20, 2) if vol_avg20 > 0 else 1.0
    vol_3d_avg = volume.iloc[-3:].mean()
    vol_trend  = "rising" if vol_3d_avg > vol_avg20 else "falling"

    # Bollinger Bands (20-period, 2 std dev)
    bb_mid_series = close.rolling(20).mean()
    bb_std_series = close.rolling(20).std()
    bb_upper = round((bb_mid_series + 2 * bb_std_series).iloc[-1], 2) if len(close) >= 20 else None
    bb_lower = round((bb_mid_series - 2 * bb_std_series).iloc[-1], 2) if len(close) >= 20 else None
    bb_middle = round(bb_mid_series.iloc[-1], 2) if len(close) >= 20 else None
    _bb_range = (bb_upper - bb_lower) if (bb_upper and bb_lower and bb_upper != bb_lower) else None
    bb_pct = round((float(close.iloc[-1]) - bb_lower) / _bb_range, 3) if _bb_range else None

    # Chart patterns — only currently forming (rightmost structural point ≤ 40 bars ago)
    ACTIVE_BARS = 40
    patterns = []
    pattern_outcomes = {}
    try:
        for detect_fn in [detect_double_bottom, detect_inv_head_shoulders, detect_cup_and_handle,
                          detect_head_shoulders, detect_double_top]:
            try:
                matches = detect_fn(df)
                active = [m for m in matches if m.get("recency_bars", 999) <= ACTIVE_BARS]
                if active:
                    pat = active[0]
                    patterns.append(pat)
                    outcomes = get_historical_outcomes(df, pat["pattern"])
                    if outcomes.get("total", 0) >= 1:
                        pattern_outcomes[pat["pattern"]] = outcomes
            except Exception:
                pass
    except Exception:
        pass
    try:
        sr_levels = detect_support_resistance(df)
    except Exception:
        sr_levels = []

    earnings = fetch_earnings_date(ticker) if ticker else {"earnings_date": None, "days_to_earnings": None}

    # Previous-bar values — needed for rule-based signal engine
    prev_close     = round(float(close.iloc[-2]), 2) if len(close) >= 2 else None
    prev2_close    = round(float(close.iloc[-3]), 2) if len(close) >= 3 else None
    prev_macd_hist = round(macd_obj.macd_diff().iloc[-2], 4) if len(close) >= 2 else None
    prev_ma50      = round(close.rolling(50).mean().iloc[-2], 2) if len(close) >= 51 else None
    prev_rsi       = round(rsi_series.iloc[-2], 2) if len(close) >= 2 else None
    prev2_rsi      = round(rsi_series.iloc[-3], 2) if len(close) >= 3 else None

    # ── Engine 4: SPY Regime indicators (SMA RSI, 20d momentum, breach tracking) ──
    # SMA-based RSI (rolling mean, not EMA) — matches backtest_master.py Engine 4
    delta_e4 = close.diff()
    gain_e4  = (delta_e4.where(delta_e4 > 0, 0)).rolling(window=14).mean()
    loss_e4  = (-delta_e4.where(delta_e4 < 0, 0)).rolling(window=14).mean()
    rs_e4    = gain_e4 / loss_e4.replace(0, float("nan"))
    sma_rsi_series = (100 - (100 / (1 + rs_e4)))
    sma_rsi = round(float(sma_rsi_series.iloc[-1]), 2) if len(close) >= 14 else None

    # 20-day price momentum (for regime entry filter: >= 2%)
    ret20 = round(float(close.pct_change(20).iloc[-1]), 4) if len(close) >= 21 else None

    # Consecutive days SPY has closed below MA50 (for 5-day breach / regime exit)
    ma50_series = close.rolling(50).mean()
    ma50_breach_days = 0
    if len(close) >= 50:
        for i in range(1, min(11, len(close))):
            if close.iloc[-i] < ma50_series.iloc[-i]:
                ma50_breach_days += 1
            else:
                break

    # 10-day minimum of SMA RSI (pullback filter for Engine 4 entry)
    sma_rsi_10d_min = None
    if sma_rsi_series is not None and len(sma_rsi_series.dropna()) >= 10:
        sma_rsi_10d_min = round(float(sma_rsi_series.iloc[-10:].min()), 2)

    return {
        "rsi": rsi,
        "rsi_label": rsi_label,
        "macd_line": round(macd_line, 4),
        "macd_signal": round(signal_line, 4),
        "macd_histogram": round(macd_histogram, 4),
        "macd_crossover": macd_crossover,
        "prev_macd_hist": prev_macd_hist,
        "latest_close": round(float(close.iloc[-1]), 2),
        "latest_open": round(float(df["Open"].iloc[-1]), 2),
        "latest_low": round(float(df["Low"].iloc[-1]), 2),
        "prev_close": prev_close,
        "prev2_close": prev2_close,
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
        "week52_high": week52_high,
        "week52_high_date": week52_high_date,
        "week52_low": week52_low,
        "week52_low_date": week52_low_date,
        "ma20": ma20,
        "ma50": ma50,
        "high20": high20,
        "prev_ma50": prev_ma50,
        "ma200": ma200,
        "golden_cross": golden_cross,
        "prev_rsi": prev_rsi,
        "prev2_rsi": prev2_rsi,
        "atr14": atr14,
        "vol_today": vol_today,
        "vol_avg20": vol_avg20,
        "rel_vol": rel_vol,
        "vol_trend": vol_trend,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
        "bb_pct": bb_pct,
        "patterns": patterns,
        "pattern_outcomes": pattern_outcomes,
        "sr_levels": sr_levels,
        "earnings_date": earnings["earnings_date"],
        "days_to_earnings": earnings["days_to_earnings"],
        # Engine 4 regime indicators
        "sma_rsi": sma_rsi,
        "ret20": ret20,
        "ma50_breach_days": ma50_breach_days,
        "sma_rsi_10d_min": sma_rsi_10d_min,
    }


if __name__ == "__main__":
    from fetcher import fetch_price_data
    ticker = "AAPL"
    print(f"Computing indicators for {ticker}...")
    df = fetch_price_data(ticker)
    result = compute_indicators(df)
    for k, v in result.items():
        print(f"  {k}: {v}")
