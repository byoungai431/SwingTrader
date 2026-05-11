"""
Chart Analyzer — Daily Scanner
Scans the top-100 S&P 500 universe for developing chart patterns, updates DB state,
and sends Telegram alerts at APPROACHING and CONFIRMED stages.

Run:  python -m chart_analyzer.scanner
"""

from __future__ import annotations


import json
import urllib.request
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

from chart_analyzer.history import (
    init_db,
    get_active_setups,
    upsert_setup,
    mark_alert_sent,
    invalidate_setup,
    get_universe_tickers,
)
from chart_analyzer.universe import get_universe
from chart_analyzer.patterns import ALL_PATTERNS

# ─────────────────────────────────────────────────────────────────────────────
# Stage ordering (higher = more advanced)
# ─────────────────────────────────────────────────────────────────────────────
_STAGE_RANK = {"DETECTED": 1, "WATCHING": 2, "APPROACHING": 3, "CONFIRMED": 4}

# Download window for live scanner
_DATA_PERIOD = "6mo"     # ~130 trading days — enough for all 5 patterns
_BATCH_SIZE  = 50
_MAX_AGE_DAYS = 30       # invalidate setups stuck below APPROACHING for > 30 days


# ─────────────────────────────────────────────────────────────────────────────
# Telegram alert
# ─────────────────────────────────────────────────────────────────────────────

def _load_telegram_creds():
    """Read TELEGRAM_ENABLED/BOT_TOKEN/CHAT_ID from config or secrets.toml."""
    try:
        from config import TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        return TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except Exception:
        return False, "", ""


_PATTERN_LABELS = {
    "InvHnS":      "Inv Head & Shoulders",
    "AscTriangle": "Ascending Triangle",
    "CupHandle":   "Cup & Handle",
    "BullFlag":    "Bull Flag",
    "FallingWedge":"Falling Wedge",
}


