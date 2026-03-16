"""
export_results.py
Exports all signals from signals.db to a plain text file in 'backtest results/'.
Run: /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 export_results.py
"""

import os
import sqlite3
from datetime import datetime
import yfinance as yf

BASE_DIR   = os.path.dirname(__file__)
DB_PATH    = os.path.join(BASE_DIR, "data", "signals.db")
OUT_DIR    = os.path.join(BASE_DIR, "backtest results")
os.makedirs(OUT_DIR, exist_ok=True)

TRADE_SIZE = 10_000
TODAY      = datetime.today().strftime("%Y-%m-%d")
OUT_FILE   = os.path.join(OUT_DIR, f"signal_results_{TODAY}.txt")

# ── Fetch current prices ──────────────────────────────────────────────────────
def get_current_prices(tickers):
    if not tickers:
        return {}
    raw = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
    prices = {}
    if len(tickers) == 1:
        try:
            prices[tickers[0]] = float(raw["Close"].dropna().iloc[-1])
        except Exception:
            pass
    else:
        for t in tickers:
            try:
                prices[t] = float(raw["Close"][t].dropna().iloc[-1])
            except Exception:
                pass
    return prices

# ── Load signals ──────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT id, ticker, date, signal, confidence,
           entry_zone, stop_loss, target, price,
           exit_price, exit_date, rationale
    FROM signals ORDER BY date ASC, id ASC
""").fetchall()
conn.close()

open_tickers   = list({r["ticker"] for r in rows if not r["exit_date"]})
current_prices = get_current_prices(open_tickers)

# ── Build trade records ───────────────────────────────────────────────────────
trades = []
for r in rows:
    entry  = r["price"]
    closed = bool(r["exit_date"])
    exit_p = r["exit_price"] if closed else current_prices.get(r["ticker"])

    if entry and exit_p:
        pnl_pct    = (exit_p - entry) / entry if r["signal"] == "BUY" else (entry - exit_p) / entry
        pnl_dollar = pnl_pct * TRADE_SIZE
    else:
        pnl_pct = pnl_dollar = None

    trades.append({
        "id":          r["id"],
        "ticker":      r["ticker"],
        "signal":      r["signal"],
        "date":        r["date"],
        "confidence":  "★" * int(r["confidence"]) + "☆" * (5 - int(r["confidence"])) if r["confidence"] else "—",
        "entry_price": entry,
        "entry_zone":  r["entry_zone"] or "—",
        "stop_loss":   r["stop_loss"] or "—",
        "target":      r["target"] or "—",
        "exit_price":  exit_p,
        "exit_date":   r["exit_date"] or "Open",
        "status":      "Closed" if closed else "Open",
        "pnl_pct":     pnl_pct,
        "pnl_dollar":  pnl_dollar,
        "rationale":   r["rationale"] or "—",
    })

# ── Totals ────────────────────────────────────────────────────────────────────
closed_trades    = [t for t in trades if t["status"] == "Closed" and t["pnl_dollar"] is not None]
open_trades      = [t for t in trades if t["status"] == "Open"   and t["pnl_dollar"] is not None]
closed_pnl       = [t["pnl_dollar"] for t in closed_trades]
wins             = [p for p in closed_pnl if p > 0]
losses           = [p for p in closed_pnl if p <= 0]
total_realized   = sum(closed_pnl) if closed_pnl else 0
total_unrealized = sum(t["pnl_dollar"] for t in open_trades if t["pnl_dollar"] is not None)
total_combined   = total_realized + total_unrealized
win_rate         = len(wins) / len(closed_pnl) * 100 if closed_pnl else 0

# ── Write file ────────────────────────────────────────────────────────────────
DIV  = "=" * 80
DIV2 = "-" * 80

lines = []
lines.append(DIV)
lines.append("  TO THE MOON — AI Swing Trading Signal Results")
lines.append(f"  Generated : {TODAY}")
lines.append(f"  Trade size: ${TRADE_SIZE:,} assumed per signal")
lines.append(DIV)
lines.append("")

# ── Summary ───────────────────────────────────────────────────────────────────
lines.append("PERFORMANCE SUMMARY")
lines.append(DIV2)
if rows:
    lines.append(f"  Backtest Period  : {rows[0]['date']} → {TODAY}")
lines.append(f"  Total Signals    : {len(trades)}")
lines.append(f"  Closed Trades    : {len(closed_trades)}")
lines.append(f"  Open Trades      : {len(open_trades)}")
lines.append(f"  Win Rate (closed): {win_rate:.1f}%")
lines.append(f"  Avg Win          : ${sum(wins)/len(wins):+,.2f}" if wins else "  Avg Win          : —")
lines.append(f"  Avg Loss         : ${sum(losses)/len(losses):+,.2f}" if losses else "  Avg Loss          : —")
lines.append(f"  Realized P&L     : ${total_realized:+,.2f}")
lines.append(f"  Unrealized P&L   : ${total_unrealized:+,.2f}  (open positions, live price)")
lines.append(f"  Combined P&L     : ${total_combined:+,.2f}")
lines.append("")

# ── Trade log ─────────────────────────────────────────────────────────────────
lines.append("TRADE LOG")
lines.append(DIV2)
lines.append("")

for t in trades:
    pnl_str = f"{t['pnl_pct']:+.2%}  /  ${t['pnl_dollar']:+,.2f}" if t["pnl_pct"] is not None else "Pending"
    lines.append(f"  [{t['id']}]  {t['ticker']}  |  {t['signal']}  |  {t['date']}  |  {t['confidence']}  |  {t['status']}")
    lines.append(f"      Entry Price  : ${t['entry_price']:.2f}" if t['entry_price'] else "      Entry Price  : —")
    lines.append(f"      Entry Zone   : {t['entry_zone']}")
    lines.append(f"      Stop Loss    : {t['stop_loss']}")
    lines.append(f"      Target       : {t['target']}")
    exit_p_str = f"${t['exit_price']:.2f}" if t['exit_price'] else "—"
    lines.append(f"      Exit Price   : {exit_p_str}  (Exit Date: {t['exit_date']})")
    lines.append(f"      P&L          : {pnl_str}")
    lines.append(f"      Rationale    : {t['rationale']}")
    lines.append("")

lines.append(DIV)
lines.append("  End of Report")
lines.append(DIV)

with open(OUT_FILE, "w") as f:
    f.write("\n".join(lines))

print(f"✅  Saved: {OUT_FILE}")
print(f"   {len(trades)} signals | {len(closed_trades)} closed | {len(open_trades)} open")
print(f"   Realized P&L:   ${total_realized:+,.2f}")
print(f"   Unrealized P&L: ${total_unrealized:+,.2f}")
print(f"   Combined P&L:   ${total_combined:+,.2f}")
