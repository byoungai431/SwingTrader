"""
nexus_bot.py — Interactive Telegram assistant for Nexus Edge (SwingTrader).

Long-polls the Telegram Bot API and answers questions about the live portfolio,
signals, and performance using Claude. Also comments on an external daily market
review when a human replies to / forwards it with a wake word.

The whole authorized group can talk to Nexus. To keep it from answering every line
of group chatter (and burning API tokens), in a group Nexus only responds to:
  • a slash command  (/status, /positions, /signals, /performance, /help, /review)
  • an @mention of the bot, or a reply to one of the bot's own messages
  • a message starting with the wake word  ("nexus ...")
Direct 1:1 messages are always treated as queries.

NOTE: for free-form (non-command) group messages to reach the bot at all, group
privacy mode must be DISABLED in BotFather (/setprivacy → Disable) for this bot.

Run:        python3 nexus_bot.py
Scheduled:  ~/Library/LaunchAgents/com.swingtrader.nexusbot.plist
"""

import html
import json
import pathlib
import time
import urllib.parse
import urllib.request
from datetime import datetime

import anthropic
import psycopg2.extras

from config import ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ENABLED
from notify import send_to_chat, _telegram_chat_ids, _tier_label
from history import get_conn, get_performance_stats

# ── Config ────────────────────────────────────────────────────────────────────
NEXUS_MODEL   = "claude-sonnet-5"   # bump to "claude-opus-4-8" for deeper reasoning
WAKE_WORD     = "nexus"
POLL_TIMEOUT  = 30                  # long-poll seconds
MAX_TG_LEN    = 3900                # Telegram hard limit is 4096; leave headroom
OFFSET_FILE   = pathlib.Path(__file__).parent / ".nexus_bot_offset"
API_BASE      = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

AUTHORIZED    = set(_telegram_chat_ids())
_client       = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# Populated at startup via getMe
BOT_ID: int | None       = None
BOT_USERNAME: str | None = None


# ── Telegram API helpers ──────────────────────────────────────────────────────
def _api_get(method: str, params: dict | None = None) -> dict:
    """Call a Telegram Bot API method via GET. Returns parsed JSON (or {} on error)."""
    url = f"{API_BASE}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=POLL_TIMEOUT + 15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Telegram API {method} failed: {e}")
        return {}


def _get_me() -> None:
    global BOT_ID, BOT_USERNAME
    data = _api_get("getMe")
    if data.get("ok"):
        BOT_ID       = data["result"].get("id")
        BOT_USERNAME = data["result"].get("username")
        print(f"  Nexus bot online as @{BOT_USERNAME} (id {BOT_ID})")


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        print(f"  Could not persist offset: {e}")


def _reply(chat_id, text: str, reply_to: int | None = None) -> None:
    """Send a (possibly long) reply, chunked to Telegram's size limit."""
    if not text:
        return
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > MAX_TG_LEN:
            chunks.append(buf)
            buf = ""
        buf += (line + "\n")
    if buf:
        chunks.append(buf)
    for i, chunk in enumerate(chunks):
        send_to_chat(chat_id, chunk, reply_to_message_id=reply_to if i == 0 else None)


# ── Nexus data context (reuses existing helpers — no reimplemented queries) ────
def _open_positions() -> list[dict]:
    """Live open BUY positions enriched with current price / uPnL (from run_daily)."""
    try:
        from run_daily import _get_open_positions
        return _get_open_positions()
    except Exception as e:
        print(f"  open positions lookup failed: {e}")
        return []


def _recent_signals(limit: int = 15) -> list[dict]:
    """Most recent BUY/SELL signals from the DB."""
    try:
        conn = get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT ticker, date, signal, confidence, price, rationale
                FROM signals
                WHERE signal IN ('BUY', 'SELL')
                ORDER BY id DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"  recent signals lookup failed: {e}")
        return []


def _tier_name(conf) -> str:
    """Plain 'Tier N' string (no emoji) for AI context / compact rendering."""
    conf = int(conf or 0)
    if conf >= 6: return "Tier 1"
    if conf >= 5: return "Tier 2"
    if conf >= 4: return "Tier 3"
    if conf >= 3: return "Tier 4"
    return "—"


