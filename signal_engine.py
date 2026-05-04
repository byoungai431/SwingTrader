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
                    "rationale":          f"5★ LEVERAGED MOMENTUM BREAKOUT: {base_ticker} at 20-Day High with {rel_vol:.1f}x Vol. BUY {lev_ticker}. 15% Continuous Trailing Stop. Uncapped target.",
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
            f"{'5★ MAX' if stars >= 6 else '5★' if stars >= 5 else '4★'} SECTOR HUNTER: {sector} is top-{_E5_TOP_N} "
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
    conf = 5 if is_t1 else 3  # T2/T3 both 3★ (Tier 4); only E7 is Tier 3 (4★)

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
            f"{'5★' if is_t1 else '3★'} RANGE REVERSION E6: {ticker} BB mean-reversion. "
            f"{path_str}. ADX {adx_val:.1f} ({adx_streak}-bar chop), "
            f"RSI {rsi_val:.1f}, Vol {rel_vol:.1f}x. "
            f"Entry ${cur_close:.2f}, Stop -6% (${stop_price:.2f}), "
            f"Target +10% (${tp_price:.2f}), Stale {E6_STALE_CUT_DAYS}d, Max {E6_MAX_HOLD}d."
        ),
        "entry_zone": f"${cur_close:.2f}",
        "stop_loss":  f"${stop_price:.2f}",
        "target":     f"${tp_price:.2f}",
    }


# ── ENGINE 7: DOUBLE BOTTOM W2 ANTICIPATION ───────────────────────────────────
# Detects W1 (first confirmed swing low) via argrelextrema, watches for price
# to return to the W1 zone, and confirms a bounce on the current bar.
# Validated: PF 1.79 in-sample (2020–2026), PF 1.75 out-of-sample (2015–2020).
E7_LOOKBACK        = 150
E7_ORDER_W1        = 10
E7_MIN_SEP         = 15
E7_ZONE_PCT        = 0.05
E7_CANCEL_PCT      = 0.06
E7_TOUCH_PCT       = 0.01
E7_TOUCH_WINDOW    = 5
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


