"""
notify.py — Email, push, and Telegram notification sender for SwingTrader signals.

Email:    uses Gmail SMTP with an App Password (no extra dependencies).
Push:     uses ntfy.sh — free, no account needed, works on iOS & Android.
Telegram: uses the Telegram Bot API — free, instant, works on all platforms.
"""

import html
import smtplib
import urllib.request
import urllib.error
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

try:
    from config import (
        EMAIL_ENABLED, EMAIL_FROM, EMAIL_APP_PASSWORD, EMAIL_TO,
        NTFY_ENABLED, NTFY_TOPIC,
        TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
        TELEGRAM_GROUP_CHAT_ID,
    )
except ImportError:
    EMAIL_ENABLED          = False
    NTFY_ENABLED           = False
    TELEGRAM_ENABLED       = False
    TELEGRAM_GROUP_CHAT_ID = ""


# ── Daily summary email ───────────────────────────────────────────────────────

_CSS = """
  body      { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
              background: #0a0a28; color: #c8c8ff; margin: 0; padding: 20px; }
  .header   { background: linear-gradient(135deg, #1a1060, #0d0840);
              border: 1px solid #3a2a9a; border-radius: 12px;
              padding: 20px 24px; margin-bottom: 20px; }
  .header h1{ margin: 0; font-size: 22px; color: #a090ff; letter-spacing: 2px; }
  .header p { margin: 6px 0 0; font-size: 13px; color: #7870cc; }
  .stats    { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat     { background: #0e0e36; border: 1px solid #2a1e78; border-radius: 10px;
              padding: 12px 18px; flex: 1; min-width: 100px; text-align: center; }
  .stat .num{ font-size: 24px; font-weight: 700; color: #a090ff; }
  .stat .lbl{ font-size: 11px; color: #6060aa; letter-spacing: 1px;
              text-transform: uppercase; margin-top: 2px; }
  .section  { margin-bottom: 20px; }
  .sec-hdr  { font-size: 12px; letter-spacing: 2px; text-transform: uppercase;
              color: #7060dd; margin: 0 0 10px; font-weight: 700; }
  .card     { background: #0e0e36; border: 1px solid #2a1e78; border-radius: 10px;
              padding: 14px 18px; margin-bottom: 10px; }
  .card-top { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .ticker   { font-size: 20px; font-weight: 700; color: #fff; }
  .badge    { display: inline-block; padding: 2px 10px; border-radius: 20px;
              font-size: 11px; font-weight: 700; letter-spacing: 1px; }
  .buy      { background: rgba(0,220,130,0.15); color: #00dc82;
              border: 1px solid rgba(0,220,130,0.4); }
  .sell     { background: rgba(255,80,80,0.15); color: #ff5050;
              border: 1px solid rgba(255,80,80,0.4); }
  .hold     { background: rgba(160,144,255,0.15); color: #a090ff;
              border: 1px solid rgba(160,144,255,0.4); }
  .stars    { color: #ffd700; font-size: 14px; }
  .stars-max{ color: #4dd0e1; font-size: 14px; }
  .stars-4  { color: #b0bec5; font-size: 14px; }
  .meta     { font-size: 12px; color: #9090bb; line-height: 1.9; }
  .meta b   { color: #c8c8ff; font-weight: 600; }
  .rationale{ font-size: 12px; color: #8880aa; margin-top: 8px;
              padding-top: 8px; border-top: 1px solid #1c1c52; line-height: 1.6; }
  .notrade  { background: #080820; border: 1px solid #1a1a44; border-radius: 10px;
              padding: 16px 20px; color: #5050aa; font-size: 13px; text-align: center; }
  .pnl-pos  { color: #00dc82; font-weight: 700; }
  .pnl-neg  { color: #ff5050; font-weight: 700; }
  .footer   { font-size: 11px; color: #4a4a88; text-align: center;
              margin-top: 24px; padding-top: 16px; border-top: 1px solid #1c1c52; }
"""


