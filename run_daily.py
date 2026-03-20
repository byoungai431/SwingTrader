"""
run_daily.py — Headless daily signal runner.

Fetches price data, computes indicators, calls Claude for signals,
and saves BUY/SELL results to the SQLite history DB.

Run manually:  python3 run_daily.py
Scheduled via: ~/Library/LaunchAgents/com.swingtrader.daily.plist
"""

import sys
import time
import os
from datetime import datetime

import psycopg2.extras
import yfinance as yf
import pandas as pd

from config import WATCHLIST, HISTORY_DAYS, ANTHROPIC_API_KEY, VIX_MAX
from indicators import compute_indicators
from fundamentals import fetch_fundamentals
from signal_engine import get_signal
from history import save_signal, get_performance_stats, get_conn
from notify import send_telegram, send_daily_summary, send_push, send_exit_alert

# ── Set to False to fall back to watchlist only ───────────────────────────────
USE_SP500 = True

# ── Exit parameters (mirrors backtest.py config) ───────────────────────────────
FLOOR_5STAR    = 0.10  # Exit 5★ if trade drops this % from entry (0 = disabled)
MAX_HOLD_DAYS  = 30    # Trading-day hold limit for 3★/4★ trades
MAX_HOLD_5STAR = 35    # Trading-day hold limit for 5★ trades
STALE_CUT_DAYS = 12    # Exit after N trading days if gain < 0% (0 = disabled; 5★ exempt)


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
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        tickers = pd.read_html(html)[0]["Symbol"].tolist()
        # Normalize BRK.B → BRK-B style used by yfinance
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        print(f"  WARNING: Could not fetch S&P 500 list ({e}). Falling back to watchlist.")
        return list(WATCHLIST)


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
                SELECT id, ticker, date, confidence, price, stop_loss, target
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
        if len(tickers) == 1:
            try:
                close = raw["Close"].dropna()
                current_prices[tickers[0]] = float(close.iloc[-1])
                delta = close.diff()
                gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
                loss = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
                current_rsi[tickers[0]] = round(float(100 - 100 / (1 + gain / loss)).iloc[-1], 2)
            except Exception:
                pass
        else:
            for t in tickers:
                try:
                    close = raw["Close"][t].dropna()
                    current_prices[t] = float(close.iloc[-1])
                    delta = close.diff()
                    gain = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
                    loss = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
                    current_rsi[t] = round(float(100 - 100 / (1 + gain / loss)).iloc[-1], 2)
                except Exception:
                    pass
    except Exception:
        pass

    for p in positions:
        cur = current_prices.get(p["ticker"])
        p["current_price"] = cur
        p["current_rsi"] = current_rsi.get(p["ticker"])
        entry = p.get("price") or 0
        if cur and entry:
            p["unrealized_pnl"] = (cur - entry) / entry * 100
        else:
            p["unrealized_pnl"] = None

    return positions


def _check_stop_target_hits(open_positions: list[dict]) -> list[dict]:
    """Auto-close any position that hit its stop loss, target, floor, max hold, or stale cut."""
    hits = []
    today = datetime.now().strftime("%Y-%m-%d")
    for p in open_positions:
        cur = p.get("current_price")
        if not cur:
            continue
        cur = float(cur)
        entry = p.get("price") or 0
        stop   = _parse_price(p.get("stop_loss"))
        target = _parse_price(p.get("target"))
        conf   = p.get("confidence") or 0

        hit_type = None
        if stop and cur <= stop:
            hit_type = "STOP"
        elif target and cur >= target:
            hit_type = "TARGET"

        # 5★ floor stop: exit if down FLOOR_5STAR% from entry (5★ have no regular stop)
        if hit_type is None and conf == 5 and FLOOR_5STAR > 0 and entry:
            if cur <= entry * (1 - FLOOR_5STAR):
                hit_type = "FLOOR_5STAR"

        # 5★ RSI momentum exit: exit when RSI > 72 (mirrors backtest RSI_5STAR_EXIT)
        rsi_now = p.get("current_rsi")
        if hit_type is None and conf == 5 and rsi_now is not None and rsi_now > 72:
            hit_type = "RSI_EXIT"

        # Max hold: exit after N trading days (30 for 3★/4★, 35 for 5★)
        if hit_type is None and entry and p.get("date"):
            try:
                days_held = len(pd.bdate_range(str(p["date"])[:10], today))
                max_hold = MAX_HOLD_5STAR if conf == 5 else MAX_HOLD_DAYS
                if days_held >= max_hold:
                    hit_type = "MAX_HOLD"
            except Exception:
                pass

        # Stale cut: exit after STALE_CUT_DAYS if gain < 0% (5★ exempt)
        if hit_type is None and conf != 5 and entry and p.get("date") and STALE_CUT_DAYS > 0:
            try:
                days_held = len(pd.bdate_range(str(p["date"])[:10], today))
                if days_held >= STALE_CUT_DAYS and cur < entry:
                    hit_type = "STALE_CUT"
            except Exception:
                pass

        if hit_type:
            pnl = (cur - entry) / entry * 100 if entry else 0
            pnl_sign = "+" if pnl >= 0 else ""
            label_map = {
                "TARGET":      "Target hit",
                "STOP":        "Stop loss hit",
                "FLOOR_5STAR": f"Floor stop hit (-{int(FLOOR_5STAR * 100)}%)",
                "RSI_EXIT":    f"RSI momentum exit (RSI {rsi_now:.1f} > 72)",
                "MAX_HOLD":    "Max hold reached",
                "STALE_CUT":   f"Stale cut (no gain after {STALE_CUT_DAYS}d)",
            }
            label = label_map.get(hit_type, hit_type)
            try:
                conn = get_conn()
                with conn.cursor() as db_cur:
                    db_cur.execute("""
                        UPDATE signals SET exit_price = %s, exit_date = %s
                        WHERE id = %s AND exit_price IS NULL
                    """, (float(cur), today, p["id"]))
                conn.commit()
                conn.close()
            except Exception:
                pass
            # Log a SELL row so history shows why the position was closed
            save_signal(p["ticker"], {
                "signal":           "SELL",
                "confidence_stars": conf,
                "rationale":        f"{label} — {pnl_sign}{pnl:.2f}% from entry ${entry:.2f}",
                "entry_zone":       None,
                "stop_loss":        p.get("stop_loss"),
                "target":           p.get("target"),
            }, cur)
            hits.append({**p, "hit_type": hit_type, "pnl": pnl})

    return hits


