"""
run_daily.py — Headless daily signal runner.

Fetches price data, computes indicators, calls Claude for signals,
and saves BUY/SELL results to the SQLite history DB.

Run manually:  python3 run_daily.py
Scheduled via: ~/Library/LaunchAgents/com.swingtrader.daily.plist
"""

import sys
import os
from datetime import datetime

import psycopg2.extras
import yfinance as yf
import pandas as pd

from config import WATCHLIST, HISTORY_DAYS, ANTHROPIC_API_KEY, VIX_MAX
from indicators import compute_indicators
from signal_engine import (get_signal, get_index_fade_signal, get_leveraged_signal, get_regime_signal,
                           compute_hot_sectors, get_sector_hunter_signal, E5_SECTOR_ETF_MAP,
                           get_chop_signal, E6_CONSEC_LOSS_LIMIT, E6_LOSS_COOLDOWN_DAYS,
                           get_pattern_signal, scan_e7_watching)
from history import save_signal, get_performance_stats, get_conn, save_e7_watching, deduplicate_open_signals
from notify import send_telegram, send_daily_summary, send_daily_telegram, send_push, send_exit_alert
from chart_analyzer.scanner import run_scan
from chart_analyzer.history import get_active_setups as e8_get_active_setups

# ── Set to False to fall back to watchlist only ───────────────────────────────
USE_SP500 = True

# ── Exit parameters (mirrors backtest.py config) ───────────────────────────────
FLOOR_5STAR    = 0.10  # Exit 5★ if trade drops this % from entry (0 = disabled)
MAX_HOLD_DAYS  = 30    # Trading-day hold limit for 4★ trades
MAX_HOLD_5STAR = 35    # Trading-day hold limit for 5★ trades
STALE_CUT_DAYS = 12    # Exit after N trading days if gain < 0% (0 = disabled; 5★ exempt)
IBS_MIN_EXIT   = 0.90  # Exit 5★/5★ MAX when IBS (close near day's high) exceeds this

# ── Shared liquidity filter (dollar-volume rank) ──────────────────────────────
# Keeps engines that opt in inside the most-tradable names, cutting real-world slippage.
# Audit 2026-07-12: Engine 6 peaks at top-300 (best win% + OOS Sharpe net of slippage; ranks
# 300-500 dilute quality). E5 validated BETTER on the full universe, so it is NOT gated here.
E6_LIQUIDITY_TOP_N = 300  # Engine 6 only fires on names ranked ≤ this by 20d avg dollar volume (0 = disabled)

# ── IBS Entry Filter (mirrors backtest IBS5s25_4s30 config) ───────────────────
# Require today's bar to close near its LOW before entering — confirms deep
# oversold exhaustion. Mirrors the optimized backtest's biggest quality driver.
IBS_ENTRY_FILTER      = True  # Set False to disable
IBS_MAX_ENTRY_5STAR   = 0.25  # 5★/5★ MAX: today's IBS must be below this
IBS_MAX_ENTRY_4STAR   = 0.30  # 4★: today's IBS must be below this

# ── 5★ Consecutive Loss Cooldown ───────────────────────────────────────────────
CONSEC_5STAR_LOSS_LIMIT   = 4   # Pause 5★ entries after this many consecutive losses
CONSEC_5STAR_COOLDOWN_DAYS = 10  # Calendar days to sit out before re-enabling 5★ entries


def _get_sp500_tickers():
    """Fetch current S&P 500 tickers from Wikipedia."""
    import urllib.request
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        from io import StringIO
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        tickers = pd.read_html(StringIO(html))[0]["Symbol"].tolist()
        # Normalize BRK.B → BRK-B style used by yfinance
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        print(f"  WARNING: Could not fetch S&P 500 list ({e}). Falling back to watchlist.")
        return list(WATCHLIST)


def _get_sp500_sector_map() -> dict:
    """Fetch S&P 500 tickers and GICS sectors from Wikipedia. Returns {ticker: sector}."""
    import urllib.request
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        from io import StringIO
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        table = pd.read_html(StringIO(html))[0]
        table["Symbol"] = table["Symbol"].str.replace(".", "-", regex=False)
        return dict(zip(table["Symbol"], table["GICS Sector"]))
    except Exception as e:
        print(f"  WARNING: Could not fetch S&P 500 sector map ({e}).")
        return {}


def _get_vix() -> float | None:
    """Fetch the latest VIX closing price. Returns None on failure."""
    try:
        raw = yf.download("^VIX", period="5d", progress=False, auto_adjust=True)
        return float(raw["Close"].dropna().iloc[-1].item())
    except Exception:
        return None


