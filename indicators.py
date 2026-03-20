import pandas as pd
import ta
from patterns import (detect_double_bottom, detect_inv_head_shoulders,
                      detect_cup_and_handle, detect_head_shoulders,
                      detect_double_top, detect_support_resistance,
                      get_historical_outcomes)


def compute_indicators(df: pd.DataFrame) -> dict:
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

    # 50-day and 200-day moving averages
    ma50 = round(close.rolling(50).mean().iloc[-1], 2) if len(close) >= 50 else None
    ma200 = round(close.rolling(200).mean().iloc[-1], 2) if len(close) >= 200 else None

    golden_cross = (ma50 is not None and ma200 is not None and ma50 > ma200)

    # ATR (14-period) — used for stop-loss sizing
    atr_series = ta.volatility.AverageTrueRange(
        high=df["High"], low=df["Low"], close=df["Close"], window=14
    ).average_true_range()
    atr14 = round(atr_series.iloc[-1], 2)

    # Volume metrics
    volume = df["Volume"]
    vol_today  = int(volume.iloc[-1])
    vol_avg20  = int(volume.rolling(20).mean().iloc[-1]) if len(volume) >= 20 else vol_today
    rel_vol    = round(vol_today / vol_avg20, 2) if vol_avg20 > 0 else 1.0
    vol_3d_avg = volume.iloc[-3:].mean()
    vol_trend  = "rising" if vol_3d_avg > vol_avg20 else "falling"

    # Chart patterns — only currently forming (rightmost structural point ≤ 40 bars ago)
    ACTIVE_BARS = 40
    patterns = []
    pattern_outcomes = {}
    for detect_fn in [detect_double_bottom, detect_inv_head_shoulders, detect_cup_and_handle,
                      detect_head_shoulders, detect_double_top]:
        matches = detect_fn(df)
        active = [m for m in matches if m.get("recency_bars", 999) <= ACTIVE_BARS]
        if active:
            pat = active[0]
            patterns.append(pat)
            outcomes = get_historical_outcomes(df, pat["pattern"])
            if outcomes.get("total", 0) >= 1:
                pattern_outcomes[pat["pattern"]] = outcomes
    sr_levels = detect_support_resistance(df)

    return {
        "rsi": rsi,
        "rsi_label": rsi_label,
        "macd_line": round(macd_line, 4),
        "macd_signal": round(signal_line, 4),
        "macd_histogram": round(macd_histogram, 4),
        "macd_crossover": macd_crossover,
        "latest_close": round(float(close.iloc[-1]), 2),
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
        "week52_high": week52_high,
        "week52_high_date": week52_high_date,
        "week52_low": week52_low,
        "week52_low_date": week52_low_date,
        "ma50": ma50,
        "ma200": ma200,
        "golden_cross": golden_cross,
        "atr14": atr14,
        "vol_today": vol_today,
        "vol_avg20": vol_avg20,
        "rel_vol": rel_vol,
        "vol_trend": vol_trend,
        "patterns": patterns,
        "pattern_outcomes": pattern_outcomes,
        "sr_levels": sr_levels,
    }


if __name__ == "__main__":
    from fetcher import fetch_price_data
    ticker = "AAPL"
    print(f"Computing indicators for {ticker}...")
    df = fetch_price_data(ticker)
    result = compute_indicators(df)
    for k, v in result.items():
        print(f"  {k}: {v}")