def fetch_price_data(ticker: str) -> pd.DataFrame:
    """Fetch OHLCV history without Streamlit caching."""
    stock = yf.Ticker(ticker)
    df = stock.history(period=HISTORY_DAYS)
    df.index = pd.to_datetime(df.index)
    return df


def run():
    if ANTHROPIC_API_KEY == "your-api-key-here":
        print("ERROR: Set your Anthropic API key in config.py before running.")
        sys.exit(1)

    today    = datetime.now().strftime("%Y-%m-%d")
    universe = _get_sp500_tickers() if USE_SP500 else list(WATCHLIST)
    source   = f"S&P 500 ({len(universe)} stocks)" if USE_SP500 else f"Watchlist ({len(universe)} stocks)"

    # ── Market regime check ────────────────────────────────────────────────────
    vix_level   = _get_vix()
    buy_blocked = VIX_MAX is not None and vix_level is not None and vix_level > VIX_MAX
    if vix_level is not None:
        regime_label = f"⚠️  HIGH FEAR — BUY signals SUPPRESSED (VIX {vix_level:.1f} > {VIX_MAX})" if buy_blocked \
                       else f"✅  Normal (VIX {vix_level:.1f} ≤ {VIX_MAX})"
    else:
        regime_label = "VIX unavailable — regime filter inactive"

    print(f"\n{'='*60}")
    print(f"  SwingTrader Daily Run — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Universe: {source}")
    print(f"  Regime:   {regime_label}")
    print(f"{'='*60}\n")

    results          = {"BUY": [], "SELL": [], "NO TRADE": [], "ERROR": []}
    notify_signals   = []   # collects signal dicts for BUY/SELL to notify at end
    skipped          = 0
    rsi_filtered     = 0    # stocks skipped due to neutral RSI (40–60)

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

            ind  = compute_indicators(df)

            # Skip neutral-RSI stocks — Claude almost never signals here
            rsi = ind.get("rsi")
            if rsi is not None and 40 < float(rsi) < 60:
                rsi_filtered += 1
                continue

            fund = fetch_fundamentals(ticker)
            sig  = get_signal(ticker, ind, fund)

            signal = sig.get("signal", "ERROR")
            conf   = sig.get("confidence_stars", 0)
            price  = float(ind["latest_close"])

            # Suppress BUY signals when VIX is elevated
            if signal == "BUY" and buy_blocked:
                signal = "NO TRADE"
                sig["signal"] = "NO TRADE"

            # SELL signals only apply to open BUY positions — skip if none exists
            if signal == "SELL":
                try:
                    conn = get_conn()
                    with conn.cursor() as _c:
                        _c.execute(
                            "SELECT id FROM signals WHERE ticker=%s AND signal='BUY' AND exit_price IS NULL LIMIT 1",
                            (ticker,)
                        )
                        has_open_buy = _c.fetchone() is not None
                    conn.close()
                except Exception:
                    has_open_buy = False
                if not has_open_buy:
                    signal = "NO TRADE"
                    sig["signal"] = "NO TRADE"

            if signal in ("BUY", "SELL"):
                save_signal(ticker, sig, price)
                signal_dict = {
                    "ticker":     ticker,
                    "signal":     signal,
                    "confidence": conf,
                    "price":      price,
                    "entry_zone": sig.get("entry_zone"),
                    "stop_loss":  sig.get("stop_loss"),
                    "target":     sig.get("target"),
                    "rationale":  sig.get("rationale"),
                }
                notify_signals.append(signal_dict)
                # Only push immediate alert for 4★/5★ BUY signals
                if signal == "BUY" and conf >= 4:
                    send_telegram([signal_dict])

            results[signal if signal in results else "ERROR"].append(ticker)

            stars = "★" * conf + "☆" * (5 - conf)
            print(f"  {ticker:<6}  {signal:<8}  {stars}  ${price:.2f}")
            if sig.get("rationale"):
                print(f"         {sig['rationale'][:90]}")

            # Brief pause to stay within API rate limits
            time.sleep(0.5)

        except Exception as e:
            print(f"  {ticker}: ERROR — {e}")
            results["ERROR"].append(ticker)

    print(f"\n{'─'*60}")
    print(f"  Universe : {source}")
    print(f"  Skipped  : {skipped} (already run today)")
    print(f"  Filtered : {rsi_filtered} (RSI 40–60, neutral zone)")
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

    # Daily summary email — always sends (signals or not)
    perf = get_performance_stats()
    send_daily_summary(notify_signals, results, source, skipped, open_positions, perf,
                       vix_level=vix_level, vix_max=VIX_MAX)
    send_push(notify_signals)


if __name__ == "__main__":
    run()