def _tier_label(conf: int) -> str:
    """Text tier label for Telegram/push, matching the app's tier badges (app.py stars())."""
    conf = int(conf or 0)
    if conf >= 6:
        return "🔷 TIER 1 · High Conviction"
    if conf >= 5:
        return "🟡 TIER 2 · Confident"
    if conf >= 4:
        return "⚪ TIER 3 · Qualified"
    if conf >= 3:
        return "🟤 TIER 4 · Range Reversion"
    return "—"


def _tier_html(conf: int) -> str:
    """Styled HTML tier badge for the email, matching the app's tier colors (app.py conf_stars_html())."""
    conf = int(conf or 0)
    if conf >= 6:
        color, text = "#40E0FF", "TIER 1 · High Conviction"
    elif conf >= 5:
        color, text = "#f9c846", "TIER 2 · Confident"
    elif conf >= 4:
        color, text = "#a8a8c0", "TIER 3 · Qualified"
    elif conf >= 3:
        color, text = "#cd7f32", "TIER 4 · Range Reversion"
    else:
        return '<span style="color:#5a5a7a;font-weight:600;">—</span>'
    return (f'<span style="color:{color};font-weight:800;letter-spacing:1.5px;'
            f'font-size:12px;">{text}</span>')


def _confidence_caveat(conf: int) -> str:
    """One-line reason a sub-Tier-1 signal isn't maximal conviction. Empty for Tier 1 (conf>=6)."""
    conf = int(conf or 0)
    if conf >= 6:
        return ""
    if conf >= 5:
        return "⚠️ Not Tier 1 — strong setup but not the deepest-oversold / highest-conviction trigger."
    if conf >= 4:
        return "⚠️ Tier 3 — qualifying signal; fewer confirming conditions aligned."
    return "⚠️ Tier 4 — range/mean-reversion setup; lower expected edge, size accordingly."


def _signal_card_html(s: dict) -> str:
    conf   = s.get("confidence", 0) or 0
    tier   = _tier_html(conf)
    sig    = s.get("signal", "")
    cls    = "buy" if sig == "BUY" else "sell"
    price  = f"${s['price']:.2f}" if s.get("price") else "—"
    stop   = s.get("stop_loss", "—") or "—"
    target = s.get("target", "—") or "—"
    rat    = html.escape(s.get("rationale", "") or "")

    rat_block = f'<div class="rationale">{rat}</div>' if rat else ""
    # Confidence caveat for sub-Tier-1 BUYs
    caveat = _confidence_caveat(conf) if sig == "BUY" else ""
    if caveat:
        rat_block += (f'<div class="rationale" style="color:#f9c846;">'
                      f'{html.escape(caveat)}</div>')
    return f"""
<div class="card">
  <div class="card-top">
    <span class="ticker">{s['ticker']}</span>
    <span class="badge {cls}">{sig}</span>
    {tier}
  </div>
  <div class="meta">
    <b>Entry:</b> {price} &nbsp;|&nbsp; <b>Stop:</b> {html.escape(str(stop))} &nbsp;|&nbsp; <b>Target:</b> {html.escape(str(target))}
  </div>
  {rat_block}
</div>"""


def _open_position_card_html(r: dict) -> str:
    """Card for an open BUY position with live P&L."""
    conf    = r.get("confidence", 0) or 0
    tier    = _tier_html(conf)
    entry   = r.get("price", 0) or 0
    stop    = r.get("stop_loss", "—") or "—"
    target  = r.get("target", "—") or "—"
    date    = r.get("date", "")
    cur     = r.get("current_price")
    upnl    = r.get("unrealized_pnl")

    if cur and upnl is not None:
        pnl_color = "#00dc82" if upnl >= 0 else "#ff5050"
        pnl_block = (
            f'<span style="color:{pnl_color};font-weight:700;">{upnl:+.2f}%</span>'
            f' &nbsp;·&nbsp; <b>Now:</b> ${cur:.2f}'
        )
    else:
        pnl_block = '<span style="color:#5050aa;">—</span>'

    return f"""
<div class="card">
  <div class="card-top">
    <span class="ticker">{r['ticker']}</span>
    <span class="badge hold">OPEN</span>
    {tier}
    &nbsp; {pnl_block}
  </div>
  <div class="meta">
    <b>Entry:</b> ${entry:.2f} &nbsp;|&nbsp; <b>Stop:</b> {html.escape(str(stop))} &nbsp;|&nbsp; <b>Target:</b> {html.escape(str(target))}
    &nbsp;·&nbsp; opened {date}
  </div>
</div>"""