def _nexus_context() -> str:
    """Compact text snapshot of Nexus's live state for the Claude prompt."""
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
    pos  = _open_positions()
    perf = get_performance_stats()
    sigs = _recent_signals(15)

    lines = [f"== NEXUS LIVE STATE (as of {ts}) =="]

    lines.append(f"\nOPEN POSITIONS ({len(pos)}):")
    if pos:
        for p in pos:
            cur  = p.get("current_price")
            upnl = p.get("unrealized_pnl")
            cur_s  = f"${cur:.2f}" if cur else "n/a"
            upnl_s = f"{upnl:+.2f}%" if upnl is not None else "n/a"
            rat = (p.get("rationale") or "")[:200]
            lines.append(
                f"- {p['ticker']} | {_tier_name(p.get('confidence'))} | "
                f"entry ${p.get('price', 0):.2f} → now {cur_s} ({upnl_s}) | "
                f"since {p.get('date', '')} | {rat}"
            )
    else:
        lines.append("- none")

    lines.append(
        f"\nCLOSED-TRADE PERFORMANCE: {perf.get('total', 0)} trades, "
        f"win rate {perf.get('win_rate', 0):.0f}%, avg {perf.get('avg_pnl', 0):+.1f}%, "
        f"total {perf.get('total_pnl', 0):+.0f}%"
    )

    lines.append(f"\nRECENT SIGNALS ({len(sigs)}):")
    if sigs:
        for s in sigs:
            rat = (s.get("rationale") or "")[:160]
            lines.append(
                f"- {s.get('date', '')} {s['ticker']} {s['signal']} "
                f"{_tier_name(s.get('confidence'))} ${s.get('price') or 0:.2f} — {rat}"
            )
    else:
        lines.append("- none")

    lines.append(
        "\nTIER SCALE: Tier 1 = highest conviction, Tier 4 = lowest. "
        "Higher tier = stronger/more-confirmed setup."
    )
    return "\n".join(lines)


# ── Claude ────────────────────────────────────────────────────────────────────
def _ask_claude(system: str, user_text: str) -> str:
    if _client is None:
        return "⚠️ Nexus AI isn't configured (missing ANTHROPIC_API_KEY)."
    try:
        resp = _client.messages.create(
            model=NEXUS_MODEL,
            max_tokens=1200,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts).strip() or "(no response)"
    except Exception as e:
        print(f"  Claude error: {e}")
        return "⚠️ Nexus is unavailable right now — try again shortly."


_QA_SYSTEM = (
    "You are Nexus, the assistant for the Nexus Edge swing-trading system. "
    "Answer the user's question using ONLY the provided live portfolio/signal/performance "
    "data — never invent positions, prices, or trades. Explain quality as Tier 1–4 "
    "(Tier 1 = highest conviction). Be concise and direct; this is a Telegram chat. "
    "Do not use Markdown formatting (no **bold** or backticks) — plain text only."
)

_REVIEW_SYSTEM = (
    "You are Nexus, the assistant for the Nexus Edge swing-trading system. You are given an "
    "external daily market review plus Nexus's own live signals and positions. Respond with a "
    "concise, structured take in three labeled sections:\n"
    "AGREE: points in the review that match Nexus's own data/signals.\n"
    "DISAGREE / CAUTION: points that conflict with Nexus's signals or that you'd flag.\n"
    "YOUR TICKERS IN PLAY: any tickers the review names that Nexus holds or recently signaled, "
    "with their tier and current status.\n"
    "Ground every claim in the provided Nexus data — never invent positions or trades. If the "
    "review names nothing Nexus tracks, say so. Plain text only (no Markdown). Keep it Telegram-length."
)


def _answer_question(question: str) -> str:
    ctx = _nexus_context()
    return _ask_claude(_QA_SYSTEM, f"{ctx}\n\n== USER QUESTION ==\n{question}")


def _answer_review(review_text: str, user_note: str) -> str:
    ctx = _nexus_context()
    note = f"\n\n== USER NOTE ==\n{user_note}" if user_note.strip() else ""
    return _ask_claude(
        _REVIEW_SYSTEM,
        f"{ctx}\n\n== EXTERNAL MARKET REVIEW ==\n{review_text}{note}",
    )