def _in_cooldown(ticker: str, today: str, days: int = 5) -> bool:
    """Return True if a BUY signal exists for this ticker within the last `days` calendar days.

    Prevents re-entering a position that was recently signaled.
    Also returns True if the ticker was already processed today (any signal).
    """
    try:
        from datetime import datetime, timedelta
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
        conn = get_conn()
        with conn.cursor() as cur:
            # Block if already processed today (any signal) OR BUY within cooldown window
            cur.execute(
                """SELECT id FROM signals WHERE ticker=%s AND (
                       date = %s
                       OR (signal='BUY' AND date >= %s)
                   ) LIMIT 1""",
                (ticker, today, cutoff)
            )
            row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _in_e6_cooldown(today: str) -> bool:
    """Return True if E6 entries should be paused (5 consecutive E6 losses within cooldown period)."""
    try:
        from datetime import datetime, timedelta
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT exit_price, price, exit_date
                     FROM signals
                    WHERE signal = 'BUY' AND rationale ILIKE '%%Range Reversion E6%%'
                      AND exit_price IS NOT NULL
                    ORDER BY exit_date DESC
                    LIMIT %s""",
                (E6_CONSEC_LOSS_LIMIT,)
            )
            rows = cur.fetchall()
        conn.close()
        if len(rows) < E6_CONSEC_LOSS_LIMIT:
            return False
        all_losses = all(float(r["exit_price"]) < float(r["price"]) for r in rows)
        if not all_losses:
            return False
        most_recent = str(rows[0]["exit_date"])[:10]
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=E6_LOSS_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        return most_recent >= cutoff
    except Exception:
        return False


def _in_5star_cooldown(today: str, engine: str = "E1") -> bool:
    """Return True if 5★ BUY entries should be paused for the given engine.

    Queries the last CONSEC_5STAR_LOSS_LIMIT closed 5★ positions for that specific
    engine only. If every one was a loss AND the most recent exit falls within
    CONSEC_5STAR_COOLDOWN_DAYS calendar days, new 5★ BUY signals are suppressed.
    Each engine is evaluated independently — losses from one engine do not pause others.
    """
    try:
        from datetime import datetime, timedelta
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT exit_price, price, exit_date, rationale
                     FROM signals
                    WHERE signal = 'BUY' AND confidence >= 5 AND exit_price IS NOT NULL
                    ORDER BY exit_date DESC
                    LIMIT %s""",
                (CONSEC_5STAR_LOSS_LIMIT * 10,)
            )
            all_rows = cur.fetchall()
        conn.close()

        # Filter to only rows matching this engine
        rows = [r for r in all_rows if _detect_engine(r.get("rationale", "")) == engine]
        rows = rows[:CONSEC_5STAR_LOSS_LIMIT]

        if len(rows) < CONSEC_5STAR_LOSS_LIMIT:
            return False

        all_losses = all(float(r["exit_price"]) < float(r["price"]) for r in rows)
        if not all_losses:
            return False

        most_recent_exit = str(rows[0]["exit_date"])[:10]
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=CONSEC_5STAR_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        return most_recent_exit >= cutoff
    except Exception:
        return False


def _parse_price(s) -> float | None:
    """Parse a price string like '$185.50' or '185-190' → float (lower bound for ranges)."""
    if not s:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    if "-" in s:
        s = s.split("-")[0].strip()
    try:
        return float(s)
    except Exception:
        return None


def _get_open_positions() -> list[dict]:
    """Return all open BUY positions enriched with current price and unrealized P&L."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, ticker, date, confidence, price, stop_loss, target, rationale, peak_price
                FROM signals
                WHERE signal = 'BUY' AND exit_price IS NULL
                ORDER BY date DESC
            """)
            rows = cur.fetchall()
        conn.close()
        positions = [dict(r) for r in rows]
    except Exception:
        return []

    if not positions:
        return []

    # Fetch current prices + enough history to compute RSI(14) for all open positions
    tickers = list({p["ticker"] for p in positions})
    current_prices = {}
    current_rsi = {}
    try:
        raw = yf.download(tickers, period="3mo", progress=False, auto_adjust=True)
        current_ibs = {}
        current_highs = {}
        current_lows  = {}
        if len(tickers) == 1:
            try:
                close = raw["Close"].dropna()
                high  = raw["High"].dropna()
                low   = raw["Low"].dropna()
                current_prices[tickers[0]] = float(close.iloc[-1])
                current_highs[tickers[0]]  = float(high.iloc[-1])
                current_lows[tickers[0]]   = float(low.iloc[-1])
                delta = close.diff()
                gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
                loss = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
                current_rsi[tickers[0]] = round(float(100 - 100 / (1 + gain / loss)).iloc[-1], 2)
                rng = float(high.iloc[-1]) - float(low.iloc[-1])
                if rng > 0:
                    current_ibs[tickers[0]] = round((float(close.iloc[-1]) - float(low.iloc[-1])) / rng, 3)
            except Exception:
                pass
        else:
            for t in tickers:
                try:
                    close = raw["Close"][t].dropna()
                    high  = raw["High"][t].dropna()
                    low   = raw["Low"][t].dropna()
                    current_prices[t] = float(close.iloc[-1])
                    current_highs[t]  = float(high.iloc[-1])
                    current_lows[t]   = float(low.iloc[-1])
                    delta = close.diff()
                    gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
                    loss = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
                    current_rsi[t] = round(float(100 - 100 / (1 + gain / loss)).iloc[-1], 2)
                    rng = float(high.iloc[-1]) - float(low.iloc[-1])
                    if rng > 0:
                        current_ibs[t] = round((float(close.iloc[-1]) - float(low.iloc[-1])) / rng, 3)
                except Exception:
                    pass
    except Exception:
        pass

    for p in positions:
        cur = current_prices.get(p["ticker"])
        p["current_price"] = cur
        p["current_high"]  = current_highs.get(p["ticker"])
        p["current_low"]   = current_lows.get(p["ticker"])
        p["current_rsi"]   = current_rsi.get(p["ticker"])
        p["current_ibs"]   = current_ibs.get(p["ticker"])
        entry = p.get("price") or 0
        if cur and entry:
            p["unrealized_pnl"] = (cur - entry) / entry * 100
        else:
            p["unrealized_pnl"] = None

    return positions


