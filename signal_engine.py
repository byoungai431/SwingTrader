"""
signal_engine.py — Rule-based signal generator mirroring the backtest E1 logic.

Entry logic (Engine 1 — Core Swing):
  BUY  : ≥3 of 4 conditions + 2 consecutive green candles + price above MA200
         1. RSI < 40            (oversold depth)
         2. MACD histogram crosses above 0   (momentum reversal)
         3. Close reclaimed MA50 from below  (pullback bounce)
         4. Relative volume ≥ 1.5x           (institutional participation)
  SELL : ≥2 of 3 conditions + 2 consecutive red candles + price below MA200
         1. RSI > 65
         2. MACD histogram crosses below 0
         3. Death cross (MA50 < MA200)

Confidence tiers (matches backtest exactly):
  6  (5★ MAX) : RSI < 25 — Deep Oversold Blue Diamond
  4  (4★)     : All other qualifying BUY signals

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
    cond_ma50 = (close > ma50) and (prev_close <= prev_ma50)             # reclaimed MA50 from below
    cond_vol  = (rel_vol is not None) and (rel_vol >= VOL_SPIKE_MIN)

    buys_count = sum([cond_rsi, cond_macd, cond_ma50, cond_vol])

    # ── SELL conditions ───────────────────────────────────────────────────────
    # Note: E1 SELL signals are a secondary overlay. Primary exits are handled by the
    # auto-exit system in run_daily.py (stop, trail, IBS, max-hold). E1 SELL only fires
    # when there is an open BUY and bear-regime conditions are met simultaneously.
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

    # ── Confidence scoring (matches backtest exactly) ─────────────────────────
    if signal == "BUY":
        conf = 6 if rsi < RSI_5STAR_ENTRY else 4
    else:
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
            f"{star_label} BUY (E1): {strat_str}. "
            f"Entry ${close:.2f}, ATR stop ${stop_price:.2f}, "
            f"target ${tgt_price:.2f} ({tp_pct*100:.0f}% TP, {rr_ratio}:1 R:R). "
            f"{regime_str}"
        )
    else:
        rationale = (
            f"{star_label} SELL (E1): {strat_str}. "
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
    "SPY":  "SPXU",
    "UPRO": "SPXU",   # levered signal source → same execution as SPY fade
    "QQQ":  "SQQQ",
    "TQQQ": "SQQQ",   # levered signal source → same execution as QQQ fade
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
        _levered = base_ticker in {"UPRO", "TQQQ", "SPXU", "SQQQ"}
        _high_conv = _levered or rsi >= 85  # mirrors backtest: levered OR RSI>=85 gets extreme TP
        stars = 6  # 5 star max (blue diamond) — RSI 80+ AND upper BB is max conviction for E2
        strategies = [f"Overbought Fade (RSI {rsi:.1f} ≥ 80)", "BB Upper Band Breach"]

        tp_pct   = 0.25 if _high_conv else 0.15   # backtest: IX_TP_EXTREME=0.25, IX_TP_BASE=0.15
        stop_pct = 0.15                            # backtest: IX_STOP_PCT=0.15

        rationale = (
            f"5 star max (blue diamond) INDEX FADE (E2): {base_ticker} Overbought. "
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
            "rationale":          f"5★ MAX DIAMOND LEVERAGED SHOCK-BOUNCE (E3): {base_ticker} Capitulation. BUY {lev_ticker}. 10% Trailing Stop triggers at +35%. Hard Floor Stop: -15%. Cash-out target: Exit when {lev_ticker} RSI > 70.",
            "entry_zone":         "Market Open",
            "stop_loss":          "-15%",
            "target":             "Trail > +35%",
        }
        
    # 5★ Leveraged Breakout (near 20-day high + volume)
    high20 = indicators.get("high20")
    ma200  = indicators.get("ma200")
    ma50   = indicators.get("ma50")

    if all(v is not None for v in [high20, ma200, ma50]):
        if close > ma200 and close > ma50:
            if close >= (high20 * 0.99) and rel_vol >= 1.2:
                strategies.append(f"Momentum Breakout 5★ ({base_ticker})")
                return {
                    "ticker":             lev_ticker,
                    "signal":             "BUY",
                    "confidence_stars":   5,
                    "strategies_aligned": strategies,
                    "fundamentals_bonus": False,
                    "rationale":          f"5★ LEVERAGED MOMENTUM BREAKOUT (E3): {base_ticker} at 20-Day High with {rel_vol:.1f}x Vol. BUY {lev_ticker}. 15% Continuous Trailing Stop. Uncapped target.",
                    "entry_zone":         "Market Open",
                    "stop_loss":          "-15% Trail",
                    "target":             "Uncapped",
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
            f"5 star (gold stars) REGIME MOMENTUM (E4): SPY confirmed bull + RSI pullback. BUY UPRO. "
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
_E5_ENTRY_RSI = 50   # audit 2026-07-12: 40→50 revives a near-dead engine (~4→~94 tr/yr, OOS +5.9%/Sharpe 0.66 net of slippage on live full-500). Energy/Materials kept EXCLUDED (re-enabling hurt OOS).


def compute_hot_sectors() -> set:
    """Download sector ETF + SPY data and return the set of hot sector ETF tickers today.
    Hot = top-3 by blended RS rank AND positive 5-day RS slope (mirrors backtest logic).
    """
    import numpy as np
    import pandas as pd
    import yfinance as yf

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

    # Price proximity to MA20/MA50 — mirrors backtest: must be above 95% of both MAs
    if close < ma20 * 0.95 or close < ma50 * 0.95:
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
            f"{'5★ MAX' if stars >= 6 else '5★' if stars >= 5 else '4★'} SECTOR HUNTER (E5): {sector} is top-{_E5_TOP_N} "
            f"by relative strength. {ticker} RSI {rsi:.1f} — oversold within leading sector. "
            f"Entry ${close:.2f}, TP +25%, Hard Stop -12%, Max Hold 30d."
        ),
        "entry_zone": f"${close:.2f}",
        "stop_loss":  "-12%",
        "target":     "+25%",
    }


# ── ENGINE 6: RANGE REVERSION ─────────────────────────────────────────────────
# Mirrors backtest_chop.py entry logic exactly using rolling daily history.
# Takes full OHLCV DataFrames — needs ~60+ bars for ADX/BB/streak calculations.
# T1 → 5★  |  T2 → 4★  |  T3 → filtered by existing <4★ gate in run_daily.py

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
E6_CONSEC_LOSS_LIMIT   = 5
E6_LOSS_COOLDOWN_DAYS  = 15


def _e6_adx(high, low, close, period=14):
    """EWM-based ADX — exact formula from backtest_chop.py."""
    import numpy as np
    import pandas as pd
    up_move  = high.diff()
    dn_move  = -low.diff()
    plus_dm  = pd.Series(np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0), index=close.index)
    minus_dm = pd.Series(np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0), index=close.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_s    = tr.ewm(com=period - 1, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(com=period - 1, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(com=period - 1, adjust=False).mean() / atr_s
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(com=period - 1, adjust=False).mean()


def _e6_consec_streak(series, condition_fn):
    """Count consecutive bars from END of series where condition_fn(val) is True."""
    import pandas as pd
    count = 0
    for val in reversed(series.values):
        if pd.isna(val) or not condition_fn(val):
            break
        count += 1
    return count


def _e6_spy_gated(spy_df):
    """
    Replay SPY 50 SMA streak on last 30 bars to get current gate state.
    Activates after 2 consecutive closes below 50 SMA, lifts after 2 above.
    Returns True (gated/blocked) or False (open).
    """
    import pandas as pd
    if spy_df is None or len(spy_df) < 55:
        return True
    ma50   = spy_df["Close"].rolling(50).mean()
    below  = spy_df["Close"] < ma50
    gated  = False
    b_str  = 0
    a_str  = 0
    for val in below.iloc[-30:].values:
        if pd.isna(val):
            continue
        if val:
            b_str += 1; a_str = 0
            if b_str >= 2:
                gated = True
        else:
            a_str += 1; b_str = 0
            if a_str >= 2:
                gated = False
    return gated


def get_chop_signal(ticker: str, df: "pd.DataFrame", spy_df: "pd.DataFrame") -> dict:
    """
    Engine 6 Range Reversion live signal.
    Mirrors backtest_chop.py entry logic exactly using 1yr daily OHLCV history.
    """
    import numpy as np
    import pandas as pd

    _NO_TRADE = {
        "ticker": ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "E6 Range Reversion: conditions not met.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if df is None or len(df) < 60:
        return _NO_TRADE

    # ── SPY gates ─────────────────────────────────────────────────────────────
    if spy_df is None or len(spy_df) < 60:
        return _NO_TRADE

    # Gate 1: SPY must be in chop regime (ADX < 22 for 7+ consecutive bars)
    spy_adx    = _e6_adx(spy_df["High"], spy_df["Low"], spy_df["Close"])
    spy_streak = _e6_consec_streak(spy_adx.dropna(), lambda v: v < E6_ADX_MAX)
    if spy_streak < E6_CHOP_BARS:
        return _NO_TRADE

    # Gate 2: SPY 50 SMA 2-bar streak gate
    if _e6_spy_gated(spy_df):
        return _NO_TRADE

    # ── Stock indicators ──────────────────────────────────────────────────────
    close  = df["Close"].dropna()
    high   = df["High"].reindex(close.index)
    low    = df["Low"].reindex(close.index)
    volume = df["Volume"].reindex(close.index)

    if len(close) < 55:
        return _NO_TRADE

    # ADX streak + current value
    adx_series = _e6_adx(high, low, close)
    adx_streak = _e6_consec_streak(adx_series.dropna(), lambda v: v < E6_ADX_MAX)
    if adx_streak < E6_CHOP_BARS:
        return _NO_TRADE
    adx_val = float(adx_series.dropna().iloc[-1])
    if adx_val < E6_ADX_MIN or adx_val > E6_ADX_MAX:
        return _NO_TRADE

    # Range streak (no 20-day breakout for 10+ consecutive bars)
    high20 = high.rolling(20).max()
    low20  = low.rolling(20).min()
    in_range = ((close < high20) & (close > low20)).astype(int)
    range_streak = _e6_consec_streak(in_range, lambda v: v == 1)
    if range_streak < E6_RANGE_BARS:
        return _NO_TRADE

    # RSI
    delta     = close.diff()
    gain      = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
    loss      = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
    rsi_val   = float((100 - 100 / (1 + gain / loss)).iloc[-1])
    if rsi_val < E6_RSI_MIN or rsi_val > E6_RSI_MAX:
        return _NO_TRADE

    # Volume
    vol_avg20 = volume.rolling(20).mean()
    rel_vol   = float(volume.iloc[-1]) / float(vol_avg20.iloc[-1])
    if rel_vol < E6_VOL_SPIKE_MIN:
        return _NO_TRADE

    # Bollinger Bands
    bb_mid    = close.rolling(E6_BB_PERIOD).mean()
    bb_lower  = bb_mid - E6_BB_STD * close.rolling(E6_BB_PERIOD).std()
    bb_lo     = float(bb_lower.iloc[-1])

    # Recent dip below lower BB (previous 10 bars, shift(1) mirrors backtest)
    below_flag   = (close < bb_lower).astype(int)
    recent_below = int(below_flag.iloc[-11:-1].max())
    if recent_below != 1:
        return _NO_TRADE

    # Max dip depth + min close in lookback (previous 10 bars)
    dip_pct_s      = ((bb_lower - close) / bb_lower).clip(lower=0)
    max_dip        = float(dip_pct_s.iloc[-11:-1].max())
    min_close_lb   = float(close.iloc[-11:-1].min())
    cur_close      = float(close.iloc[-1])

    # ── Entry paths ───────────────────────────────────────────────────────────
    path_a = (E6_DIP_MIN_PCT <= max_dip < E6_DIP_AVOID_LOW) and (cur_close >= bb_lo)
    path_b = (max_dip >= E6_DIP_AVOID_HIGH) and (cur_close >= min_close_lb * (1 + E6_DEEP_REBOUND_PCT))
    rec_req = E6_SHALLOW_RECOVER_PCT if max_dip < E6_DIP_MIN_PCT else E6_STRONG_RECOVER_PCT
    path_c  = (max_dip > 0) and (cur_close >= bb_lo * (1 + rec_req))

    # Block ambiguous dip zone (7–9%)
    if 0.07 <= max_dip < E6_DIP_AVOID_HIGH and cur_close < bb_lo * 1.02:
        return _NO_TRADE

    if not (path_a or path_b or path_c):
        return _NO_TRADE

    # ── Tier → confidence stars ───────────────────────────────────────────────
    is_t1 = (
        rel_vol >= 3.0 or
        rsi_val < 35 or
        (path_a and rel_vol >= 2.0) or
        (15 <= adx_val < 18) or
        (15 <= adx_val < 18 and 50 <= rsi_val < 65)
    )
    is_t2 = (not is_t1) and (
        (1.5 <= rel_vol < 2.0) or
        (55 <= rsi_val < 65) or
        (2.0 <= rel_vol < 3.0) or
        (18 <= adx_val < 22 and 35 <= rsi_val < 50)
    )
    # T2 and T3 intentionally share conf=3 — is_t2 is kept for the rationale label only
    conf = 5 if is_t1 else 3

    path_labels = (["Path A (BB dip 2–5%)"] if path_a else []) + \
                  (["Path B (deep dip 9%+ rebound)"] if path_b else []) + \
                  (["Path C (+5% recovery above BB)"] if path_c else [])
    path_str   = " + ".join(path_labels)
    tier_label = "T1 5★" if is_t1 else ("T2 3★" if is_t2 else "T3 3★")
    stop_price = round(cur_close * (1 - E6_STOP_PCT), 2)
    tp_price   = round(cur_close * (1 + E6_TP_PCT), 2)

    return {
        "ticker":             ticker,
        "signal":             "BUY",
        "confidence_stars":   conf,
        "strategies_aligned": [
            f"Range Reversion E6 ({tier_label})",
            f"{path_str}",
            f"ADX {adx_val:.1f} ({adx_streak}d chop streak), RSI {rsi_val:.1f}, Vol {rel_vol:.1f}x",
            f"BB dip depth {max_dip*100:.1f}% in last {E6_DIP_LOOKBACK}d",
        ],
        "fundamentals_bonus": False,
        "rationale": (
            f"{'5★' if is_t1 else '3★'} RANGE REVERSION (E6): {ticker} BB mean-reversion. "
            f"{path_str}. ADX {adx_val:.1f} ({adx_streak}-bar chop), "
            f"RSI {rsi_val:.1f}, Vol {rel_vol:.1f}x. "
            f"Entry ${cur_close:.2f}, Stop -6% (${stop_price:.2f}), "
            f"Target +10% (${tp_price:.2f}), Stale {E6_STALE_CUT_DAYS}d, Max {E6_MAX_HOLD}d."
        ),
        "entry_zone": f"${cur_close:.2f}",
        "stop_loss":  f"${stop_price:.2f}",
        "target":     f"${tp_price:.2f}",
    }


# ── Engine 7: Classical Double Bottom (Neckline Breakout) ──────────────────────
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

E7_LOOKBACK        = 150
E7_ORDER_W1        = 8
E7_DOWNTREND_BARS  = 20
E7_W1_DEPTH_MIN    = 0.15
E7_W1_DEPTH_MAX    = 0.25
E7_NECKLINE_MIN    = 0.05
E7_W2_W1_TOL       = 0.04
E7_W2_MIN_SEP      = 15
E7_W2_MAX_SEP      = 100
E7_TB_MAX_SEP      = 60
E7_BREAKOUT_STALE  = 0.05
E7_STOP_PCT        = 0.05
E7_W2_VOL_MIN      = 1.2
E7_PRIOR_LOW_MIN   = 30
E7_PRIOR_LOW_MAX   = 500
E7_PRIOR_LOW_TOL   = 0.03
E7_T1_DEPTH_MIN    = 0.19
E7_SKIP_SECTORS    = {"Consumer Staples", "Materials"}


def _e7_macd_hist(closes, fast=12, slow=26, signal=9):
    s    = pd.Series(closes)
    macd = s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()
    return (macd - macd.ewm(span=signal, adjust=False).mean()).values


def _e7_w1_quality_checks(closes, highs, lows, df_full, start, w1_sub, w2_sub):
    """Returns (neckline_price, depth_pct, at_support) or None."""
    w1_price = closes[w1_sub]

    neck_slice = highs[w1_sub + 1 : w2_sub]
    if len(neck_slice) == 0:
        return None
    neck_local     = int(np.argmax(neck_slice))
    neck_sub       = w1_sub + 1 + neck_local
    neckline_price = float(highs[neck_sub])

    if neckline_price < w1_price * (1 + E7_NECKLINE_MIN):
        return None
    if neckline_price <= closes[w2_sub]:
        return None

    pre_w1_highs = highs[max(0, w1_sub - 20) : w1_sub]
    if len(pre_w1_highs) < 5:
        return None
    prior_high = float(np.max(pre_w1_highs))
    depth_pct  = (prior_high - w1_price) / prior_high
    if depth_pct < E7_W1_DEPTH_MIN or depth_pct > E7_W1_DEPTH_MAX:
        return None

    dt_start = max(0, w1_sub - E7_DOWNTREND_BARS)
    if w1_sub - dt_start < 5:
        return None
    slope = np.polyfit(np.arange(w1_sub - dt_start), closes[dt_start:w1_sub], 1)[0]
    if slope >= 0:
        return None

    at_support       = False
    w1_abs           = start + w1_sub
    prior_look_start = max(0, w1_abs - E7_PRIOR_LOW_MAX)
    prior_look_end   = max(0, w1_abs - E7_PRIOR_LOW_MIN)
    if prior_look_end - prior_look_start >= 20:
        prior_lows   = df_full.iloc[prior_look_start:prior_look_end]["Low"].squeeze().values.astype(float)
        prior_minidx = argrelextrema(prior_lows, np.less, order=5)[0]
        if len(prior_minidx) > 0:
            swing_lows = prior_lows[prior_minidx]
            at_support = any(abs(sl - w1_price) / w1_price <= E7_PRIOR_LOW_TOL for sl in swing_lows)

    return neckline_price, depth_pct, at_support


def _e7_find_db(df):
    """Classical double bottom neckline breakout. Returns setup dict or None."""
    if len(df) < E7_W2_MIN_SEP + E7_ORDER_W1 * 2 + 5:
        return None
    as_of_idx = len(df) - 1
    end   = as_of_idx
    start = max(0, end - E7_LOOKBACK)
    sub   = df.iloc[start : end + 1]
    n     = len(sub)

    if n < E7_W2_MIN_SEP * 2 + E7_ORDER_W1 * 2 + 5:
        return None

    closes    = sub["Close"].squeeze().values.astype(float)
    highs     = sub["High"].squeeze().values.astype(float)
    lows      = sub["Low"].squeeze().values.astype(float)
    vols      = sub["Volume"].squeeze().values.astype(float) if "Volume" in sub.columns else np.ones(n)
    cur_close = closes[-1]

    all_mins = argrelextrema(closes, np.less, order=E7_ORDER_W1)[0]
    if len(all_mins) < 2:
        return None

    valid_w2 = [i for i in all_mins if 3 <= (n - 1 - i) <= 40]
    if not valid_w2:
        return None
    w2_sub   = valid_w2[-1]
    w2_price = closes[w2_sub]

    valid_w1 = [i for i in all_mins if i < w2_sub and E7_W2_MIN_SEP <= (w2_sub - i) <= E7_W2_MAX_SEP]
    if not valid_w1:
        return None

    for w1_sub in reversed(valid_w1):
        w1_price = closes[w1_sub]

        if abs(w2_price - w1_price) / w1_price > E7_W2_W1_TOL:
            continue

        if w2_price < w1_price:
            macd_hist = _e7_macd_hist(closes)
            w1_macd   = float(macd_hist[w1_sub]) if w1_sub < len(macd_hist) else 0.0
            w2_macd   = float(macd_hist[w2_sub]) if w2_sub < len(macd_hist) else 0.0
            if w2_macd <= w1_macd:
                continue
            w2_avg_vol = np.mean(vols[max(0, w2_sub - 20) : w2_sub]) if w2_sub > 0 else vols[w2_sub]
            if E7_W2_VOL_MIN > 0 and w2_avg_vol > 0 and vols[w2_sub] / w2_avg_vol < E7_W2_VOL_MIN:
                continue

        result = _e7_w1_quality_checks(closes, highs, lows, df, start, w1_sub, w2_sub)
        if result is None:
            continue
        neckline_price, depth_pct, at_support = result

        if not at_support:
            continue

        if cur_close <= neckline_price:
            continue
        if cur_close > neckline_price * (1 + E7_BREAKOUT_STALE):
            continue

        avg_vol   = np.mean(vols[max(0, n - 21) : n - 1]) if n > 1 else vols[-1]
        vol_ratio = float(vols[-1] / avg_vol) if avg_vol > 0 else 1.0
        w2_type   = "W2_HIGHER" if w2_price >= w1_price else "W2_LOWER_DIV"

        return {
            "w1_price":       w1_price,
            "w1_abs_idx":     start + w1_sub,
            "neckline_price": neckline_price,
            "w2_price":       w2_price,
            "entry_price":    cur_close,
            "stop_price":     neckline_price * (1 - E7_STOP_PCT),
            "target_price":   neckline_price + (neckline_price - w2_price),
            "depth_pct":      depth_pct,
            "vol_ratio":      vol_ratio,
            "entry_type":     w2_type,
        }

    return None


def _e7_find_tb(df):
    """Triple bottom neckline breakout. Returns setup dict or None."""
    if len(df) < E7_W2_MIN_SEP + E7_ORDER_W1 * 2 + 5:
        return None
    as_of_idx = len(df) - 1
    end   = as_of_idx
    start = max(0, end - E7_LOOKBACK)
    sub   = df.iloc[start : end + 1]
    n     = len(sub)

    if n < E7_W2_MIN_SEP * 3 + E7_ORDER_W1 * 3 + 5:
        return None

    closes    = sub["Close"].squeeze().values.astype(float)
    highs     = sub["High"].squeeze().values.astype(float)
    lows      = sub["Low"].squeeze().values.astype(float)
    vols      = sub["Volume"].squeeze().values.astype(float) if "Volume" in sub.columns else np.ones(n)
    cur_close = closes[-1]

    all_mins = argrelextrema(closes, np.less, order=E7_ORDER_W1)[0]
    if len(all_mins) < 3:
        return None

    valid_w3 = [i for i in all_mins if 3 <= (n - 1 - i) <= 40]
    if not valid_w3:
        return None
    w3_sub   = valid_w3[-1]
    w3_price = closes[w3_sub]

    valid_w2 = [i for i in all_mins if i < w3_sub and E7_W2_MIN_SEP <= (w3_sub - i) <= E7_TB_MAX_SEP]
    if not valid_w2:
        return None

    for w2_sub in reversed(valid_w2):
        w2_price = closes[w2_sub]
        if abs(w2_price - w3_price) / w3_price > E7_W2_W1_TOL:
            continue

        valid_w1 = [i for i in all_mins if i < w2_sub and E7_W2_MIN_SEP <= (w2_sub - i) <= E7_TB_MAX_SEP]
        if not valid_w1:
            continue

        for w1_sub in reversed(valid_w1):
            w1_price = closes[w1_sub]

            all_3 = [w1_price, w2_price, w3_price]
            if (max(all_3) - min(all_3)) / min(all_3) > E7_W2_W1_TOL:
                continue

            p1_slice = highs[w1_sub + 1 : w2_sub]
            p2_slice = highs[w2_sub + 1 : w3_sub]
            if len(p1_slice) == 0 or len(p2_slice) == 0:
                continue
            neckline_price = min(float(np.max(p1_slice)), float(np.max(p2_slice)))

            if neckline_price < w1_price * (1 + E7_NECKLINE_MIN):
                continue
            if neckline_price <= w3_price:
                continue

            pre_w1_highs = highs[max(0, w1_sub - 20) : w1_sub]
            if len(pre_w1_highs) < 5:
                continue
            prior_high = float(np.max(pre_w1_highs))
            depth_pct  = (prior_high - w1_price) / prior_high
            if depth_pct < E7_W1_DEPTH_MIN or depth_pct > E7_W1_DEPTH_MAX:
                continue

            dt_start = max(0, w1_sub - E7_DOWNTREND_BARS)
            if w1_sub - dt_start < 5:
                continue
            slope = np.polyfit(np.arange(w1_sub - dt_start), closes[dt_start:w1_sub], 1)[0]
            if slope >= 0:
                continue

            at_support       = False
            w1_abs           = start + w1_sub
            prior_look_start = max(0, w1_abs - E7_PRIOR_LOW_MAX)
            prior_look_end   = max(0, w1_abs - E7_PRIOR_LOW_MIN)
            if prior_look_end - prior_look_start >= 20:
                prior_lows   = df.iloc[prior_look_start:prior_look_end]["Low"].squeeze().values.astype(float)
                prior_minidx = argrelextrema(prior_lows, np.less, order=5)[0]
                if len(prior_minidx) > 0:
                    swing_lows = prior_lows[prior_minidx]
                    at_support = any(abs(sl - w1_price) / w1_price <= E7_PRIOR_LOW_TOL for sl in swing_lows)
            if not at_support:
                continue

            if cur_close <= neckline_price:
                continue
            if cur_close > neckline_price * (1 + E7_BREAKOUT_STALE):
                continue

            avg_vol   = np.mean(vols[max(0, n - 21) : n - 1]) if n > 1 else vols[-1]
            vol_ratio = float(vols[-1] / avg_vol) if avg_vol > 0 else 1.0

            return {
                "w1_price":       w1_price,
                "w1_abs_idx":     start + w1_sub,
                "neckline_price": neckline_price,
                "w2_price":       w3_price,
                "entry_price":    cur_close,
                "stop_price":     neckline_price * (1 - E7_STOP_PCT),
                "target_price":   neckline_price + (neckline_price - w3_price),
                "depth_pct":      depth_pct,
                "vol_ratio":      vol_ratio,
                "entry_type":     "TRIPLE_BOTTOM",
            }

    return None


def get_pattern_signal(ticker: str, df: "pd.DataFrame", spy_df: "pd.DataFrame", sector: str = "") -> dict:
    """Engine 7: Classical Double Bottom / Triple Bottom Neckline Breakout."""
    _NO_TRADE = {
        "ticker": ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "E7 Pattern: conditions not met.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if sector in E7_SKIP_SECTORS:
        return _NO_TRADE
    if df is None or len(df) < 200:
        return _NO_TRADE
    if spy_df is None or len(spy_df) < 55:
        return _NO_TRADE

    spy_close_s = spy_df["Close"].squeeze().dropna()
    if len(spy_close_s) < 55:
        return _NO_TRADE
    spy_ma50_val = float(spy_close_s.rolling(50).mean().iloc[-1])
    if pd.isna(spy_ma50_val) or float(spy_close_s.iloc[-1]) <= spy_ma50_val:
        return _NO_TRADE

    try:
        close_s = df["Close"].squeeze().dropna()
        if len(close_s) < 200:
            return _NO_TRADE
    except Exception:
        return _NO_TRADE

    setup = _e7_find_db(df)
    if setup is None:
        setup = _e7_find_tb(df)
    if setup is None:
        return _NO_TRADE

    depth_pct      = setup["depth_pct"]
    neckline_price = setup["neckline_price"]
    w1_price       = setup["w1_price"]
    w2_price       = setup["w2_price"]
    entry_price    = setup["entry_price"]
    stop_price     = round(setup["stop_price"], 2)
    target_price   = round(setup["target_price"], 2)
    vol_ratio      = setup["vol_ratio"]
    entry_type     = setup["entry_type"]

    stars = 6 if depth_pct >= E7_T1_DEPTH_MIN else 5
    w2_label = "triple low" if entry_type == "TRIPLE_BOTTOM" else (
        "higher low" if w2_price >= w1_price else "lower low + MACD div"
    )

    return {
        "ticker":             ticker,
        "signal":             "BUY",
        "confidence_stars":   stars,
        "strategies_aligned": [
            f"Neckline breakout ${neckline_price:.2f} confirmed, W1 ${w1_price:.2f} / W2 ${w2_price:.2f} ({w2_label})",
            f"Depth {depth_pct*100:.0f}%, vol {vol_ratio:.1f}x. Stop ${stop_price:.2f}",
            f"Measured move target ${target_price:.2f}. SPY above MA50.",
        ],
        "fundamentals_bonus": False,
        "rationale": (
            f"DOUBLE BOTTOM (E7): {ticker} neckline breakout ${neckline_price:.2f}. "
            f"W1 ${w1_price:.2f} / W2 ${w2_price:.2f} ({w2_label}). "
            f"Depth {depth_pct*100:.0f}%, vol {vol_ratio:.1f}x. "
            f"Stop ${stop_price:.2f}, target ${target_price:.2f} (measured move)."
        ),
        "entry_zone": f"${entry_price:.2f}",
        "stop_loss":  f"${stop_price:.2f}",
        "target":     f"${target_price:.2f}",
    }


def scan_e7_watching(ticker: str, df, spy_df, sector: str = ""):
    """
    Returns a watching dict if this ticker has a valid E7 setup approaching neckline.
    Returns None if no qualifying pre-breakout setup found.
    """
    if sector in E7_SKIP_SECTORS:
        return None
    if spy_df is None or len(spy_df) < 55:
        return None
    try:
        spy_close_s = spy_df["Close"].squeeze().dropna()
        spy_ma50    = float(spy_close_s.rolling(50).mean().iloc[-1])
        if float(spy_close_s.iloc[-1]) <= spy_ma50:
            return None
    except Exception:
        return None

    if df is None or len(df) < 200:
        return None

    try:
        close_s = df["Close"].squeeze().dropna()
        open_s  = df["Open"].squeeze().reindex(close_s.index)
        low_s   = df["Low"].squeeze().reindex(close_s.index)
        high_s  = df["High"].squeeze().reindex(close_s.index)
    except Exception:
        return None

    if len(close_s) < 200:
        return None

    cur_close = float(close_s.iloc[-1])
    if pd.isna(cur_close):
        return None

    end   = len(df) - 1
    start = max(0, end - E7_LOOKBACK)
    sub   = df.iloc[start : end + 1]
    n     = len(sub)

    if n < E7_W2_MIN_SEP * 2 + E7_ORDER_W1 * 2 + 5:
        return None

    closes = sub["Close"].squeeze().values.astype(float)
    highs  = sub["High"].squeeze().values.astype(float)
    lows   = sub["Low"].squeeze().values.astype(float)
    vols   = sub["Volume"].squeeze().values.astype(float) if "Volume" in sub.columns else np.ones(n)

    all_mins = argrelextrema(closes, np.less, order=E7_ORDER_W1)[0]
    if len(all_mins) < 2:
        return None

    valid_w2 = [i for i in all_mins if 3 <= (n - 1 - i) <= E7_W2_MAX_SEP]
    if not valid_w2:
        return None
    w2_sub   = valid_w2[-1]
    w2_price = closes[w2_sub]

    valid_w1 = [i for i in all_mins if i < w2_sub and E7_W2_MIN_SEP <= (w2_sub - i) <= E7_W2_MAX_SEP]
    if not valid_w1:
        return None

    for w1_sub in reversed(valid_w1):
        w1_price = closes[w1_sub]

        if abs(w2_price - w1_price) / w1_price > E7_W2_W1_TOL:
            continue

        if w2_price < w1_price:
            macd_hist = _e7_macd_hist(closes)
            w1_macd   = float(macd_hist[w1_sub]) if w1_sub < len(macd_hist) else 0.0
            w2_macd   = float(macd_hist[w2_sub]) if w2_sub < len(macd_hist) else 0.0
            if w2_macd <= w1_macd:
                continue
            w2_avg_vol = np.mean(vols[max(0, w2_sub - 20) : w2_sub]) if w2_sub > 0 else vols[w2_sub]
            if E7_W2_VOL_MIN > 0 and w2_avg_vol > 0 and vols[w2_sub] / w2_avg_vol < E7_W2_VOL_MIN:
                continue

        result = _e7_w1_quality_checks(closes, highs, lows, df, start, w1_sub, w2_sub)
        if result is None:
            continue
        neckline_price, depth_pct, at_support = result

        if not at_support:
            continue

        # Watching: current close must be below neckline (pre-breakout)
        if cur_close > neckline_price:
            continue

        # W1 support must not be broken
        w1_date_idx  = close_s.index.get_indexer([sub.index[w1_sub]], method="nearest")[0]
        post_w1_lows = low_s.iloc[w1_date_idx + 1:].values.astype(float)
        if len(post_w1_lows) > 0 and float(np.min(post_w1_lows)) < w1_price * (1 - 0.005):
            continue

        in_zone        = cur_close >= neckline_price * (1 - E7_BREAKOUT_STALE)
        pct_above_zone = (neckline_price - cur_close) / neckline_price if neckline_price > 0 else 0.0

        w1_date   = sub.index[w1_sub]
        chart_loc = close_s.index.get_indexer([w1_date], method="nearest")[0]
        chart_loc = max(0, chart_loc - 10)
        df_chart  = pd.DataFrame({
            "Open":  open_s.iloc[chart_loc:],
            "High":  high_s.iloc[chart_loc:],
            "Low":   low_s.iloc[chart_loc:],
            "Close": close_s.iloc[chart_loc:],
        })

        return {
            "ticker":         ticker,
            "w1_price":       round(w1_price, 2),
            "w1_date":        w1_date,
            "neckline":       round(neckline_price, 2),
            "zone_top":       round(neckline_price, 2),
            "cur_close":      round(cur_close, 2),
            "depth":          depth_pct,
            "velocity":       0.0,
            "in_zone":        in_zone,
            "pct_above_zone": pct_above_zone,
            "tier1":          depth_pct >= E7_T1_DEPTH_MIN,
            "missed_active":  False,
            "df_chart":       df_chart,
        }

    return None


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