def _vix_badge(vix_level: float | None, vix_max: int | None) -> str:
    if vix_level is None:
        return ""
    if vix_max is not None and vix_level > vix_max:
        color = "#ff5050"
        label = f"⚠️ VIX {vix_level:.1f} — HIGH FEAR · BUY signals suppressed"
    else:
        color = "#00dc82"
        label = f"✅ VIX {vix_level:.1f} — Normal market conditions"
    return f'<p style="margin-top:6px;font-size:12px;color:{color};font-weight:600">{label}</p>'


def _build_summary_html(signals: list[dict], results: dict, source: str,
                        skipped: int, open_positions: list[dict],
                        perf: dict | None = None,
                        vix_level: float | None = None,
                        vix_max: int | None = None) -> str:
    buys  = [s for s in signals if s.get("signal") == "BUY"]
    sells = [s for s in signals if s.get("signal") == "SELL"]
    date  = datetime.now().strftime("%A, %B %d, %Y &nbsp;·&nbsp; %I:%M %p")

    total_scanned = (len(results.get("BUY", [])) + len(results.get("SELL", [])) +
                     len(results.get("NO TRADE", [])) + len(results.get("ERROR", [])))

    p = perf or {}
    win_rate  = p.get("win_rate", 0)
    avg_pnl   = p.get("avg_pnl", 0)
    total_pnl = p.get("total_pnl", 0)
    total_trades = p.get("total", 0)
    wr_color  = "#00dc82" if win_rate >= 50 else "#ff5050"
    avg_color = "#00dc82" if avg_pnl  >= 0  else "#ff5050"
    tp_color  = "#00dc82" if total_pnl >= 0  else "#ff5050"

    perf_row = ""
    if total_trades > 0:
        perf_row = f"""
<div class="stats" style="margin-top:-8px;">
  <div class="stat"><div class="num" style="color:{wr_color}">{win_rate:.0f}%</div><div class="lbl">Win Rate</div></div>
  <div class="stat"><div class="num" style="color:{avg_color}">{avg_pnl:+.1f}%</div><div class="lbl">Avg P&amp;L</div></div>
  <div class="stat"><div class="num" style="color:{tp_color}">{total_pnl:+.0f}%</div><div class="lbl">Total P&amp;L</div></div>
  <div class="stat"><div class="num">{total_trades}</div><div class="lbl">Closed Trades</div></div>
</div>"""

    # Stats bar
    stats = f"""
<div class="stats">
  <div class="stat"><div class="num">{total_scanned + skipped}</div><div class="lbl">Scanned</div></div>
  <div class="stat"><div class="num" style="color:#00dc82">{len(buys)}</div><div class="lbl">BUY</div></div>
  <div class="stat"><div class="num" style="color:#ff5050">{len(sells)}</div><div class="lbl">SELL</div></div>
  <div class="stat"><div class="num">{len(open_positions)}</div><div class="lbl">Open</div></div>
</div>
{perf_row}"""

    # New signals
    signal_section = ""
    if buys:
        cards = "".join(_signal_card_html(s) for s in buys)
        signal_section += f'<div class="section"><div class="sec-hdr">🚀 New BUY Signals</div>{cards}</div>'
    if sells:
        cards = "".join(_signal_card_html(s) for s in sells)
        signal_section += f'<div class="section"><div class="sec-hdr">🔻 New SELL Signals</div>{cards}</div>'
    if not buys and not sells:
        signal_section = '<div class="notrade">No actionable signals found today — market conditions did not meet entry criteria.</div>'

    # Open positions
    open_section = ""
    if open_positions:
        cards = "".join(_open_position_card_html(r) for r in open_positions)
        open_section = f'<div class="section" style="margin-top:20px"><div class="sec-hdr">📂 Open Positions ({len(open_positions)})</div>{cards}</div>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{_CSS}</style></head>