def _update_peak_price(signal_id: int, new_peak: float):
    """Persist new peak_price if it exceeds the stored value (or was NULL)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signals SET peak_price = %s
                WHERE id = %s AND (peak_price IS NULL OR peak_price < %s)
            """, (new_peak, signal_id, new_peak))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _detect_engine(rationale: str) -> str:
    """Identify which engine generated a signal from its rationale text."""
    r = (rationale or "").lower()
    if "(e6)" in r or "range reversion e6" in r:                          return "E6"
    if "double bottom" in r and "e7" in r:                                return "E7"
    if "(e8b)" in r:                                                       return "E8B"
    if "(e8)" in r:                                                        return "E8"
    if "sector hunter" in r:                                              return "E5"
    if "(e4)" in r or ("regime" in r and "(e1)" not in r):               return "E4"
    if "(e3)" in r or "leveraged" in r or "shock-bounce" in r or "momentum breakout" in r: return "E3"
    if "(e2)" in r or "index fade" in r or "blue diamond" in r:          return "E2"
    if "(e1)" in r:                                                       return "E1"
    return "E1"


# Per-engine exit parameters (mirrors backtest_master.py constants)
# trail_trigger: peak gain % at which trailing stop activates (None = no trail)
# trail_distance: trail gap below peak (e.g. 0.04 = 4% below peak)
# max_hold / max_hold_5star: trading-day hold limit
# stale_cut: trading days before stale cut (None = disabled)
# stale_unconditional: if True, cut regardless of P&L (E6 behavior); else only if loss
_ENGINE_PARAMS = {
    "E1": {"trail_trigger": 0.07, "trail_distance": 0.08, "max_hold": 30, "max_hold_5star": 35, "stale_cut": 12,  "stale_unconditional": False},
    "E2": {"trail_trigger": None, "trail_distance": None, "max_hold": 30, "max_hold_5star": 30, "stale_cut": 20,  "stale_unconditional": False},
    "E3": {"trail_trigger": None, "trail_distance": None, "max_hold": 35, "max_hold_5star": 30, "stale_cut": None, "stale_unconditional": False},
    "E4": {"trail_trigger": 0.17, "trail_distance": 0.10, "max_hold": 90, "max_hold_5star": 90, "stale_cut": None, "stale_unconditional": False},
    "E5": {"trail_trigger": 0.08, "trail_distance": 0.04, "max_hold": 30, "max_hold_5star": 30, "stale_cut": 15,  "stale_unconditional": False},
    "E6": {"trail_trigger": 0.08, "trail_distance": 0.04, "max_hold": 25, "max_hold_5star": 25, "stale_cut": 12,  "stale_unconditional": True},
    "E7":  {"trail_trigger": 0.07, "trail_distance": 0.03, "max_hold": 60, "max_hold_5star": 60, "stale_cut": 35,  "stale_unconditional": False},
    "E8":  {"trail_trigger": None, "trail_distance": None, "max_hold": 40, "max_hold_5star": 40, "stale_cut": None, "stale_unconditional": False},
    "E8B": {"trail_trigger": None, "trail_distance": None, "max_hold": 60, "max_hold_5star": 60, "stale_cut": None, "stale_unconditional": False},
}