def get_pattern_signal(ticker: str, df: "pd.DataFrame", spy_df: "pd.DataFrame") -> dict:
    """
    Engine 7: Double Bottom W2 Anticipation.
    Detects W1 via argrelextrema(order=10), then checks if the current bar
    is a confirming bounce while price is still in the W1 zone.
    Requires full OHLCV DataFrame (>=200 bars) for both ticker and SPY.
    """
    import numpy as np
    import pandas as pd
    from scipy.signal import argrelextrema

    _NO_TRADE = {
        "ticker": ticker, "signal": "NO TRADE", "confidence_stars": 0,
        "strategies_aligned": [], "fundamentals_bonus": False,
        "rationale": "E7 Pattern: conditions not met.",
        "entry_zone": None, "stop_loss": None, "target": None,
    }

    if df is None or len(df) < 200:
        return _NO_TRADE
    if spy_df is None or len(spy_df) < 55:
        return _NO_TRADE

    # SPY regime: SPY close > MA50
    spy_close_s = spy_df["Close"].squeeze().dropna()
    if len(spy_close_s) < 55:
        return _NO_TRADE
    spy_ma50_val = float(spy_close_s.rolling(50).mean().iloc[-1])
    if pd.isna(spy_ma50_val) or float(spy_close_s.iloc[-1]) <= spy_ma50_val:
        return _NO_TRADE

    # Build aligned OHLCV series (squeeze handles MultiIndex columns from yfinance)
    try:
        close_s = df["Close"].squeeze().dropna()
        open_s  = df["Open"].squeeze().reindex(close_s.index)
        low_s   = df["Low"].squeeze().reindex(close_s.index)
        high_s  = df["High"].squeeze().reindex(close_s.index)
        vol_s   = df["Volume"].squeeze().reindex(close_s.index).fillna(0) if "Volume" in df.columns \
                  else pd.Series(0.0, index=close_s.index)
    except Exception:
        return _NO_TRADE

    if len(close_s) < 200:
        return _NO_TRADE

    # Current (confirming) bar
    cur_close = float(close_s.iloc[-1])
    cur_open  = float(open_s.iloc[-1])
    cur_low   = float(low_s.iloc[-1])
    cur_vol   = float(vol_s.iloc[-1])
    vol_avg20 = float(vol_s.rolling(20).mean().iloc[-1])

    if pd.isna(cur_close) or pd.isna(cur_open):
        return _NO_TRADE

    # Lookback window — exclude current bar so argrelextrema isn't biased by today
    lb_close = close_s.iloc[-(E7_LOOKBACK + 1):-1]
    lb_low   = low_s.iloc[-(E7_LOOKBACK + 1):-1]
    lb_high  = high_s.iloc[-(E7_LOOKBACK + 1):-1]
    n = len(lb_close)
    if n < E7_ORDER_W1 * 2 + E7_MIN_SEP + 5:
        return _NO_TRADE

    lows_arr  = lb_low.values
    close_arr = lb_close.values
    highs_arr = lb_high.values

    # Find confirmed local minima
    min_idxs = argrelextrema(lows_arr, np.less, order=E7_ORDER_W1)[0]
    min_idxs = [i for i in min_idxs if i <= n - E7_MIN_SEP - 1]
    if len(min_idxs) == 0:
        return _NO_TRADE

    for w1_local_idx in reversed(min_idxs):
        w1_price = float(lows_arr[w1_local_idx])

        if cur_close > w1_price * (1 + E7_ZONE_PCT):
            continue    # price not in W1 zone
        if cur_close < w1_price * (1 - E7_CANCEL_PCT):
            continue    # support broken

        # Neckline: highest close from W1 onward in lookback
        neckline = float(close_arr[w1_local_idx:].max())
        if neckline < w1_price * (1 + E7_NECKLINE_MIN):
            continue    # no meaningful recovery
        if cur_close > neckline:
            continue    # already through target — too late

        # W1 depth from prior high
        pre_start    = max(0, w1_local_idx - E7_DEPTH_WINDOW)
        pre_highs    = highs_arr[pre_start: w1_local_idx]
        if len(pre_highs) == 0:
            continue
        high_idx     = int(np.argmax(pre_highs))
        prior_high   = float(pre_highs[high_idx])
        if prior_high <= w1_price:
            continue
        w1_depth     = prior_high / w1_price - 1
        if w1_depth < E7_DEPTH_MIN:
            continue

        # W1 velocity
        bars_from_high = max(1, len(pre_highs) - high_idx)
        w1_velocity    = w1_depth / bars_from_high
        if w1_velocity < E7_VELOCITY_MIN or w1_velocity > E7_VELOCITY_MAX:
            continue

        # Touch check: last TOUCH_WINDOW bars of lookback + current bar
        touch_lows = list(lb_low.iloc[-E7_TOUCH_WINDOW:].values) + [cur_low]
        if not any(l <= w1_price * (1 + E7_TOUCH_PCT) for l in touch_lows):
            continue

        # Confirming bar must be green and close above W1
        if cur_close <= cur_open or cur_close <= w1_price:
            continue

        bar_body = (cur_close - cur_open) / cur_open if cur_open > 0 else 0.0
        if vol_avg20 > 0:
            vol_ratio        = cur_vol / vol_avg20
            above_vol_std    = vol_ratio >= E7_VOL_MULT
            above_vol_relax  = vol_ratio >= E7_VOL_MULT_RELAX
        else:
            vol_ratio = 0.0
            above_vol_std = above_vol_relax = True

        standard_trigger = bar_body >= E7_BAR_BODY_MIN   and above_vol_std
        strong_candle    = bar_body >= E7_BAR_BODY_STRONG and above_vol_relax

        if not (standard_trigger or strong_candle):
            continue

        stop_price = round(w1_price * (1 - E7_STOP_PCT), 2)

        _tier1 = (w1_depth >= 0.20) or (0.010 <= w1_velocity <= 0.015)
        _stars = 6 if _tier1 else 5
        _star_label = "5★" if _stars == 6 else "4★"

        return {
            "ticker":             ticker,
            "signal":             "BUY",
            "confidence_stars":   _stars,
            "strategies_aligned": [
                f"W2 anticipation: W1 support ${w1_price:.2f}, neckline ${neckline:.2f}",
                f"W1 depth {w1_depth*100:.0f}%, velocity {w1_velocity*100:.2f}%/bar",
                f"Vol {vol_ratio:.1f}x avg, green bar, SPY above MA50",
            ],
            "fundamentals_bonus": False,
            "rationale": (
                f"{_star_label} DOUBLE BOTTOM W2 (E7): {ticker} bouncing off W1 support "
                f"${w1_price:.2f}. Neckline ${neckline:.2f}. "
                f"Depth {w1_depth*100:.0f}%, vel {w1_velocity*100:.2f}%/bar. "
                f"Vol {vol_ratio:.1f}x avg."
            ),
            "entry_zone": f"${cur_close:.2f}",
            "stop_loss":  f"${stop_price:.2f}",
            "target":     f"${neckline:.2f}",
        }

    return _NO_TRADE