# ── Slash-command formatters ──────────────────────────────────────────────────
def _fmt_positions() -> str:
    pos = _open_positions()
    if not pos:
        return "📭 No open positions right now."
    out = [f"📂 <b>Open Positions ({len(pos)})</b>", ""]
    for p in pos:
        cur  = p.get("current_price")
        upnl = p.get("unrealized_pnl")
        cur_s  = f"${cur:.2f}" if cur else "—"
        upnl_s = f"{upnl:+.2f}%" if upnl is not None else "—"
        out.append(f"<b>{html.escape(p['ticker'])}</b>  {_tier_label(p.get('confidence'))}")
        out.append(f"  Entry ${p.get('price', 0):.2f} → Now {cur_s}  ({upnl_s})  · since {p.get('date', '')}")
    return "\n".join(out)


def _fmt_performance() -> str:
    p = get_performance_stats()
    if not p.get("total"):
        return "📊 No closed trades on record yet."
    return (
        "📊 <b>Performance (closed trades)</b>\n"
        f"  Win rate: <b>{p['win_rate']:.0f}%</b>  ({p['wins']}W / {p['losses']}L)\n"
        f"  Avg P&amp;L: <b>{p['avg_pnl']:+.1f}%</b>\n"
        f"  Total P&amp;L: <b>{p['total_pnl']:+.0f}%</b>  over {p['total']} trades"
    )


def _fmt_signals() -> str:
    sigs = _recent_signals(12)
    if not sigs:
        return "📭 No recent signals on record."
    out = ["🛰️ <b>Recent Signals</b>", ""]
    for s in sigs:
        icon = "🚀" if s["signal"] == "BUY" else "🔻"
        out.append(
            f"{icon} <b>{html.escape(s['ticker'])}</b>  {_tier_label(s.get('confidence'))}  "
            f"${s.get('price') or 0:.2f}  <i>{s.get('date', '')}</i>"
        )
    return "\n".join(out)


def _fmt_status() -> str:
    pos = _open_positions()
    p   = get_performance_stats()
    open_upnl = [x["unrealized_pnl"] for x in pos if x.get("unrealized_pnl") is not None]
    avg_open  = sum(open_upnl) / len(open_upnl) if open_upnl else 0
    return (
        "🟢 <b>Nexus Status</b>\n"
        f"  Open positions: <b>{len(pos)}</b>  (avg uPnL {avg_open:+.1f}%)\n"
        f"  Closed trades: <b>{p.get('total', 0)}</b>  ·  win rate {p.get('win_rate', 0):.0f}%\n"
        "  Ask me anything, e.g. \"nexus why is AAPL only tier 2?\""
    )


_HELP = (
    "🤖 <b>Nexus — commands</b>\n"
    "/status — quick overview\n"
    "/positions — open positions & live P&amp;L\n"
    "/signals — recent BUY/SELL signals\n"
    "/performance — closed-trade stats\n"
    "/review — (reply to a market review) get my take\n"
    "/help — this message\n\n"
    "Or just talk to me: start with <b>nexus</b> (or @mention me) and ask anything about the portfolio, "
    "e.g. <i>\"nexus how are my positions doing?\"</i>  In a group, reply to your friend's daily market "
    "review with <i>\"nexus thoughts?\"</i> and I'll weigh in."
)


# ── Message routing ───────────────────────────────────────────────────────────
def _command(text: str) -> str | None:
    """Return the bare command word (no slash, no @botname) or None."""
    t = text.strip()
    if not t.startswith("/"):
        return None
    return t.split()[0][1:].split("@")[0].lower()


def _strip_wake(text: str) -> str:
    """Remove a leading wake word / @mention / command token from the message."""
    t = text.strip()
    if t.startswith("/"):
        t = t.split(None, 1)[1] if len(t.split(None, 1)) > 1 else ""
    low = t.lower()
    if low.startswith(WAKE_WORD):
        t = t[len(WAKE_WORD):].lstrip(" ,:—-")
    if BOT_USERNAME:
        t = t.replace(f"@{BOT_USERNAME}", "").strip()
    return t.strip()