def _check_stop_target_hits(open_positions: list[dict]) -> list[dict]:
    """Auto-close any open position that hit its stop, target, trail, floor, max hold, or stale cut."""
    hits = []
    today = datetime.now().strftime("%Y-%m-%d")
    for p in open_positions:
        cur = p.get("current_price")
        if not cur:
            continue
        cur        = float(cur)
        today_low  = float(p.get("current_low")  or cur)
        today_high = float(p.get("current_high") or cur)
        entry      = float(p.get("price") or 0)
        stop       = _parse_price(p.get("stop_loss"))
        target     = _parse_price(p.get("target"))
        conf       = p.get("confidence") or 0
        engine     = _detect_engine(p.get("rationale") or "")
        cfg        = _ENGINE_PARAMS.get(engine, _ENGINE_PARAMS["E1"])

        # ── Update peak_price daily (use today's high; seed with entry if never set) ──
        stored_peak = p.get("peak_price")
        new_peak    = max(today_high, stored_peak or 0, entry or 0)
        if stored_peak is None or today_high > (stored_peak or 0):
            _update_peak_price(p["id"], new_peak)
        peak = new_peak

        # ── Exit checks (priority order matches backtest) ──────────────────────
        hit_type    = None
        trail_level = None

        # 1. Hard stop-loss (intraday low)
        if stop and today_low <= stop:
            hit_type = "STOP"

        # 2. Take-profit (intraday high)
        elif target and today_high >= target:
            hit_type = "TARGET"

        # 3. Floor stop for 5★ entries (intraday low) — E7 uses its own stop, not FLOOR_5STAR
        if hit_type is None and conf >= 5 and FLOOR_5STAR > 0 and entry and engine != "E7":
            if today_low <= entry * (1 - FLOOR_5STAR):
                hit_type = "FLOOR_5STAR"

        # 4. Trailing stop (intraday low vs trail level)
        if hit_type is None and cfg["trail_trigger"] and cfg["trail_distance"] and entry and peak:
            peak_gain = (peak - entry) / entry
            if peak_gain >= cfg["trail_trigger"]:
                trail_level = peak * (1 - cfg["trail_distance"])
                if today_low <= trail_level:
                    hit_type = "TRAIL_STOP"

        # 5. RSI momentum exit (5★ SWING / E1 only)
        rsi_now = p.get("current_rsi")
        if hit_type is None and conf >= 5 and engine == "E1" and rsi_now is not None and rsi_now > 72:
            hit_type = "RSI_EXIT"

        # 6. IBS exit (5★ SWING / E1 only, profitable only)
        ibs_now = p.get("current_ibs")
        if hit_type is None and conf >= 5 and engine == "E1" and ibs_now is not None and ibs_now > IBS_MIN_EXIT and cur > entry:
            hit_type = "IBS_EXIT"

        # 7. Max hold
        if hit_type is None and entry and p.get("date"):
            try:
                days_held = len(pd.bdate_range(str(p["date"])[:10], today))
                max_hold  = cfg["max_hold_5star"] if conf >= 5 else cfg["max_hold"]
                if days_held >= max_hold:
                    hit_type = "MAX_HOLD"
            except Exception:
                pass

        # 8. Stale cut (engine-specific: E6 is unconditional; others require a loss)
        if hit_type is None and cfg["stale_cut"] and entry and p.get("date"):
            try:
                days_held = len(pd.bdate_range(str(p["date"])[:10], today))
                if days_held >= cfg["stale_cut"]:
                    if cfg["stale_unconditional"] or cur < entry:
                        hit_type = "STALE_CUT"
            except Exception:
                pass

        if hit_type:
            # Use exact stop/target/trail level as exit price where available
            if hit_type == "STOP" and stop:
                exit_price = stop
            elif hit_type == "TARGET" and target:
                exit_price = target
            elif hit_type == "TRAIL_STOP" and trail_level:
                exit_price = trail_level
            elif hit_type == "FLOOR_5STAR" and entry:
                exit_price = round(entry * (1 - FLOOR_5STAR), 4)
            else:
                exit_price = cur

            pnl      = (exit_price - entry) / entry * 100 if entry else 0
            pnl_sign = "+" if pnl >= 0 else ""

            trail_pct = int(cfg["trail_distance"] * 100) if cfg["trail_distance"] else 0
            label_map = {
                "TARGET":      "Target hit",
                "STOP":        "Stop loss hit",
                "FLOOR_5STAR": f"Floor stop hit (-{int(FLOOR_5STAR * 100)}%)",
                "TRAIL_STOP":  f"Trailing stop hit (-{trail_pct}% from peak ${peak:.2f})" if peak else "Trailing stop hit",
                "RSI_EXIT":    f"RSI momentum exit (RSI {rsi_now:.1f} > 72)" if rsi_now is not None else "RSI momentum exit",
                "IBS_EXIT":    f"IBS exit (IBS {ibs_now:.2f} > {IBS_MIN_EXIT}) — mean-reversion exhausted" if ibs_now is not None else "IBS exit",
                "MAX_HOLD":    f"Max hold reached ({cfg['max_hold_5star'] if conf >= 5 else cfg['max_hold']}d)",
                "STALE_CUT":   f"Stale cut ({cfg['stale_cut']}d{'  unconditional' if cfg['stale_unconditional'] else ', no gain'})",
            }
            label = label_map.get(hit_type, hit_type)
            try:
                conn = get_conn()
                with conn.cursor() as db_cur:
                    db_cur.execute("""
                        UPDATE signals SET exit_price = %s, exit_date = %s
                        WHERE id = %s AND exit_price IS NULL
                    """, (float(exit_price), today, p["id"]))
                conn.commit()
                conn.close()
            except Exception:
                pass
            save_signal(p["ticker"], {
                "signal":           "SELL",
                "confidence_stars": conf,
                "rationale":        f"{label} [{engine}] — {pnl_sign}{pnl:.2f}% from entry ${entry:.2f}",
                "entry_zone":       None,
                "stop_loss":        p.get("stop_loss"),
                "target":           p.get("target"),
            }, exit_price)
            hits.append({**p, "hit_type": hit_type, "pnl": pnl, "current_price": exit_price})

    return hits


def fetch_price_data(ticker: str) -> pd.DataFrame:
    """Fetch OHLCV history without Streamlit caching."""
    stock = yf.Ticker(ticker)
    df = stock.history(period=HISTORY_DAYS)
    df.index = pd.to_datetime(df.index)
    return df


