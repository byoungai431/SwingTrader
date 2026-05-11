"""
chart_analyzer/alerts.py — Daily digest alert for the Chart Analyzer.

Sends a single Telegram message summarising all active CONFIRMED and
APPROACHING pattern setups.  Intended to be called once at end-of-scan.

Usage (from scanner or cron):
    from chart_analyzer.alerts import send_daily_digest
    send_daily_digest()
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from chart_analyzer.history import get_active_setups

_PATTERN_LABELS = {
    "InvHnS":      "Inv H&S",
    "AscTriangle": "Asc Triangle",
    "CupHandle":   "Cup & Handle",
    "BullFlag":    "Bull Flag",
    "FallingWedge":"Falling Wedge",
}


def _load_telegram_creds() -> tuple[bool, str, str]:
    try:
        from config import TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        return bool(TELEGRAM_ENABLED), str(TELEGRAM_BOT_TOKEN), str(TELEGRAM_CHAT_ID)
    except Exception:
        return False, "", ""


def _send(text: str) -> bool:
    enabled, token, chat_id = _load_telegram_creds()
    if not enabled or not token or not chat_id:
        return False
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
        print(f"  ✈️  Chart Analyzer daily digest sent.")
        return True
    except Exception as e:
        print(f"  ✈️  Digest send FAILED: {e}")
        return False


def _fmt_setup(s: dict) -> str:
    ticker  = s.get("ticker", "")
    ptype   = _PATTERN_LABELS.get(s.get("pattern_type", ""), s.get("pattern_type", ""))
    stop    = s.get("stop_price")
    target  = s.get("target_price")
    entry   = s.get("entry_price")
    vr      = s.get("vol_ratio")

    stop_s   = f"${stop:.2f}"   if stop   else "—"
    tgt_s    = f"${target:.2f}" if target else "—"
    entry_s  = f"${entry:.2f}"  if entry  else ""
    vr_s     = f" {vr:.1f}×vol" if vr     else ""

    entry_part = f" · entry {entry_s}{vr_s}" if entry_s else ""
    return f"  <b>{ticker}</b> — {ptype}{entry_part} · Stop {stop_s} · T {tgt_s}"


def send_daily_digest() -> bool:
    """
    Fetch all active CONFIRMED and APPROACHING setups and send a
    single digest Telegram message.  Returns True if message sent.
    """
    confirmed   = get_active_setups(stage="CONFIRMED")
    approaching = get_active_setups(stage="APPROACHING")

    date_str = datetime.now().strftime("%b %d, %Y")

    lines = [f"📊 <b>Chart Analyzer — {date_str}</b>\n"]

    if confirmed:
        lines.append(f"🔔 <b>CONFIRMED ({len(confirmed)})</b>")
        for s in confirmed:
            lines.append(_fmt_setup(s))
        lines.append("")

    if approaching:
        lines.append(f"📡 <b>APPROACHING ({len(approaching)})</b>")
        for s in approaching:
            lines.append(_fmt_setup(s))
        lines.append("")

    if not confirmed and not approaching:
        lines.append("No CONFIRMED or APPROACHING patterns today.")

    lines.append('<i>Informational only — not a trade signal.</i>')

    return _send("\n".join(lines))


if __name__ == "__main__":
    send_daily_digest()