def scan_e7_watching(ticker: str, df, spy_df):
    """
    Returns a setup dict if this ticker has a valid E7 double bottom forming:
      - W1 detected + depth/velocity pass
      - Price rallied to neckline (left side of W confirmed)
      - Price now pulling back below neckline (right side developing)
      - Support not broken
    Returns None if no qualifying setup found.
    """
    import numpy as np
    import pandas as pd
    from scipy.signal import argrelextrema

    if spy_df is None or len(spy_df) < 55:
        return None
    try:
        spy_close_s = spy_df["Close"].squeeze().dropna()
        spy_ma50    = float(spy_close_s.rolling(50).mean().iloc[-1])
        if float(spy_close_s.iloc[-1]) <= spy_ma50:
            return None
    except Exception:
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

    lb_close = close_s.iloc[-(E7_LOOKBACK + 1):-1]
    lb_low   = low_s.iloc[-(E7_LOOKBACK + 1):-1]
    lb_high  = high_s.iloc[-(E7_LOOKBACK + 1):-1]
    lb_open  = open_s.iloc[-(E7_LOOKBACK + 1):-1]
    n = len(lb_close)
    if n < E7_ORDER_W1 * 2 + E7_MIN_SEP + 5:
        return None

    lows_arr  = lb_low.values
    close_arr = lb_close.values
    highs_arr = lb_high.values
    open_arr  = lb_open.values

    min_idxs = argrelextrema(lows_arr, np.less, order=E7_ORDER_W1)[0]
    min_idxs = [i for i in min_idxs if i <= n - E7_MIN_SEP - 1]

    for w1_local_idx in reversed(min_idxs):
        w1_price = float(lows_arr[w1_local_idx])

        # If a more-recent local minimum is lower than this one, this W1 is
        # superseded — skip it (visual-only guard; does not affect get_pattern_signal)
        if any(lows_arr[i] < w1_price for i in min_idxs if i > w1_local_idx):
            continue

        zone_top = w1_price * (1 + E7_ZONE_PCT)

        # Neckline = highest close from W1 onward in lookback
        neckline = float(close_arr[w1_local_idx:].max())
        if neckline < w1_price * (1 + E7_NECKLINE_MIN):
            continue

        # Price must be pulling back: current below neckline
        if cur_close >= neckline:
            continue

        # W1 support must never have been broken after W1 formed.
        # If any bar's low after W1 went below W1, the double bottom is invalid.
        post_w1_lows = lows_arr[w1_local_idx + 1:]
        if len(post_w1_lows) > 0 and float(post_w1_lows.min()) < w1_price:
            continue

        # Current price must still be at or above W1
        if cur_close < w1_price:
            continue

        # Check if W2 already confirmed: dipped into zone AND had a bullish
        # close back above zone_top.  If still below neckline, flag as
        # missed_active (show in a separate section) rather than dropping it.
        missed_active   = False
        neck_local      = int(np.argmax(close_arr[w1_local_idx:])) + w1_local_idx
        post_nl         = lows_arr[neck_local + 1:]
        post_nc         = close_arr[neck_local + 1:]
        post_no         = open_arr[neck_local + 1:]
        if len(post_nl) > 0 and np.any(post_nl <= zone_top):
            first_zone  = int(np.argmax(post_nl <= zone_top))
            w2_confirmed = any(
                post_nc[i] > zone_top and post_nc[i] > post_no[i]
                for i in range(first_zone, len(post_nc))
            )
            if w2_confirmed:
                if cur_close < neckline and cur_close > w1_price:
                    missed_active = True  # trade triggered; heading toward target
                else:
                    continue  # fully played out or broken below W1

        # Depth from prior high
        pre_start  = max(0, w1_local_idx - E7_DEPTH_WINDOW)
        pre_highs  = highs_arr[pre_start: w1_local_idx]
        if len(pre_highs) == 0:
            continue
        high_idx   = int(np.argmax(pre_highs))
        prior_high = float(pre_highs[high_idx])
        if prior_high <= w1_price:
            continue
        w1_depth = prior_high / w1_price - 1
        if w1_depth < E7_DEPTH_MIN:
            continue

        # Velocity
        bars_from_high = max(1, len(pre_highs) - high_idx)
        w1_velocity    = w1_depth / bars_from_high
        if w1_velocity < E7_VELOCITY_MIN or w1_velocity > E7_VELOCITY_MAX:
            continue

        in_zone        = (cur_close >= w1_price) and (cur_close <= zone_top)
        pct_above_zone = max(0.0, (cur_close - zone_top) / zone_top)

        # Build chart slice: 10 bars before W1 → current bar
        w1_date    = lb_close.index[w1_local_idx]
        chart_loc  = close_s.index.get_indexer([w1_date], method="nearest")[0]
        chart_loc  = max(0, chart_loc - 10)
        df_chart   = pd.DataFrame({
            "Open":   open_s.iloc[chart_loc:],
            "High":   high_s.iloc[chart_loc:],
            "Low":    low_s.iloc[chart_loc:],
            "Close":  close_s.iloc[chart_loc:],
        })

        _tier1 = (w1_depth >= 0.20) or (0.010 <= w1_velocity <= 0.015)

        return {
            "ticker":         ticker,
            "w1_price":       round(w1_price, 2),
            "w1_date":        w1_date,
            "neckline":       round(neckline, 2),
            "zone_top":       round(zone_top, 2),
            "cur_close":      round(cur_close, 2),
            "depth":          w1_depth,
            "velocity":       w1_velocity,
            "in_zone":        in_zone,
            "pct_above_zone": pct_above_zone,
            "df_chart":       df_chart,
            "tier1":          _tier1,
            "missed_active":  missed_active,
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
