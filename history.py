import os
import psycopg2
import psycopg2.extras
from datetime import datetime


def get_conn():
    """Return a psycopg2 connection. Reads DATABASE_URL from Streamlit secrets or env."""
    try:
        import streamlit as st
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = os.environ.get("DATABASE_URL", "")
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
        conn.commit()
    finally:
        conn.close()


def save_signal(ticker: str, sig: dict, price: float):
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
            # Auto-close any open BUY from a prior date at the current price
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


def dismiss_signal(signal_id: int):
    """Mark a signal as viewed/dismissed so it hides from the Recommended list."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signals SET dismissed_at = %s
                WHERE id = %s
            """, (now, signal_id))
        conn.commit()
    finally:
        conn.close()


# ── My Positions ───────────────────────────────────────────────────────────────

def enter_position(signal_id: int | None, ticker: str, entry_price: float,
                   confidence: int, stop_loss: str | None, target: str | None,
                   notes: str | None = None) -> int:
    """Record a trade the user manually entered. Returns the new position id."""
    init_db()
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO positions
                    (signal_id, ticker, entry_date, entry_price, confidence,
                     stop_loss, target, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (signal_id, ticker, today, entry_price, confidence,
                  stop_loss, target, notes))
            new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def get_my_open_positions() -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, signal_id, ticker, entry_date, entry_price,
                       confidence, stop_loss, target, notes
                FROM positions
                WHERE exit_date IS NULL
                ORDER BY entry_date DESC
            """)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_my_position_history(limit: int = 50) -> list[dict]:
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, ticker, entry_date, entry_price, confidence,
                       stop_loss, target, exit_price, exit_date, exit_reason, notes
                FROM positions
                WHERE exit_date IS NOT NULL
                ORDER BY exit_date DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def close_my_position(position_id: int, exit_price: float, exit_reason: str = "Manual"):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE positions
                SET exit_price = %s, exit_date = %s, exit_reason = %s
                WHERE id = %s
            """, (exit_price, today, exit_reason, position_id))
        conn.commit()
    finally:
        conn.close()
