"""
signal_engine.py — Rule-based signal generator mirroring the SC12 backtest.

Entry logic (no Claude API):
  BUY  : ≥3 of 4 conditions + 2 consecutive green candles + price above MA200
         1. RSI < 40            (oversold depth)
         2. MACD histogram crosses above 0   (momentum reversal)
         3. Close reclaimed MA50 from below  (pullback bounce)
         4. Relative volume ≥ 1.5x           (institutional participation)
  SELL : ≥2 of 3 conditions + 2 consecutive red candles + price below MA200
         1. RSI > 65
         2. MACD histogram crosses below 0
         3. Death cross (MA50 < MA200)

Confidence tiers:
  6  (5★ MAX) : RSI < 25 AND price ≤ lower Bollinger Band
  5  (5★)     : RSI < 25
  4  (4★)     : ≥3 BUY conditions met + RSI bonus (RSI < 40 in last 3 bars)
  <4           : dropped by run_daily.py — never saved or notified

Engine 2: Index Fade
  BUY  : Base RSI ≥ 80 AND price ≥ BB Upper (both required). RSI ≥ 85 → 5★.

Engine 3: Leveraged Shock-Bounce
  BUY  : Base RSI < 25 (5★) or RSI < 30 + Relative Vol ≥ 1.5x (4★). Inverse/Leveraged mapped.
"""


# ── Take-profit targets (mirrors backtest TP_PCT) ─────────────────────────────
TP_PCT_4STAR     = 0.20   # 4★ BUY target: +20%
TP_PCT_5STAR     = 0.25   # 5★ / 5★ MAX BUY target: +25%
ATR_STOP_MULT    = 2.0    # Stop = entry ± ATR_STOP_MULT × ATR14
RSI_ENTRY_MAX    = 40     # RSI must be below this for condition 1
RSI_5STAR_ENTRY  = 25     # RSI below this → 5★ auto-upgrade
VOL_SPIKE_MIN    = 1.5    # Minimum relative volume for condition 4


