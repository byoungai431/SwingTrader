import os
import pathlib
import psycopg2
import psycopg2.extras
from datetime import datetime


def _load_toml_secret(key: str) -> str:
    """Read a single key from .streamlit/secrets.toml for headless runs."""
    import tomllib
    toml_path = pathlib.Path(__file__).parent / ".streamlit" / "secrets.toml"
    try:
        with open(toml_path, "rb") as f:
            return tomllib.load(f).get(key, "")
    except Exception:
        return ""


def get_conn():
    """Return a psycopg2 connection. Reads DATABASE_URL from Streamlit secrets or env."""
    try:
        import streamlit as st
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = os.environ.get("DATABASE_URL", "") or _load_toml_secret("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured in secrets or environment.")
    return psycopg2.connect(url)


def init_db():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id             SERIAL PRIMARY KEY,
                    ticker         TEXT    NOT NULL,
                    date           TEXT    NOT NULL,
                    signal         TEXT    NOT NULL,
                    confidence     INTEGER,
                    rationale      TEXT,
                    entry_zone     TEXT,
                    stop_loss      TEXT,
                    target         TEXT,
                    price          REAL,
                    created_at     TEXT    DEFAULT NOW()::text,
                    exit_price     REAL,
                    exit_date      TEXT,
                    dismissed_at   TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id             SERIAL PRIMARY KEY,
                    signal_id      INTEGER,
                    ticker         TEXT    NOT NULL,
                    entry_date     TEXT    NOT NULL,
                    entry_price    REAL    NOT NULL,
                    confidence     INTEGER,
                    stop_loss      TEXT,
                    target         TEXT,
                    notes          TEXT,
                    exit_price     REAL,
                    exit_date      TEXT,
                    exit_reason    TEXT,
                    created_at     TEXT    DEFAULT NOW()::text
                )
            """)
            # Per-user columns / tables (safe to run on every startup)
            cur.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS user_id TEXT")
            cur.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS position_amount REAL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id          TEXT PRIMARY KEY,
                    starting_balance REAL    DEFAULT 10000,
                    updated_at       TEXT    DEFAULT NOW()::text
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS signal_dismissals (
                    signal_id    INTEGER NOT NULL,
                    user_id      TEXT    NOT NULL,
                    dismissed_at TEXT    NOT NULL,
                    PRIMARY KEY (signal_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watchlists (
                    user_id TEXT NOT NULL,
                    ticker  TEXT NOT NULL,
                    PRIMARY KEY (user_id, ticker)
                )
            """)
        conn.commit()
    finally:
        conn.close()


def save_signal(ticker: str, sig: dict, price: float):
    price = float(price)  # guard against np.float64 (NumPy 2.0 breaks psycopg2 adaptation)
    init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Skip if the same signal type already logged for this ticker today
            cur.execute("""
                SELECT id FROM signals
                WHERE ticker = %s AND signal = %s AND date = %s
                LIMIT 1
            """, (ticker, sig.get("signal"), today))
            if cur.fetchone():
                return
            # Auto-close any open BUY from a prior date when a SELL is confirmed
            if sig.get("signal") == "SELL":
                cur.execute("""
                    UPDATE signals
                    SET exit_price = %s, exit_date = %s
                    WHERE ticker = %s AND signal = 'BUY' AND exit_price IS NULL
                      AND date < %s
                """, (price, today, ticker, today))
            cur.execute("""
                INSERT INTO signals
                    (ticker, date, signal, confidence, rationale, entry_zone, stop_loss, target, price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                ticker,
                today,
                sig.get("signal"),
                sig.get("confidence_stars"),
                sig.get("rationale"),
                sig.get("entry_zone"),
                sig.get("stop_loss"),
                sig.get("target"),
                price,
            ))
        conn.commit()
    finally:
        conn.close()


def log_exit(signal_id: int, exit_price: float):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signals
                SET exit_price = %s, exit_date = %s
                WHERE id = %s
            """, (exit_price, today, signal_id))
        conn.commit()
    finally:
        conn.close()