<body>
<div class="header">
  <h1>⭐ SWINGTRADER DAILY REPORT</h1>
  <p>{date}</p>
  <p style="margin-top:4px;font-size:12px;color:#5858a0">{source}</p>
  {_vix_badge(vix_level, vix_max)}
</div>
{stats}
{signal_section}
{open_section}
<div class="footer">SwingTrader · Automated daily scan</div>
</body>
</html>"""


# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(signals: list[dict]) -> bool:
    """Kept for backwards compat — delegates to send_daily_summary."""
    return send_daily_summary(signals, {}, "Watchlist", 0, [])


def send_daily_summary(signals: list[dict], results: dict, source: str,
                       skipped: int, open_positions: list[dict],
                       perf: dict | None = None,
                       vix_level: float | None = None,
                       vix_max: int | None = None) -> bool:
    """Send the full daily HTML summary email. Always sends even with no signals."""
    if not EMAIL_ENABLED:
        return False

    buys  = sum(1 for s in signals if s.get("signal") == "BUY")
    sells = sum(1 for s in signals if s.get("signal") == "SELL")

    if buys or sells:
        subject = f"⭐ SwingTrader {datetime.now().strftime('%b %d')}: {buys} BUY, {sells} SELL"
    else:
        subject = f"📊 SwingTrader Daily Report — {datetime.now().strftime('%b %d, %Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO

    # Plain-text fallback
    txt_lines = [
        f"SwingTrader Daily Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Universe: {source}  |  Skipped: {skipped}",
        "",
    ]
    if signals:
        for s in signals:
            conf = s.get("confidence", 0) or 0
            tier = _tier_label(conf)
            txt_lines.append(f"{s['signal']:4}  {s['ticker']:<6}  {tier}  ${s.get('price', 0):.2f}")
            if s.get("rationale"):
                txt_lines.append(f"      {s['rationale']}")
            caveat = _confidence_caveat(conf) if s.get("signal") == "BUY" else ""
            if caveat:
                txt_lines.append(f"      {caveat}")
    else:
        txt_lines.append("No actionable signals found today.")

    if open_positions:
        txt_lines += ["", f"Open Positions ({len(open_positions)}):"]
        for r in open_positions:
            txt_lines.append(f"  {r['ticker']:<6}  entry ${r.get('price', 0):.2f}  since {r.get('date', '')}")

    msg.attach(MIMEText("\n".join(txt_lines), "plain"))
    msg.attach(MIMEText(_build_summary_html(signals, results, source, skipped, open_positions, perf, vix_level, vix_max), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"  ✉  Daily summary email sent → {EMAIL_TO}")
        return True
    except Exception as e:
        print(f"  ✉  Email FAILED: {e}")
        return False


# ── ntfy.sh push notification ─────────────────────────────────────────────────

def send_push(signals: list[dict]) -> bool:
    """Send a push notification via ntfy.sh. Returns True on success."""
    if not NTFY_ENABLED:
        return False
    if not signals:
        return False

    buys  = [s for s in signals if s.get("signal") == "BUY"]
    sells = [s for s in signals if s.get("signal") == "SELL"]

    title = f"SwingTrader: {len(buys)} BUY, {len(sells)} SELL"

    lines = []
    for s in buys:
        conf = s.get("confidence", 0) or 0
        lines.append(f"🚀 {s['ticker']}  {_tier_label(conf)}  ${s.get('price', 0):.2f}")
    for s in sells:
        conf = s.get("confidence", 0) or 0
        lines.append(f"🔻 {s['ticker']}  {_tier_label(conf)}  ${s.get('price', 0):.2f}")

    body = "\n".join(lines)

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": "high",
                "Tags":     "chart_with_upwards_trend",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        print(f"  📲  Push sent → ntfy.sh/{NTFY_TOPIC}")
        return True
    except Exception as e:
        print(f"  📲  Push FAILED: {e}")
        return False


# ── Telegram ──────────────────────────────────────────────────────────────────

def _telegram_chat_ids() -> list[str]:
    """Return all configured Telegram chat IDs (personal + group if set)."""
    ids = [str(TELEGRAM_CHAT_ID)] if TELEGRAM_CHAT_ID else []
    if TELEGRAM_GROUP_CHAT_ID and str(TELEGRAM_GROUP_CHAT_ID) not in ids:
        ids.append(str(TELEGRAM_GROUP_CHAT_ID))
    return ids


def send_to_chat(chat_id, text: str, reply_to_message_id: int | None = None) -> bool:
    """Send a single HTML message to one Telegram chat. Returns True on success.

    Used both by the outbound alert helpers and by the interactive nexus_bot
    to reply to whichever chat messaged it.
    """
    body = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    if reply_to_message_id is not None:
        body["reply_to_message_id"] = reply_to_message_id
    payload = json.dumps(body).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"  ✈️  Telegram FAILED (chat {chat_id}): {e}")
        return False


def _telegram_send_text(text: str) -> bool:
    """Send a text message to all configured Telegram chat IDs."""
    success = False
    for chat_id in _telegram_chat_ids():
        if send_to_chat(chat_id, text):
            success = True
    return success

def _build_telegram_message(signals: list[dict]) -> str:
    """Build an HTML-formatted Telegram message."""
    buys  = [s for s in signals if s.get("signal") == "BUY"]
    sells = [s for s in signals if s.get("signal") == "SELL"]

    date  = datetime.now().strftime("%b %d, %Y · %I:%M %p")
    lines = [
        f"⭐ <b>SWINGTRADER SIGNALS</b>",
        f"<i>{date}</i>",
        "",
    ]

    def _esc(v):
        return html.escape(str(v)) if v else ""

    if buys:
        lines.append(f"🚀 <b>BUY Signals ({len(buys)})</b>")
        for s in buys:
            conf  = s.get("confidence", 0) or 0
            price = f"${s.get('price', 0):.2f}"
            lines.append(f"  <b>{_esc(s['ticker'])}</b>  {_tier_label(conf)}  {price}")
            lines.append(f"  Entry: {price}  |  Stop: {_esc(s.get('stop_loss', '—'))}  |  Target: {_esc(s.get('target', '—'))}")
            if s.get("rationale"):
                lines.append(f"  <i>{_esc(s['rationale'])}</i>")
            caveat = _confidence_caveat(conf)
            if caveat:
                lines.append(f"  {_esc(caveat)}")
            lines.append("")

    if sells:
        lines.append(f"🔻 <b>SELL Signals ({len(sells)})</b>")
        for s in sells:
            conf  = s.get("confidence", 0) or 0
            price = f"${s.get('price', 0):.2f}"
            lines.append(f"  <b>{_esc(s['ticker'])}</b>  {_tier_label(conf)}  {price}")
            if s.get("rationale"):
                lines.append(f"  <i>{_esc(s['rationale'])}</i>")
            lines.append("")

    return "\n".join(lines).strip()


def send_telegram(signals: list[dict]) -> bool:
    """Send a Telegram message via Bot API. Returns True on success."""
    if not TELEGRAM_ENABLED:
        return False
    if not signals:
        return False

    text = _build_telegram_message(signals)
    ok   = _telegram_send_text(text)
    if ok:
        print(f"  ✈️  Telegram sent → {_telegram_chat_ids()}")
    return ok


# ── End-of-day Telegram summary (4★/5★ only) ─────────────────────────────────

def send_daily_telegram(signals: list[dict]) -> bool:
    """Send an end-of-day Telegram summary for 3★+ signals.

    Always sends — if no qualifying signals, explicitly states so.
    """
    if not TELEGRAM_ENABLED:
        return False

    high_conf = [s for s in signals if (s.get("confidence") or 0) >= 3]
    buys  = [s for s in high_conf if s.get("signal") == "BUY"]
    sells = [s for s in high_conf if s.get("signal") == "SELL"]

    def _esc(v):
        return html.escape(str(v)) if v else ""

    date  = datetime.now().strftime("%b %d, %Y · %I:%M %p")
    lines = [
        "⭐ <b>SWINGTRADER DAILY SUMMARY</b>",
        f"<i>{date}</i>",
        "<i>Tier 1–4 signals</i>",
        "",
    ]

    if not buys and not sells:
        lines.append("📭 No Tier 1–4 signals today.")
    else:
        if buys:
            lines.append(f"🚀 <b>BUY ({len(buys)})</b>")
            for s in buys:
                conf  = s.get("confidence", 0) or 0
                price = f"${s.get('price', 0):.2f}"
                lines.append(f"  <b>{_esc(s['ticker'])}</b>  {_tier_label(conf)}  {price}")
                lines.append(f"  Stop: {_esc(s.get('stop_loss', '—'))}  |  Target: {_esc(s.get('target', '—'))}")
                if s.get("rationale"):
                    lines.append(f"  <i>{_esc(s['rationale'])}</i>")
                caveat = _confidence_caveat(conf)
                if caveat:
                    lines.append(f"  {_esc(caveat)}")
                lines.append("")

        if sells:
            lines.append(f"🔻 <b>SELL ({len(sells)})</b>")
            for s in sells:
                conf  = s.get("confidence", 0) or 0
                price = f"${s.get('price', 0):.2f}"
                lines.append(f"  <b>{_esc(s['ticker'])}</b>  {_tier_label(conf)}  {price}")
                if s.get("rationale"):
                    lines.append(f"  <i>{_esc(s['rationale'])}</i>")
                lines.append("")

    text = "\n".join(lines).strip()
    ok   = _telegram_send_text(text)
    if ok:
        print(f"  ✈️  Daily Telegram summary sent → {_telegram_chat_ids()}")
    return ok


# ── Stop/target exit alerts ───────────────────────────────────────────────────

def send_exit_alert(hits: list[dict]) -> bool:
    """Send a Telegram alert for positions that hit their stop loss or target."""
    if not TELEGRAM_ENABLED or not hits:
        return False

    def _esc(v):
        return html.escape(str(v)) if v else ""

    date  = datetime.now().strftime("%b %d, %Y · %I:%M %p")
    lines = [f"🔔 <b>POSITION CLOSED</b>", f"<i>{date}</i>", ""]

    for h in hits:
        if h["hit_type"] == "TARGET":
            icon, label = "🎯", "Target hit"
        elif h["hit_type"] == "IBS_EXIT":
            icon, label = "📈", "IBS exit — mean-reversion exhausted"
        elif h["hit_type"] == "RSI_EXIT":
            icon, label = "📊", "RSI momentum exit"
        elif h["hit_type"] == "TRAIL_STOP":
            icon, label = "📉", "Trailing stop hit"
        elif h["hit_type"] == "MANUAL":
            icon, label = "🤚", "Manually closed (model still tracking)"
        else:
            icon, label = "🛑", "Stop hit"
        pnl      = h.get("pnl", 0)
        pnl_sign = "+" if pnl >= 0 else ""
        cur      = h.get("current_price", 0) or 0
        entry    = h.get("price", 0) or 0
        lines.append(f"{icon} <b>{_esc(h['ticker'])}</b>  —  {label}")
        lines.append(f"  Entry: ${entry:.2f}  →  Exit: ${cur:.2f}  (<b>{pnl_sign}{pnl:.2f}%</b>)")
        lines.append("")

    text = "\n".join(lines).strip()
    ok   = _telegram_send_text(text)
    if ok:
        print(f"  🔔  Exit alert sent → {[h['ticker'] for h in hits]}")
    return ok


# ── Combined convenience function ─────────────────────────────────────────────

def notify(signals: list[dict]) -> None:
    """Send all enabled notifications. Call with list of signal dicts."""
    if not signals:
        return
    send_email(signals)
    send_push(signals)
    send_telegram(signals)