# Words that mark a message as an actual question / request (vs. a statement).
_QUESTION_STARTERS = {
    "who", "what", "whats", "when", "where", "why", "how", "hows", "which",
    "whose", "whom",
    "is", "are", "am", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "shall", "has", "have", "had", "may", "might",
    "tell", "give", "show", "list", "explain", "summarize", "summarise",
    "describe", "compare", "rate", "analyze", "analyse", "check", "find",
    "recommend", "suggest", "thoughts", "opinion",
}


def _looks_like_question(text: str) -> bool:
    """True if the message reads as a question/request rather than a statement."""
    t = text.strip()
    if not t:
        return False
    if "?" in t:
        return True
    first = t.lower().split()[0].strip(",.!:;'\"")
    # handle contractions like "what's" / "how's"
    first = first.split("'")[0]
    return first in _QUESTION_STARTERS


def _passes_trigger(text: str, msg: dict, is_private: bool) -> bool:
    """Should Nexus respond to this message? Private = always; group = gated."""
    if is_private:
        return True
    t = text.strip().lower()
    if t.startswith("/"):
        return True
    if t.startswith(WAKE_WORD):
        return True
    if BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in t:
        return True
    reply = msg.get("reply_to_message")
    if reply and reply.get("from", {}).get("id") == BOT_ID:
        return True
    return False


def _is_forward(msg: dict) -> bool:
    return any(k in msg for k in (
        "forward_origin", "forward_from", "forward_from_chat",
        "forward_date", "forward_sender_name",
    ))


def handle_update(update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat    = msg.get("chat", {})
    chat_id = chat.get("id")
    if str(chat_id) not in AUTHORIZED:
        return  # not our chat — ignore silently

    text       = msg.get("text") or msg.get("caption") or ""
    is_private = chat.get("type") == "private"
    msg_id     = msg.get("message_id")
    reply      = msg.get("reply_to_message")

    if not text:
        return
    if not _passes_trigger(text, msg, is_private):
        return

    cmd = _command(text)

    # /review, or replying to someone else's message asking for a take → review path
    replied_to_other = bool(reply and reply.get("from", {}).get("id") != BOT_ID)
    review_src = None
    if cmd == "review" and reply:
        review_src = reply.get("text") or reply.get("caption")
    elif replied_to_other and cmd is None:
        review_src = reply.get("text") or reply.get("caption")
    elif _is_forward(msg) and cmd is None and _strip_wake(text):
        # forwarded review pasted with a wake word → treat the body as the review
        review_src = _strip_wake(text)

    if review_src:
        answer = _answer_review(review_src, _strip_wake(text))
        _reply(chat_id, html.escape(answer), reply_to=msg_id)
        return

    # Slash commands (fast paths, no API call)
    if cmd:
        handlers = {
            "status":      _fmt_status,
            "positions":   _fmt_positions,
            "signals":     _fmt_signals,
            "performance": _fmt_performance,
            "help":        lambda: _HELP,
            "start":       lambda: _HELP,
        }
        fn = handlers.get(cmd)
        if fn:
            _reply(chat_id, fn(), reply_to=msg_id)
        else:
            _reply(chat_id, "Unknown command. Try /help.", reply_to=msg_id)
        return

    # Free-form path → only respond if it's actually a question/request, not a statement
    question = _strip_wake(text) or text
    if not _looks_like_question(question):
        return
    answer = _answer_question(question)
    _reply(chat_id, html.escape(answer), reply_to=msg_id)


# ── Main long-poll loop ───────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN:
        print("Telegram is disabled or bot token missing — nexus_bot exiting.")
        return
    if not AUTHORIZED:
        print("No authorized chat IDs configured — nexus_bot exiting.")
        return

    _get_me()
    print(f"  Authorized chats: {sorted(AUTHORIZED)}")
    offset = _load_offset()

    while True:
        data = _api_get("getUpdates", {
            "offset":          offset,
            "timeout":         POLL_TIMEOUT,
            "allowed_updates": json.dumps(["message", "edited_message"]),
        })
        if not data.get("ok"):
            time.sleep(3)
            continue
        for update in data.get("result", []):
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as e:
                print(f"  handle_update error: {e}")
            _save_offset(offset)


if __name__ == "__main__":
    main()