def _compute_dollar_volume_rank(tickers: list) -> dict:
    """Rank tickers by 20-day average dollar volume (Close × Volume); rank 1 = most liquid.

    Shared liquidity utility so any engine can restrict itself to the most-tradable names and
    avoid the illiquid tail where real slippage bites. One batched download. Returns {ticker: rank}
    for every name with usable data; callers treat a missing ticker as 'not top-ranked'.
    """
    if not tickers:
        return {}
    raw = yf.download(tickers, period="2mo", progress=False, auto_adjust=True, threads=True)
    dv = {}
    if isinstance(raw.columns, pd.MultiIndex):
        close, vol = raw["Close"], raw["Volume"]
        for t in tickers:
            if t in close.columns and t in vol.columns:
                s = (close[t] * vol[t]).dropna()
                if len(s) >= 10:
                    dv[t] = float(s.tail(20).mean())
    else:  # single-ticker frame
        s = (raw["Close"] * raw["Volume"]).dropna()
        if len(s) >= 10:
            dv[tickers[0]] = float(s.tail(20).mean())
    ranked = sorted(dv.items(), key=lambda kv: kv[1], reverse=True)
    return {t: i + 1 for i, (t, _) in enumerate(ranked)}


def run():
    if ANTHROPIC_API_KEY == "your-api-key-here":
        print("ERROR: Set your Anthropic API key in config.py before running.")
        sys.exit(1)

    today    = datetime.now().strftime("%Y-%m-%d")

    removed = deduplicate_open_signals()
    if removed:
        print(f"  🧹 Dedup: removed {len(removed)} duplicate open signal(s): {removed}")

    universe = _get_sp500_tickers() if USE_SP500 else list(WATCHLIST)
    
    # Inject base tickers for Engine 2 & 3
    base_tickers = ["SPY", "QQQ", "TSLA", "NVDA", "RWM", "PSQ"]
    for bt in base_tickers:
        if bt not in universe:
            universe.append(bt)

    # Force-include S&P 500 tickers that may be missed by the Wikipedia scrape
    force_include = ["COHR"]
    for ft in force_include:
        if ft not in universe:
            universe.append(ft)

    source   = f"S&P 500 + Indices ({len(universe)} items)" if USE_SP500 else f"Watchlist ({len(universe)} stocks)"

    # ── Shared liquidity rank (gates Engine 6 to top-N by dollar volume) ────────
    _dv_rank = {}
    if E6_LIQUIDITY_TOP_N and USE_SP500:
        try:
            _dv_rank = _compute_dollar_volume_rank(universe)
        except Exception as _e:
            print(f"  ⚠️  dollar-volume rank failed ({_e}); E6 liquidity gate disabled this run")
    # Only trust the gate if the rank covers most of the universe (else a data hiccup would
    # wrongly block everything) — fall back to no gate on partial failure.
    _dv_gate_active = len(_dv_rank) >= 100
    if _dv_gate_active:
        print(f"  E6 Liquidity: top-{E6_LIQUIDITY_TOP_N} of {len(_dv_rank)} ranked by $-volume")

    # ── Engine 5: pre-compute sector RS rankings ───────────────────────────────
    _sector_map = _get_sp500_sector_map() if USE_SP500 else {}
    _hot_etfs   = compute_hot_sectors()
    _etf_to_sec = {v: k for k, v in E5_SECTOR_ETF_MAP.items()}
    hot_sectors_str = ", ".join(_etf_to_sec.get(e, e) for e in sorted(_hot_etfs)) or "none"

    # ── Market regime check ────────────────────────────────────────────────────
    vix_level   = _get_vix()
    buy_blocked = VIX_MAX is not None and vix_level is not None and vix_level > VIX_MAX
    if vix_level is not None:
        regime_label = f"⚠️  HIGH FEAR — BUY signals SUPPRESSED (VIX {vix_level:.1f} > {VIX_MAX})" if buy_blocked \
                       else f"✅  Normal (VIX {vix_level:.1f} ≤ {VIX_MAX})"
    else:
        regime_label = "VIX unavailable — regime filter inactive"

    # ── Engine 6: pre-fetch SPY df + check E6 consecutive-loss cooldown ──────
    try:
        _spy_df = fetch_price_data("SPY")
    except Exception:
        _spy_df = None
    _e6_paused = _in_e6_cooldown(today)
    e6_label   = f"⏸  PAUSED ({E6_CONSEC_LOSS_LIMIT} consec losses within {E6_LOSS_COOLDOWN_DAYS}d)" \
                 if _e6_paused else "✅  Active"

    star5_paused = _in_5star_cooldown(today)
    star5_label  = f"⏸  PAUSED (≥{CONSEC_5STAR_LOSS_LIMIT} consec losses within {CONSEC_5STAR_COOLDOWN_DAYS}d)" \
                   if star5_paused else "✅  Active"

    # ── Engine 7: SPY > MA50 regime gate ──────────────────────────────────────
    try:
        _spy_close_s = _spy_df["Close"].squeeze().dropna()
        _spy_ma50    = float(_spy_close_s.rolling(50).mean().iloc[-1])
        _spy_now     = float(_spy_close_s.iloc[-1])
        e7_label = f"✅  Active (SPY ${_spy_now:.2f} > MA50 ${_spy_ma50:.2f})" \
                   if _spy_now > _spy_ma50 \
                   else f"⏸  GATED — SPY below MA50 (${_spy_now:.2f} ≤ ${_spy_ma50:.2f})"
    except Exception:
        e7_label = "⚠️  SPY data unavailable"

    print(f"\n{'='*60}")
    print(f"  SwingTrader Daily Run — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Universe: {source}")
    print(f"  Regime:   {regime_label}")
    print(f"  5★ Entry: {star5_label}")
    print(f"  E5 Hot Sectors: {hot_sectors_str}")
    print(f"  E6 Range Rev:   {e6_label}")
    print(f"  E7 Dbl Bottom:  {e7_label}")
    print(f"{'='*60}\n")

    results          = {"BUY": [], "SELL": [], "NO TRADE": [], "ERROR": []}
    notify_signals   = []   # collects signal dicts for BUY/SELL to notify at end
    skipped          = 0
    e7_watching_setups = []

    for ticker in universe:
        # Skip if already processed today
        if _in_cooldown(ticker, today):
            skipped += 1
            continue

        try:
            df = fetch_price_data(ticker)
            if df.empty:
                print(f"  {ticker}: no data")
                continue

            ind  = compute_indicators(df, ticker)

            # Evaluate multiple engines
            generated_signals = []
            _rule_etfs = {"SPY", "QQQ", "RWM", "PSQ", "SPXU", "SQQQ", "UPRO", "TQQQ", "TSLL", "NVDL", "IWM"}

            # Engine 2: Index Fade (rule-based — no RSI dependency)
            sig_fade = get_index_fade_signal(ticker, ind)
            if sig_fade.get("signal") in ("BUY", "SELL"):
                generated_signals.append(sig_fade)

            # Engine 3: Leveraged Shock-Bounce (rule-based — no RSI dependency)
            sig_lev = get_leveraged_signal(ticker, ind)
            if sig_lev.get("signal") in ("BUY", "SELL"):
                generated_signals.append(sig_lev)

            # Engine 4: SPY Regime Momentum (rule-based — SPY only)
            if ticker == "SPY":
                sig_regime = get_regime_signal(ind)
                if sig_regime.get("signal") == "BUY":
                    generated_signals.append(sig_regime)

            # Engine 5: Sector Hunter (rule-based — no RSI dependency)
            _e5_sector = _sector_map.get(ticker)
            if _e5_sector and _hot_etfs:
                sig_e5 = get_sector_hunter_signal(ticker, _e5_sector, ind, _hot_etfs)
                if sig_e5.get("signal") == "BUY":
                    generated_signals.append(sig_e5)

            # Engine 6: Range Reversion (rule-based — no RSI dependency)
            # Liquidity gate: only fire on the top-N most-tradable names (audit: E6 peaks at top-300
            # net of slippage). Disabled automatically if the rank couldn't be computed this run.
            _e6_liquid = (not _dv_gate_active) or (_dv_rank.get(ticker, 10**9) <= E6_LIQUIDITY_TOP_N)
            if ticker not in _rule_etfs and not _e6_paused and _spy_df is not None and _e6_liquid:
                sig_e6 = get_chop_signal(ticker, df, _spy_df)
                if sig_e6.get("signal") == "BUY":
                    generated_signals.append(sig_e6)

            # Engine 7: Double Bottom (rule-based — no RSI dependency)
            if ticker not in _rule_etfs and _spy_df is not None:
                _e7_sector = _sector_map.get(ticker, "")
                sig_e7 = get_pattern_signal(ticker, df, _spy_df, sector=_e7_sector)
                if sig_e7.get("signal") == "BUY":
                    generated_signals.append(sig_e7)
                else:
                    watch = scan_e7_watching(ticker, df, _spy_df, sector=_e7_sector)
                    if watch:
                        e7_watching_setups.append(watch)

            # Engine 1: Core Long Swing (rule-based, no API)
            if ticker not in _rule_etfs:
                generated_signals.append(get_signal(ticker, ind))

            if not generated_signals:
                generated_signals.append({"ticker": ticker, "signal": "NO TRADE", "confidence_stars": 0, "rationale": "No conditions met."})

            base_price = float(ind["latest_close"])

            for sig in generated_signals:
                signal = sig.get("signal", "ERROR")
                conf   = sig.get("confidence_stars", 0)
                target_ticker = sig.get("ticker", ticker)

                # Suppress BUY signals when VIX is elevated (Only applies to Core Swing; Hedges ignore VIX)
                is_core_long = (target_ticker not in ["SPXU", "SQQQ", "IWM", "QQQ", "UPRO", "TQQQ", "TSLL", "NVDL"] and target_ticker == ticker)
                if signal == "BUY" and buy_blocked and is_core_long:
                    signal = "NO TRADE"
                    sig["signal"] = "NO TRADE"

                # Suppress 5★ BUY signals during per-engine consecutive-loss cooldown (E7/E8 exempt)
                _sig_engine = _detect_engine(sig.get("rationale", ""))
                _is_e7_or_e8 = _sig_engine in ("E7", "E8", "E8B")
                if signal == "BUY" and conf >= 5 and is_core_long and not _is_e7_or_e8 and _in_5star_cooldown(today, engine=_sig_engine):
                    print(f"         ⏸  5★ cooldown active ({_sig_engine}) — skipping {target_ticker}")
                    signal = "NO TRADE"
                    sig["signal"] = "NO TRADE"

                # IBS entry filter — mirrors backtest: use YESTERDAY's IBS on base ticker
                # Applies to Engine 1 (core swing) and Engine 3 (leveraged) only.
                # E6/E7 are pattern-based and do not use IBS as an entry gate.
                is_leveraged = target_ticker in ["UPRO", "TQQQ", "TSLL", "NVDL"]
                is_e6_or_e7  = any(k in sig.get("rationale", "") for k in ("DOUBLE BOTTOM", "RANGE REVERSION", "E6", "E7"))
                apply_ibs = (is_core_long or is_leveraged) and not is_e6_or_e7
                if signal == "BUY" and IBS_ENTRY_FILTER and apply_ibs:
                    try:
                        h = float(df["High"].iloc[-2])
                        l = float(df["Low"].iloc[-2])
                        c = float(df["Close"].iloc[-2])
                        rng = h - l
                        if rng > 0:
                            prev_ibs = (c - l) / rng
                            threshold = IBS_MAX_ENTRY_5STAR if conf >= 5 else IBS_MAX_ENTRY_4STAR
                            if prev_ibs >= threshold:
                                print(f"         🚫 IBS filter: prev IBS {prev_ibs:.2f} ≥ {threshold} — skipping {target_ticker}")
                                signal = "NO TRADE"
                                sig["signal"] = "NO TRADE"
                    except Exception:
                        pass

                # Earnings proximity filter — skip BUY within 2 days of earnings (gap-risk)
                if signal == "BUY" and (is_core_long or is_leveraged):
                    dte = ind.get("days_to_earnings")
                    if dte is not None and dte <= 2:
                        print(f"         📅 Earnings filter: {target_ticker} earns in {dte}d — skipping")
                        signal = "NO TRADE"
                        sig["signal"] = "NO TRADE"

                # SELL signals only apply to open BUY positions
                if signal == "SELL":
                    try:
                        conn = get_conn()
                        with conn.cursor() as _c:
                            _c.execute(
                                "SELECT id FROM signals WHERE ticker=%s AND signal='BUY' AND exit_price IS NULL LIMIT 1",
                                (target_ticker,)
                            )
                            has_open_buy = _c.fetchone() is not None
                        conn.close()
                    except Exception:
                        has_open_buy = False
                    if not has_open_buy:
                        signal = "NO TRADE"
                        sig["signal"] = "NO TRADE"

                # Drop sub-3★ signals
                if signal in ("BUY", "SELL") and conf < 3:
                    print(f"         ✂️  Conf {conf}★ < 3★ minimum — skipping {target_ticker}")
                    signal = "NO TRADE"
                    sig["signal"] = "NO TRADE"

                if signal in ("BUY", "SELL"):
                    # Block duplicate: one open position per ticker per engine
                    if signal == "BUY":
                        _eng = _detect_engine(sig.get("rationale", ""))
                        try:
                            conn = get_conn()
                            with conn.cursor() as _dc:
                                _dc.execute(
                                    "SELECT id FROM signals WHERE ticker=%s AND signal='BUY' AND exit_price IS NULL AND rationale ILIKE %s LIMIT 1",
                                    (target_ticker, f"%{_eng}%")
                                )
                                _already_open = _dc.fetchone() is not None
                            conn.close()
                        except Exception as _dup_err:
                            print(f"         ⚠️  Duplicate guard DB error for {target_ticker}: {_dup_err} — allowing signal")
                            _already_open = False
                        if _already_open:
                            print(f"         ⛔ {target_ticker} already has an open {_eng} position — skipping duplicate")
                            signal = "NO TRADE"
                            sig["signal"] = "NO TRADE"

                if signal in ("BUY", "SELL"):
                    # If target ticker is mapped dynamically (e.g. SPY -> UPRO), we must fetch UPRO's price globally
                    if target_ticker != ticker:
                        try:
                            t_df = fetch_price_data(target_ticker)
                            sig_price = float(t_df["Close"].dropna().iloc[-1].item())
                        except Exception:
                            sig_price = base_price 
                    else:
                        sig_price = base_price
                        
                    save_signal(target_ticker, sig, sig_price)
                    signal_dict = {
                        "ticker":     target_ticker,
                        "signal":     signal,
                        "confidence": conf,
                        "price":      sig_price,
                        "entry_zone": sig.get("entry_zone"),
                        "stop_loss":  sig.get("stop_loss"),
                        "target":     sig.get("target"),
                        "rationale":  sig.get("rationale"),
                    }
                    notify_signals.append(signal_dict)

                results[signal if signal in results else "ERROR"].append(target_ticker)

                stars  = "★" * conf + "☆" * (5 - conf)
                eng    = f"[{_detect_engine(sig.get('rationale', ''))}]" if signal != "NO TRADE" else ""
                if signal != "NO TRADE" or target_ticker == ticker:
                    print(f"  {target_ticker:<6}  {signal:<8}  {eng:<6}  {stars}  (Base: ${base_price:.2f})")
                if sig.get("rationale") and signal != "NO TRADE":
                    print(f"         {sig['rationale'][:120]}")

        except Exception as e:
            print(f"  {ticker}: ERROR — {e}")
            results["ERROR"].append(ticker)

    save_e7_watching(e7_watching_setups)

    # ── Engine 8: Chart Pattern Engine (top-100 S&P 500) ─────────────────────
    print(f"\n{'─'*60}")
    print(f"  E8 Chart Pattern Engine scan starting...")
    e8_signals_saved = 0
    try:
        run_scan(verbose=False, send_alerts=False)
        today_e8 = datetime.utcnow().strftime("%Y-%m-%d")
        confirmed_setups = [
            s for s in e8_get_active_setups(stage="CONFIRMED")
            if (s.get("confirmed_at") or "")[:10] == today_e8
        ]
        _E8_PATTERN_LABELS = {
            "InvHnS": "Inv Head & Shoulders", "AscTriangle": "Ascending Triangle",
            "CupHandle": "Cup & Handle", "BullFlag": "Bull Flag", "FallingWedge": "Falling Wedge",
        }
        # Fetch current prices to guard against stale breakouts (>2% above entry = skip)
        _e8_tickers = [s.get("ticker") for s in confirmed_setups if s.get("ticker")]
        _e8_cur_prices = {}
        if _e8_tickers:
            try:
                _e8_raw = yf.download(_e8_tickers, period="2d", auto_adjust=True,
                                      progress=False, threads=True)
                for _t in _e8_tickers:
                    try:
                        _col = _e8_raw["Close"] if len(_e8_tickers) == 1 else _e8_raw["Close"][_t]
                        _e8_cur_prices[_t] = float(_col.dropna().iloc[-1])
                    except Exception:
                        pass
            except Exception:
                pass

        for setup in confirmed_setups:
            _tk, _ptype = setup.get("ticker", ""), setup.get("pattern_type", "")
            _entry = setup.get("entry_price")
            _stop  = setup.get("stop_price")
            _target = setup.get("target_price")
            if not (_entry and _stop and _target):
                continue
            # Skip if ticker already has an open BUY position
            if _in_cooldown(_tk, today):
                print(f"    ⏭  E8 SKIP {_tk:<6} — already has open/recent position")
                continue
            _entry, _stop, _target = float(_entry), float(_stop), float(_target)
            _risk, _reward = _entry - _stop, _target - _entry
            if _risk <= 0 or (_reward / _risk) < 1.15:
                continue
            # Skip if price has moved >2% from entry in either direction (stale breakout)
            _cur = _e8_cur_prices.get(_tk)
            if _cur and abs(_cur - _entry) / _entry > 0.02:
                print(f"    ⏭  E8 SKIP {_tk:<6} — price moved {(_cur/_entry-1)*100:+.1f}% from entry")
                continue
            _vr = setup.get("vol_ratio")
            _vr_str = f" vol {_vr:.1f}x" if _vr else ""
            _label = _E8_PATTERN_LABELS.get(_ptype, _ptype)
            _notes = (setup.get("notes") or "").strip()
            _etag = "(E8B)" if _ptype == "InvHnS" else "(E8)"
            _conf = 6 if _ptype == "InvHnS" else 5
            sig = {
                "signal":           "BUY",
                "confidence_stars": _conf,
                "rationale":        f"Chart pattern breakout: {_label}{_vr_str} {_etag}. Stop ${_stop:.2f}, Target ${_target:.2f}. {_notes}".strip(),
                "entry_zone":       f"${_entry:.2f}",
                "stop_loss":        f"${_stop:.2f}",
                "target":           f"${_target:.2f}",
            }
            save_signal(_tk, sig, _entry)
            results["BUY"].append(_tk)
            notify_signals.append({"ticker": _tk, "signal": "BUY", "confidence": _conf,
                "price": _entry, "entry_zone": sig["entry_zone"],
                "stop_loss": sig["stop_loss"], "target": sig["target"],
                "rationale": sig["rationale"]})
            e8_signals_saved += 1
            print(f"    ✅ {_etag} BUY: {_tk:<6} {_label} | Entry ${_entry:.2f} Stop ${_stop:.2f} Target ${_target:.2f}")
    except Exception as _e8_err:
        print(f"    ⚠️  E8 scan error: {_e8_err}")
        results["ERROR"].append("E8_SCAN")
    # ── End E8 ────────────────────────────────────────────────────────────────

    print(f"\n{'─'*60}")
    print(f"  Universe : {source}")
    print(f"  Skipped  : {skipped} (already run today)")
    print(f"  E7 Watch : {len(e7_watching_setups)} setups saved")
    print(f"  E8 Sigs  : {e8_signals_saved} BUY signal(s) saved")
    print(f"  BUY      ({len(results['BUY'])}): {', '.join(results['BUY']) or '—'}")
    print(f"  SELL     ({len(results['SELL'])}): {', '.join(results['SELL']) or '—'}")
    print(f"  ERRORS   ({len(results['ERROR'])}): {', '.join(results['ERROR']) or '—'}")
    print(f"{'='*60}\n")

    # Check stop/target hits on open positions before sending summary
    open_positions = _get_open_positions()
    hits = _check_stop_target_hits(open_positions)
    if hits:
        send_exit_alert(hits)
        # Refresh positions list after auto-closing hits
        open_positions = _get_open_positions()

    # Daily summary email — always sends (signals or not).
    # perf is optional; never let a transient DB hiccup here suppress notifications.
    try:
        perf = get_performance_stats()
    except Exception as e:
        print(f"  ⚠️  Performance stats unavailable ({e}) — sending notifications without them")
        perf = None
    send_daily_summary(notify_signals, results, source, skipped, open_positions, perf,
                       vix_level=vix_level, vix_max=VIX_MAX)
    send_push(notify_signals)
    # Telegram: 4★/5★ only — explicitly states if none found
    send_daily_telegram(notify_signals)


if __name__ == "__main__":
    run()