def _send_alert(setup: dict, stage: str) -> bool:
    """Send a Telegram alert for an APPROACHING or CONFIRMED pattern setup."""
    enabled, token, chat_id = _load_telegram_creds()
    if not enabled or not token or not chat_id:
        return False

    ticker  = setup.get("ticker", "")
    ptype   = _PATTERN_LABELS.get(setup.get("pattern_type", ""), setup.get("pattern_type", ""))
    stop    = setup.get("stop_price")
    target  = setup.get("target_price")
    entry   = setup.get("entry_price")
    vr      = setup.get("vol_ratio")
    notes   = setup.get("notes", "")

    stop_str   = f"${stop:.2f}"   if stop   else "—"
    target_str = f"${target:.2f}" if target else "—"

    if stage == "APPROACHING":
        icon = "📡"
        header = f"{icon} <b>APPROACHING — {ptype}</b>"
        body = (
            f"<b>{ticker}</b> is setting up\n"
            f"Stop: {stop_str}  |  Target: {target_str}\n"
            f"{notes}"
        )
    else:  # CONFIRMED
        icon = "🔔"
        vr_str = f"{vr:.1f}×" if vr else "—"
        entry_str = f"${entry:.2f}" if entry else "—"
        header = f"{icon} <b>CONFIRMED — {ptype}</b>"
        body = (
            f"<b>{ticker}</b> broke out at {entry_str}\n"
            f"Vol: {vr_str} avg  |  Stop: {stop_str}  |  Target: {target_str}\n"
            f"{notes}"
        )

    text = f"{header}\n\n{body}"
    payload = json.dumps({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print(f"    ✈️  Telegram {stage} alert sent → {ticker} {ptype}")
        return True
    except Exception as e:
        print(f"    ✈️  Telegram FAILED: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SPY regime helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_spy_regime() -> bool:
    """Return True if today's SPY close is above its 50-day MA."""
    spy = yf.download("SPY", period="80d", auto_adjust=True, progress=False)
    if spy.empty:
        return True  # default to bullish if data unavailable
    close = spy["Close"].values.astype(float)
    if len(close) < 50:
        return True
    return bool(close[-1] > np.mean(close[-50:]))


# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────

def _should_invalidate_stale(setup: dict) -> bool:
    """Invalidate setups stuck at DETECTED/WATCHING for more than _MAX_AGE_DAYS."""
    if setup.get("stage") in ("APPROACHING", "CONFIRMED"):
        return False
    detected_at = setup.get("detected_at")
    if not detected_at:
        return False
    try:
        dt = datetime.strptime(detected_at[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.utcnow() - dt).days > _MAX_AGE_DAYS
    except Exception:
        return False


def _handle_setup_result(
    existing: dict | None,
    new_setup: dict | None,
    now_str: str,
) -> tuple[dict | None, bool, bool]:
    """
    Decide what to upsert and whether to send alerts.

    Returns:
        (setup_to_upsert, send_approaching_alert, send_confirmed_alert)
    """
    if new_setup is None:
        # No pattern detected — check if existing needs invalidation
        if existing and _should_invalidate_stale(existing):
            return {"__invalidate__": True, "id": existing["id"]}, False, False
        return None, False, False

    new_stage  = new_setup.get("stage", "DETECTED")
    new_rank   = _STAGE_RANK.get(new_stage, 0)

    if existing is None:
        # Fresh detection
        setup = {**new_setup, "detected_at": now_str}
        if new_stage == "APPROACHING":
            setup["approaching_at"] = now_str
        elif new_stage == "CONFIRMED":
            setup["approaching_at"] = now_str
            setup["confirmed_at"]   = now_str
        send_a = new_stage == "APPROACHING"
        send_c = new_stage == "CONFIRMED"
        return setup, send_a, send_c

    # Existing setup — only progress forward, never regress
    old_rank = _STAGE_RANK.get(existing.get("stage", "DETECTED"), 0)
    if new_rank < old_rank:
        # Stage regressed (noise) — keep existing, no alerts
        return None, False, False

    setup = {**new_setup, "detected_at": existing.get("detected_at")}

    # Carry forward timestamps that have already been set
    if existing.get("approaching_at"):
        setup["approaching_at"] = existing["approaching_at"]
    if existing.get("confirmed_at"):
        setup["confirmed_at"] = existing["confirmed_at"]

    # Set new timestamps for newly reached stages
    send_a = False
    send_c = False

    if new_stage in ("APPROACHING", "CONFIRMED") and not existing.get("approaching_at"):
        setup["approaching_at"] = now_str
        send_a = not existing.get("alert_sent_approaching")

    if new_stage == "CONFIRMED" and not existing.get("confirmed_at"):
        setup["confirmed_at"] = now_str
        send_c = not existing.get("alert_sent_confirmed")

    # Carry alert-sent flags
    setup["alert_sent_approaching"] = existing.get("alert_sent_approaching", 0)
    setup["alert_sent_confirmed"]   = existing.get("alert_sent_confirmed", 0)

    return setup, send_a, send_c


# ─────────────────────────────────────────────────────────────────────────────
# Main scan
# ─────────────────────────────────────────────────────────────────────────────

def run_scan(verbose: bool = True) -> dict:
    """
    Full daily chart pattern scan.

    1. Ensures DB tables exist.
    2. Loads top-100 universe (cached or recomputed).
    3. Downloads SPY for regime gate.
    4. Downloads per-ticker OHLCV in batches.
    5. Runs all 5 pattern detectors per ticker.
    6. Upserts DB state, sends Telegram alerts.
    7. Returns a summary dict.
    """
    init_db()

    now_str  = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    if verbose:
        print(f"\n{'='*56}")
        print(f"Chart Analyzer — Daily Scan  [{date_str}]")
        print(f"{'='*56}")

    universe = get_universe()
    if verbose:
        print(f"Universe: {len(universe)} tickers")

    spy_above = _get_spy_regime()
    if verbose:
        print(f"SPY regime: {'BULL (above MA50)' if spy_above else 'BEAR (below MA50)'}")

    # Load all current active setups into a lookup dict
    existing_setups: dict[tuple, dict] = {}
    for s in get_active_setups():
        existing_setups[(s["ticker"], s["pattern_type"])] = s

    # Instantiate all detectors
    detectors = [cls() for cls in ALL_PATTERNS]

    # Summary counters
    summary = {
        "confirmed":   [],
        "approaching": [],
        "watching":    [],
        "detected":    [],
        "alerts_sent": 0,
        "tickers_scanned": 0,
        "errors": 0,
    }

    n_batches = (len(universe) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for batch_num in range(n_batches):
        batch = universe[batch_num * _BATCH_SIZE : (batch_num + 1) * _BATCH_SIZE]
        if verbose:
            print(f"\n  Batch {batch_num + 1}/{n_batches}: {len(batch)} tickers...")

        raw = yf.download(
            batch,
            period=_DATA_PERIOD,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        if raw.empty:
            continue

        for ticker in batch:
            try:
                # Extract single-ticker DataFrame
                if len(batch) == 1:
                    df = raw.copy()
                else:
                    df = raw.xs(ticker, axis=1, level=1).copy()

                df = df.dropna(subset=["Close", "High", "Low", "Volume"])
                if len(df) < 30:
                    continue

                summary["tickers_scanned"] += 1

                for detector in detectors:
                    ptype = detector.PATTERN_TYPE
                    key   = (ticker, ptype)

                    try:
                        new_setup = detector.scan(ticker, df, spy_above_ma50=spy_above)
                    except Exception as e:
                        print(f"    ⚠️  {ticker}/{ptype} detector error: {e}")
                        summary["errors"] += 1
                        new_setup = None

                    existing = existing_setups.get(key)

                    to_upsert, send_a, send_c = _handle_setup_result(
                        existing, new_setup, now_str
                    )

                    if to_upsert is None:
                        continue

                    if to_upsert.get("__invalidate__"):
                        invalidate_setup(to_upsert["id"], reason="stale — no progression")
                        if key in existing_setups:
                            del existing_setups[key]
                        continue

                    row_id = upsert_setup(to_upsert)

                    # Update our in-memory lookup with new state
                    existing_setups[key] = {**to_upsert, "id": row_id}

                    stage = to_upsert.get("stage", "DETECTED")

                    # Track in summary
                    entry = {"ticker": ticker, "pattern_type": ptype, "stage": stage}
                    if stage == "CONFIRMED":
                        summary["confirmed"].append(entry)
                    elif stage == "APPROACHING":
                        summary["approaching"].append(entry)
                    elif stage == "WATCHING":
                        summary["watching"].append(entry)
                    else:
                        summary["detected"].append(entry)

                    # Send alerts
                    if send_a:
                        if _send_alert(to_upsert, "APPROACHING"):
                            mark_alert_sent(row_id, "APPROACHING")
                            summary["alerts_sent"] += 1

                    if send_c:
                        if _send_alert(to_upsert, "CONFIRMED"):
                            mark_alert_sent(row_id, "CONFIRMED")
                            summary["alerts_sent"] += 1

                    if verbose and stage in ("APPROACHING", "CONFIRMED"):
                        label = _PATTERN_LABELS.get(ptype, ptype)
                        print(f"    {'🔔' if stage == 'CONFIRMED' else '📡'} {ticker:6s} {label:<22} [{stage}]")

            except Exception as e:
                print(f"    ⚠️  {ticker} error: {e}")
                summary["errors"] += 1
                continue

    if verbose:
        _print_summary(summary)

    return summary


def _print_summary(s: dict):
    print(f"\n{'='*56}")
    print(f"Scan Complete")
    print(f"{'='*56}")
    print(f"  Tickers scanned : {s['tickers_scanned']}")
    print(f"  Confirmed       : {len(s['confirmed'])}")
    print(f"  Approaching     : {len(s['approaching'])}")
    print(f"  Watching        : {len(s['watching'])}")
    print(f"  Detected        : {len(s['detected'])}")
    print(f"  Alerts sent     : {s['alerts_sent']}")
    if s["errors"]:
        print(f"  Errors          : {s['errors']}")

    if s["confirmed"]:
        print("\n  🔔 CONFIRMED setups:")
        for e in s["confirmed"]:
            label = _PATTERN_LABELS.get(e["pattern_type"], e["pattern_type"])
            print(f"      {e['ticker']:6s}  {label}")

    if s["approaching"]:
        print("\n  📡 APPROACHING setups:")
        for e in s["approaching"]:
            label = _PATTERN_LABELS.get(e["pattern_type"], e["pattern_type"])
            print(f"      {e['ticker']:6s}  {label}")

    print(f"{'='*56}\n")


if __name__ == "__main__":
    run_scan()