def get_performance_stats() -> dict:
    """Return overall win/loss stats for all closed BUY signals."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT price, exit_price
                FROM signals
                WHERE signal = 'BUY' AND exit_price IS NOT NULL AND price IS NOT NULL
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    wins = losses = 0
    total_pnl = 0.0
    for r in rows:
        pnl = (r["exit_price"] - r["price"]) / r["price"] * 100
        total_pnl += pnl
        if pnl >= 0:
            wins += 1
        else:
            losses += 1
    total = wins + losses
    return {
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate": wins / total * 100 if total else 0,
        "avg_pnl": total_pnl / total if total else 0,
        "total_pnl": total_pnl,
    }


def get_history(ticker: str, limit: int = 8) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, signal, confidence, rationale,
                       entry_zone, stop_loss, target, price,
                       exit_price, exit_date
                FROM signals
                WHERE ticker = %s
                ORDER BY id DESC
                LIMIT %s
            """, (ticker, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def dismiss_signal(signal_id: int, user_id: str):
    """Hide a signal from the Recommended list for this user only."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signal_dismissals (signal_id, user_id, dismissed_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (signal_id, user_id) DO NOTHING
            """, (signal_id, user_id, now))
        conn.commit()
    finally:
        conn.close()


# ── My Positions ───────────────────────────────────────────────────────────────

def get_user_settings(user_id: str) -> dict:
    """Return the user's settings dict. Creates a default row if none exists."""
    init_db()
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM user_settings WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row:
        return dict(row)
    return {"user_id": user_id, "starting_balance": 10_000.0}


def save_user_settings(user_id: str, starting_balance: float):
    init_db()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_settings (user_id, starting_balance, updated_at)
                VALUES (%s, %s, NOW()::text)
                ON CONFLICT (user_id) DO UPDATE
                    SET starting_balance = EXCLUDED.starting_balance,
                        updated_at = EXCLUDED.updated_at
            """, (user_id, starting_balance))
        conn.commit()
    finally:
        conn.close()


def enter_position(signal_id: int | None, ticker: str, entry_price: float,
                   confidence: int, stop_loss: str | None, target: str | None,
                   user_id: str = "", notes: str | None = None,
                   position_amount: float | None = None) -> int:
    """Record a trade the user manually entered. Returns the new position id."""
    init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO positions
                    (signal_id, ticker, entry_date, entry_price, confidence,
                     stop_loss, target, notes, user_id, position_amount)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (signal_id, ticker, today, entry_price, confidence,
                  stop_loss, target, notes, user_id, position_amount))
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_my_open_positions(user_id: str = "") -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, signal_id, ticker, entry_date, entry_price,
                       confidence, stop_loss, target, notes, position_amount
                FROM positions
                WHERE exit_date IS NULL AND user_id = %s
                ORDER BY entry_date DESC
            """, (user_id,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_my_position_history(user_id: str = "", limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, ticker, entry_date, entry_price, confidence,
                       stop_loss, target, exit_price, exit_date, exit_reason,
                       notes, position_amount
                FROM positions
                WHERE exit_date IS NOT NULL AND user_id = %s
                ORDER BY exit_date DESC
                LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def close_my_position(position_id: int, exit_price: float, user_id: str = "", exit_reason: str = "Manual") -> dict | None:
    """Close a user portfolio position (positions table only).

    Does NOT touch the signals table — the model continues tracking the
    original BUY signal independently so model stats stay clean.
    Returns a hit dict suitable for send_exit_alert, or None if not found.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT signal_id, ticker, entry_price, confidence, stop_loss, target
                FROM positions WHERE id = %s AND user_id = %s
            """, (position_id, user_id))
            pos = cur.fetchone()
            if not pos:
                return None
            pos = dict(pos)

            cur.execute("""
                UPDATE positions
                SET exit_price = %s, exit_date = %s, exit_reason = %s
                WHERE id = %s AND user_id = %s
            """, (exit_price, today, "Manually closed", position_id, user_id))
        conn.commit()
    finally:
        conn.close()

    entry = pos.get("entry_price") or 0
    pnl   = (exit_price - entry) / entry * 100 if entry else 0
    return {
        "ticker":        pos["ticker"],
        "price":         entry,
        "current_price": exit_price,
        "hit_type":      "MANUAL",
        "pnl":           pnl,
    }


def get_watchlist(user_id: str) -> list[str]:
    """Return the tickers on this user's watchlist, in insertion order."""
    init_db()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ticker FROM watchlists
                WHERE user_id = %s
                ORDER BY ctid
            """, (user_id,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def save_watchlist(user_id: str, tickers: list[str]):
    """Replace the user's watchlist with the given ticker list."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlists WHERE user_id = %s", (user_id,))
            if tickers:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO watchlists (user_id, ticker) VALUES %s ON CONFLICT DO NOTHING",
                    [(user_id, t) for t in tickers],
                )
        conn.commit()
    finally:
        conn.close()