def get_signal(ticker: str, indicators: dict, fundamentals: dict | None = None) -> dict:
    """
    Compute a rule-based trading signal.  Returns a dict compatible with the
    original Claude-based interface (same keys, same confidence_stars scale).
    """
    # ── Extract indicators ────────────────────────────────────────────────────
    rsi         = indicators.get("rsi")
    macd_hist   = indicators.get("macd_histogram")
    prev_mh     = indicators.get("prev_macd_hist")
    close       = indicators.get("latest_close")
    prev_close  = indicators.get("prev_close")
    prev2_close = indicators.get("prev2_close")
    ma50        = indicators.get("ma50")
    prev_ma50   = indicators.get("prev_ma50")
    ma200       = indicators.get("ma200")
    rel_vol     = indicators.get("rel_vol")
    bb_lower    = indicators.get("bb_lower")
    atr14       = indicators.get("atr14")
    prev_rsi    = indicators.get("prev_rsi")
    prev2_rsi   = indicators.get("prev2_rsi")

    _NO_TRADE = {
        "ticker": ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "Insufficient indicator data.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if any(v is None for v in [rsi, macd_hist, prev_mh, close, prev_close,
                                prev2_close, ma50, prev_ma50, ma200, atr14]):
        return _NO_TRADE

    # ── Regime ────────────────────────────────────────────────────────────────
    bull_regime = close > ma200
    bear_regime = close < ma200

    # ── 2-candle confirmation ─────────────────────────────────────────────────
    two_green = (close > prev_close) and (prev_close > prev2_close)
    two_red   = (close < prev_close) and (prev_close < prev2_close)

    # ── BUY conditions ────────────────────────────────────────────────────────
    cond_rsi  = rsi < RSI_ENTRY_MAX
    cond_macd = (macd_hist > 0) and (prev_mh <= 0)                     # histogram crosses above 0
    cond_ma50 = (close > ma50) and (prev_close <= prev_ma50 * 1.01)    # reclaimed MA50 from below
    cond_vol  = (rel_vol is not None) and (rel_vol >= VOL_SPIKE_MIN)

    buys_count = sum([cond_rsi, cond_macd, cond_ma50, cond_vol])

    # ── SELL conditions ───────────────────────────────────────────────────────
    cond_overbought = rsi > 65
    cond_macd_sell  = (macd_hist < 0) and (prev_mh >= 0)               # histogram crosses below 0
    cond_death      = ma50 < ma200

    sells_count = sum([cond_overbought, cond_macd_sell, cond_death])

    # ── Signal gate ───────────────────────────────────────────────────────────
    if buys_count >= 3 and sells_count == 0 and two_green and bull_regime:
        signal = "BUY"
    elif sells_count >= 2 and buys_count == 0 and two_red and bear_regime:
        signal = "SELL"
    else:
        # Build a brief NO TRADE rationale
        reasons = []
        if not bull_regime and buys_count >= 3:
            reasons.append("price below MA200 (bear regime blocks BUY)")
        elif buys_count < 3:
            reasons.append(f"only {buys_count}/4 BUY conditions met")
        if not two_green and buys_count >= 3 and bull_regime:
            reasons.append("no 2-bar green confirmation")
        rat = ("No trade: " + "; ".join(reasons)) if reasons else "No trade: conditions not met."
        return {**_NO_TRADE, "rationale": rat}

    # ── Confidence scoring ────────────────────────────────────────────────────
    if signal == "BUY":
        if rsi < RSI_5STAR_ENTRY:
            conf = 6          # 5★ MAX DIAMOND
        else:
            # RSI bonus: +1★ if RSI < 40 in any of last 3 bars
            rsi_recent = [v for v in [rsi, prev_rsi, prev2_rsi] if v is not None]
            rsi_bonus  = any(r < RSI_ENTRY_MAX for r in rsi_recent)
            conf = min(buys_count + (1 if rsi_bonus else 0), 4)
    else:
        # SELL confidence: count sell conditions (max 4★)
        conf = min(sells_count + 1, 4)

    # ── Strategies list ───────────────────────────────────────────────────────
    strategies = []
    if signal == "BUY":
        if rsi < RSI_5STAR_ENTRY:
            strategies.append(f"Deep-Oversold RSI ({rsi:.1f} < {RSI_5STAR_ENTRY})")
        elif cond_rsi:
            strategies.append(f"Oversold RSI ({rsi:.1f} < {RSI_ENTRY_MAX})")
        if cond_macd:
            strategies.append("MACD Histogram Cross (above 0)")
        if cond_ma50:
            strategies.append("MA50 Pullback Bounce")
        if cond_vol:
            strategies.append(f"Volume Spike ({rel_vol:.1f}x)")
        if bb_lower is not None and close <= bb_lower:
            strategies.append("BB Lower Band")
    else:
        if cond_overbought:
            strategies.append(f"Overbought RSI ({rsi:.1f} > 65)")
        if cond_macd_sell:
            strategies.append("MACD Histogram Cross (below 0)")
        if cond_death:
            strategies.append("Death Cross (MA50 < MA200)")

    # ── Stop / target ─────────────────────────────────────────────────────────
    stop_dist  = ATR_STOP_MULT * atr14
    tp_pct     = TP_PCT_5STAR if conf >= 5 else TP_PCT_4STAR

    if signal == "BUY":
        stop_price = round(close - stop_dist, 2)
        tgt_price  = round(close * (1 + tp_pct), 2)
    else:
        stop_price = round(close + stop_dist, 2)
        tgt_price  = round(close * (1 - tp_pct), 2)

    entry_zone = f"${close:.2f}"
    stop_loss  = f"${stop_price:.2f}"
    target     = f"${tgt_price:.2f}"

    star_label = "5★ MAX DIAMOND" if conf >= 6 else f"{min(conf, 5)}★"
    strat_str  = ", ".join(strategies)
    if signal == "BUY":
        regime_str = "Bull regime (above MA200)."
        rr = (close - stop_price)
        rr_ratio = round((tgt_price - close) / rr, 1) if rr > 0 else "N/A"
        rationale = (
            f"{star_label} BUY: {strat_str}. "
            f"Entry ${close:.2f}, ATR stop ${stop_price:.2f}, "
            f"target ${tgt_price:.2f} ({tp_pct*100:.0f}% TP, {rr_ratio}:1 R:R). "
            f"{regime_str}"
        )
    else:
        rationale = (
            f"{star_label} SELL: {strat_str}. "
            f"Entry ${close:.2f}, stop ${stop_price:.2f}, "
            f"target ${tgt_price:.2f}. Bear regime (below MA200)."
        )

    # ── Fundamentals bonus ────────────────────────────────────────────────────
    fund_bonus = False
    if fundamentals and signal == "BUY" and conf == 4:
        peg = fundamentals.get("peg")
        roe = fundamentals.get("roe")
        if peg and roe and peg < 1.5 and roe > 15:
            fund_bonus = True
            conf = 5   # upgrade to 5★ if strong fundamentals align

    return {
        "ticker":             ticker,
        "signal":             signal,
        "confidence_stars":   conf,
        "strategies_aligned": strategies,
        "fundamentals_bonus": fund_bonus,
        "rationale":          rationale,
        "entry_zone":         entry_zone,
        "stop_loss":          stop_loss,
        "target":             target,
    }


# ── INVERSE MAPS ──────────────────────────────────────────────────────────────
INDEX_INVERSE_MAP = {
    "SPY": "SPXU",
    "QQQ": "SQQQ",
    "SPXU": "SPY",
    "SQQQ": "QQQ",
    "RWM": "IWM",
    "PSQ": "QQQ"
}

LEVERAGED_MAP = {
    "SPY": "UPRO",
    "QQQ": "TQQQ",
    "TSLA": "TSLL",
    "NVDA": "NVDL"
}

# ── ENGINE 2: INDEX FADE ──────────────────────────────────────────────────────
# Backtest results: Fades extreme overbought indices (RSI > 80)
def get_index_fade_signal(base_ticker: str, indicators: dict) -> dict:
    rsi = indicators.get("rsi")
    close = indicators.get("latest_close")
    bb_upper = indicators.get("bb_upper")

    _NO_TRADE = {
        "ticker": base_ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "Conditions not met for Index Fade.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if any(v is None for v in [rsi, close]):
        return _NO_TRADE

    inverse_ticker = INDEX_INVERSE_MAP.get(base_ticker)
    if not inverse_ticker:
        return _NO_TRADE  # Only process known indexed pairs

    # Mirrors backtest: requires BOTH RSI >= 80 AND price >= BB Upper (tight filter)
    if rsi >= 80 and bb_upper is not None and close >= bb_upper:
        _high_conv = rsi >= 85  # mirrors backtest: levered OR RSI>=85 gets extreme TP
        stars = 6  # 5 star max (blue diamond) — RSI 80+ AND upper BB is max conviction for E2
        strategies = [f"Overbought Fade (RSI {rsi:.1f} ≥ 80)", "BB Upper Band Breach"]

        tp_pct = 0.10 if _high_conv else 0.05
        stop_pct = 0.05

        rationale = (
            f"5 star max (blue diamond) INDEX FADE: {base_ticker} Overbought. "
            f"BUY {inverse_ticker}. Target {tp_pct*100:.0f}%, Stop {stop_pct*100:.0f}%."
        )

        # Note: the target and stop numbers for the actual notification refer to P&L % 
        # since we don't know the exact entry price of the inverse ETF here.
        # We format entry_zone abstractly.
        return {
            "ticker":             inverse_ticker,
            "signal":             "BUY",
            "confidence_stars":   stars,
            "strategies_aligned": strategies,
            "fundamentals_bonus": False,
            "rationale":          rationale,
            "entry_zone":         "Market Open",
            "stop_loss":          f"-{int(stop_pct*100)}%",
            "target":             f"+{int(tp_pct*100)}%",
        }

    return _NO_TRADE


# ── ENGINE 3: LEVERAGED SHOCK-BOUNCE ──────────────────────────────────────────
# Backtest results: Deep capitulation dips (Base RSI < 25) bought directly via 3x leverage
def get_leveraged_signal(base_ticker: str, indicators: dict) -> dict:
    rsi = indicators.get("rsi")
    rel_vol = indicators.get("rel_vol")
    close = indicators.get("latest_close")
    bb_lower = indicators.get("bb_lower")

    _NO_TRADE = {
        "ticker": base_ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "Conditions not met for Leveraged Bounce.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if any(v is None for v in [rsi, rel_vol, close]):
        return _NO_TRADE

    lev_ticker = LEVERAGED_MAP.get(base_ticker)
    if not lev_ticker:
        return _NO_TRADE

    signal_triggered = False
    stars = 0
    strategies = []
    
    # 1. 5★ MAX DIAMOND RULES (Capitulation Meltdowns)
    if rsi < 25 or (rsi <= 30 and rel_vol >= 1.5):
        stars = 6 # 5★ MAX DIAMOND
        strategies = []
        if rsi < 25:
            strategies.append(f"Deep Oversold capitulation ({base_ticker} RSI < 25)")
            if bb_lower is not None and close <= bb_lower:
                strategies.append("BB Lower Band Rejection")
        else:
            strategies.append(f"Volume Sweep Capitulation ({base_ticker} RSI < 30 + {rel_vol:.1f}x Vol)")
            
        return {
            "ticker":             lev_ticker,
            "signal":             "BUY",
            "confidence_stars":   stars,
            "strategies_aligned": strategies,
            "fundamentals_bonus": False,
            "rationale":          f"5★ MAX DIAMOND LEVERAGED SHOCK-BOUNCE: {base_ticker} Capitulation. BUY {lev_ticker}. 10% Trailing Stop triggers at +35%. Hard Floor Stop: -15%. Cash-out target: Exit when {lev_ticker} RSI > 70.",
            "entry_zone":         "Market Open",
            "stop_loss":          "-15%",
            "target":             "Trail > +35%",
        }
        
    # 2. 4★ RULES (MA Bounce Trend Following Base Hits)
    ma20 = indicators.get("ma20")
    ma50 = indicators.get("ma50")
    ma200 = indicators.get("ma200")
    open_p = indicators.get("latest_open")
    low_p = indicators.get("latest_low")
    high20 = indicators.get("high20")
    prev_close = indicators.get("prev_close")
    
    if all(v is not None for v in [ma20, ma50, ma200, open_p, low_p, high20]):
        if close > ma200 and close > ma50:
            
            # --- 5★ Leveraged Breakout ---
            if close >= (high20 * 0.99) and rel_vol >= 1.2:
                strategies.append(f"Momentum Breakout 5★ ({base_ticker})")
                return {
                    "ticker":             lev_ticker,
                    "signal":             "BUY",
                    "confidence_stars":   5,
                    "strategies_aligned": strategies,
                    "fundamentals_bonus": False,
                    "rationale":          f"5★ LEVERAGED MOMENTUM BREAKOUT: {base_ticker} at 20-Day High with {rel_vol:.1f}x Vol. BUY {lev_ticker}. 15% Continuous Trailing Stop. Uncapped target.",
                    "entry_zone":         "Market Open",
                    "stop_loss":          "-15% Trail",
                    "target":             "Uncapped",
                }
            
            touched_ma20 = (low_p <= ma20)
            touched_ma50 = (low_p <= ma50)
            
            if touched_ma20 or touched_ma50:
                closed_above_ma20 = (close > ma20) if touched_ma20 else False
                closed_above_ma50 = (close > ma50) if touched_ma50 else False
                is_green = (close > open_p) if open_p else (close > prev_close)
                high_vol = (rel_vol >= 1.0)
                
                if (closed_above_ma20 or closed_above_ma50) and is_green and high_vol:
                    return {
                        "ticker":             lev_ticker,
                        "signal":             "BUY",
                        "confidence_stars":   4,
                        "strategies_aligned": [f"MA Trend Bounce ({base_ticker})"],
                        "fundamentals_bonus": False,
                        "rationale":          f"4★ LEVERAGED BASE HIT: {base_ticker} Trend Bounce. BUY {lev_ticker}. Target +10%, Stop -9%, Max Hold 12 days.",
                        "entry_zone":         "Market Open",
                        "stop_loss":          "-9%",
                        "target":             "+10%",
                    }

    return _NO_TRADE


# ── ENGINE 4: SPY REGIME MOMENTUM (UPRO) ─────────────────────────────────────
# Mirrors backtest_master.py Engine 4 logic:
#   Confirmed Bull: SPY above MA50 AND MA50 > MA200 (golden cross)
#                   SMA RSI 50-72, 20d momentum >= 2%
#   Pullback gate:  SPY SMA RSI pulled back to <= 52 in last 10 days
#   Strategy B:     UPRO Bull Hold — 35% TP, 17% trail trigger → 10% trail, 30d churn
#   Regime Exit:    SPY closes below MA50 for 5 consecutive days
def get_regime_signal(spy_indicators: dict) -> dict:
    _NO_TRADE = {
        "ticker": "UPRO", "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "SPY Regime not confirmed for UPRO entry.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    spy_close            = spy_indicators.get("latest_close")
    spy_ma50             = spy_indicators.get("ma50")
    spy_ma200            = spy_indicators.get("ma200")
    spy_smarsi           = spy_indicators.get("sma_rsi")       # SMA-based RSI for Engine 4
    spy_ret20            = spy_indicators.get("ret20")          # 20-day momentum
    spy_ma50_breach_days = spy_indicators.get("ma50_breach_days", 0)
    recent_rsi_min       = spy_indicators.get("sma_rsi_10d_min")

    if any(v is None for v in [spy_close, spy_ma50, spy_ma200, spy_smarsi]):
        return _NO_TRADE

    # 5-day breach rule — regime is broken, stay out
    if spy_ma50_breach_days >= 5:
        return {**_NO_TRADE, "rationale": f"SPY Regime EXIT: MA50 breached for {spy_ma50_breach_days} consecutive days."}

    # Confirmed bull: golden cross + RSI 50-72 + 20d momentum >= 2%
    ret20 = spy_ret20 or 0
    confirmed_bull = (
        spy_close > spy_ma50
        and spy_ma50 > spy_ma200
        and 50 <= spy_smarsi <= 72
        and ret20 >= 0.02
    )

    if not confirmed_bull:
        return {**_NO_TRADE, "rationale": f"SPY not in confirmed bull regime (RSI: {spy_smarsi:.1f}, Golden Cross: {spy_ma50 > spy_ma200}, 20d mom: {ret20:.1%})."}

    # Pullback filter: SPY SMA RSI pulled back to <= 52 in last 10 days
    if recent_rsi_min is None or recent_rsi_min > 52:
        return {**_NO_TRADE, "rationale": f"SPY confirmed bull but no RSI pullback (<=52) in last 10 days. Min: {recent_rsi_min}."}

    return {
        "ticker":             "UPRO",
        "signal":             "BUY",
        "confidence_stars":   5,   # 5 star (gold stars) — Regime Momentum
        "strategies_aligned": [
            "SPY Golden Cross (MA50 > MA200)",
            f"SPY RSI Pullback (10d min: {recent_rsi_min:.1f} <= 52)",
            f"SPY 20d Momentum: {ret20:.1%}",
        ],
        "fundamentals_bonus": False,
        "rationale": (
            f"5 star (gold stars) REGIME MOMENTUM: SPY confirmed bull + RSI pullback. BUY UPRO. "
            f"35% TP, 17% trail trigger → 10% trail, 40% floor stop, 30-day churn. "
            f"Regime exits if SPY closes below MA50 for 5 consecutive days."
        ),
        "entry_zone": "Market Open",
        "stop_loss":   "-40% floor / 10% trail after +17%",
        "target":      "+35% or 30-day churn",
    }


# ── ENGINE 5: SECTOR HUNTER ──────────────────────────────────────────────────
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

E5_EXCLUDED_SECTORS = {
    "Communication Services", "Consumer Staples", "Materials", "Energy", "Utilities"
}

E5_SECTOR_STARS = {
    "Health Care": 6, "Industrials": 6, "Real Estate": 6,   # 5★ MAX DIAMOND
    "Financials": 4, "Information Technology": 4, "Consumer Discretionary": 4,
}

_E5_TOP_N     = 3
_E5_RS_SHORT  = 20
_E5_RS_LONG   = 60
_E5_RS_SLOPE  = 5
_E5_ENTRY_RSI = 40


def compute_hot_sectors() -> set:
    """Download sector ETF + SPY data and return the set of hot sector ETF tickers today.
    Hot = top-3 by blended RS rank AND positive 5-day RS slope (mirrors backtest logic).
    """
    import numpy as np
    import pandas as pd

    etfs = list(E5_SECTOR_ETF_MAP.values())
    try:
        raw   = yf.download(etfs + ["SPY"], period="120d", progress=False, auto_adjust=True)
        close = raw["Close"]
        spy   = close["SPY"] if "SPY" in close.columns else None
        if spy is None:
            return set()

        rs_data  = {etf: close[etf] / spy.replace(0, float("nan"))
                    for etf in etfs if etf in close.columns}
        rs_df    = pd.DataFrame(rs_data).dropna(how="all")
        rs_short = rs_df.pct_change(_E5_RS_SHORT)
        rs_long  = rs_df.pct_change(_E5_RS_LONG)
        avg_rank = (rs_short.rank(axis=1, ascending=False) +
                    rs_long.rank(axis=1, ascending=False)) / 2.0
        rs_slope = rs_df.diff(_E5_RS_SLOPE)

        today_rank  = avg_rank.iloc[-1]
        today_slope = rs_slope.iloc[-1]

        hot = set()
        for etf in etfs:
            if etf not in today_rank.index:
                continue
            rank, slope = today_rank[etf], today_slope[etf]
            if pd.isna(rank) or pd.isna(slope):
                continue
            if rank <= _E5_TOP_N and slope > 0:
                hot.add(etf)
        return hot
    except Exception:
        return set()


def get_sector_hunter_signal(ticker: str, sector: str, indicators: dict,
                              hot_sector_etfs: set) -> dict:
    """Engine 5: return a BUY signal if the stock's sector is hot and entry criteria are met."""
    _NO_TRADE = {
        "ticker": ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "E5 Sector Hunter: conditions not met.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if not sector or sector in E5_EXCLUDED_SECTORS:
        return _NO_TRADE

    sector_etf = E5_SECTOR_ETF_MAP.get(sector)
    if not sector_etf or sector_etf not in hot_sector_etfs:
        return _NO_TRADE

    rsi     = indicators.get("rsi")
    close   = indicators.get("latest_close")
    prev_cl = indicators.get("prev_close")
    prev2   = indicators.get("prev2_close")
    ma20    = indicators.get("ma20")
    ma50    = indicators.get("ma50")
    ma200   = indicators.get("ma200")

    if any(v is None for v in [rsi, close, prev_cl, prev2, ma20, ma50]):
        return _NO_TRADE

    # SPY regime gate (bull market only)
    if ma200 is not None and close < ma200:
        return _NO_TRADE

    # RSI oversold — lagging the hot sector
    if rsi >= _E5_ENTRY_RSI:
        return _NO_TRADE

    # 2 consecutive green closes
    if not (close > prev_cl and prev_cl > prev2):
        return _NO_TRADE

    # Price proximity to MA20/MA50 (within 5%)
    if not (close <= ma20 * 1.05 and close >= ma50 * 0.95):
        return _NO_TRADE

    stars = E5_SECTOR_STARS.get(sector, 4)

    return {
        "ticker":             ticker,
        "signal":             "BUY",
        "confidence_stars":   stars,
        "strategies_aligned": [
            f"Sector Hunter: {sector} top-{_E5_TOP_N} by RS ({sector_etf} leading SPY)",
            f"Oversold RSI {rsi:.1f} < {_E5_ENTRY_RSI} — lagging hot sector",
            "2 consecutive green closes (momentum confirmation)",
        ],
        "fundamentals_bonus": False,
        "rationale": (
            f"{'5★ MAX' if stars >= 6 else '5★' if stars >= 5 else '4★'} SECTOR HUNTER: {sector} is top-{_E5_TOP_N} "
            f"by relative strength. {ticker} RSI {rsi:.1f} — oversold within leading sector. "
            f"Entry ${close:.2f}, TP +25%, Hard Stop -12%, Max Hold 30d."
        ),
        "entry_zone": f"${close:.2f}",
        "stop_loss":  "-12%",
        "target":     "+25%",
    }


if __name__ == "__main__":
    from fetcher import fetch_price_data
    from indicators import compute_indicators

    ticker = "AAPL"
    print(f"Fetching data and generating rule-based signal for {ticker}...")
    df  = fetch_price_data(ticker)
    ind = compute_indicators(df, ticker)
    sig = get_signal(ticker, ind)
    import json
    print(json.dumps(sig, indent=2))
