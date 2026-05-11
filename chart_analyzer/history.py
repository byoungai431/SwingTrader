from __future__ import annotations

import os
import json
import pathlib
import psycopg2
import psycopg2.extras
from datetime import datetime


def _load_toml_secret(key: str) -> str:
    import tomllib
    toml_path = pathlib.Path(__file__).parent.parent / ".streamlit" / "secrets.toml"
    try:
        with open(toml_path, "rb") as f:
            return tomllib.load(f).get(key, "")
    except Exception:
        return ""


def get_conn():
    try:
        import streamlit as st
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = os.environ.get("DATABASE_URL", "") or _load_toml_secret("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not configured in secrets or environment.")
    return psycopg2.connect(url)


def init_db():
    """Create chart analyzer tables if they do not exist."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pattern_setups (
            id SERIAL PRIMARY KEY,
            ticker TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            stage TEXT NOT NULL,
            detected_at TEXT,
            approaching_at TEXT,
            confirmed_at TEXT,
            invalidated_at TEXT,
            key_levels JSONB,
            entry_price REAL,
            stop_price REAL,
            target_price REAL,
            vol_ratio REAL,
            chart_bars INTEGER,
            alert_sent_approaching INTEGER DEFAULT 0,
            alert_sent_confirmed INTEGER DEFAULT 0,
            notes TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pattern_backtest_stats (
            pattern_type TEXT PRIMARY KEY,
            profit_factor REAL,
            win_rate REAL,
            avg_bars_to_target INTEGER,
            sample_size INTEGER,
            date_range TEXT,
            passed INTEGER DEFAULT 0,
            last_run TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pattern_universe (
            ticker TEXT PRIMARY KEY,
            dollar_vol_20d REAL,
            refreshed_at TEXT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


# ── Universe ──────────────────────────────────────────────────────────────────

def write_universe(tickers: list, dollar_vols: list):
    """Upsert top-100 universe with dollar volumes and timestamp."""
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("DELETE FROM pattern_universe")
    for ticker, dvol in zip(tickers, dollar_vols):
        cur.execute(
            """
            INSERT INTO pattern_universe (ticker, dollar_vol_20d, refreshed_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (ticker) DO UPDATE
              SET dollar_vol_20d = EXCLUDED.dollar_vol_20d,
                  refreshed_at  = EXCLUDED.refreshed_at
            """,
            (ticker, float(dvol), now),
        )
    conn.commit()
    cur.close()
    conn.close()


def get_universe_tickers() -> list:
    """Return cached list of universe tickers. Empty list if table unpopulated."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT ticker FROM pattern_universe ORDER BY dollar_vol_20d DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]


def get_universe_refresh_date() -> str | None:
    """Return the refreshed_at timestamp of the most recent universe entry."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(refreshed_at) FROM pattern_universe")
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


# ── Backtest stats ─────────────────────────────────────────────────────────────

def write_backtest_stats(results: list):
    """Upsert pattern backtest results. Each item is a dict with keys:
    pattern_type, profit_factor, win_rate, avg_bars_to_target, sample_size,
    date_range, passed.
    """
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for r in results:
        cur.execute(
            """
            INSERT INTO pattern_backtest_stats
                (pattern_type, profit_factor, win_rate, avg_bars_to_target,
                 sample_size, date_range, passed, last_run)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pattern_type) DO UPDATE
              SET profit_factor       = EXCLUDED.profit_factor,
                  win_rate            = EXCLUDED.win_rate,
                  avg_bars_to_target  = EXCLUDED.avg_bars_to_target,
                  sample_size         = EXCLUDED.sample_size,
                  date_range          = EXCLUDED.date_range,
                  passed              = EXCLUDED.passed,
                  last_run            = EXCLUDED.last_run
            """,
            (
                r["pattern_type"],
                r["profit_factor"],
                r["win_rate"],
                r["avg_bars_to_target"],
                r["sample_size"],
                r["date_range"],
                int(r["passed"]),
                now,
            ),
        )
    conn.commit()
    cur.close()
    conn.close()


def get_backtest_stats() -> list:
    """Return all rows from pattern_backtest_stats as list of dicts."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM pattern_backtest_stats ORDER BY pattern_type")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


# ── Pattern setups ─────────────────────────────────────────────────────────────

def upsert_setup(setup: dict) -> int:
    """Insert new setup or update existing one for (ticker, pattern_type) pair.

    If an active (non-invalidated) row for this ticker+pattern_type exists,
    update it. Otherwise insert a new row. Returns the row id.
    """
    conn = get_conn()
    cur = conn.cursor()

    key_levels_json = json.dumps(setup.get("key_levels", {}))

    cur.execute(
        """
        SELECT id FROM pattern_setups
        WHERE ticker = %s AND pattern_type = %s AND invalidated_at IS NULL
        ORDER BY id DESC LIMIT 1
        """,
        (setup["ticker"], setup["pattern_type"]),
    )
    row = cur.fetchone()

    if row:
        row_id = row[0]
        cur.execute(
            """
            UPDATE pattern_setups SET
                stage                 = %s,
                approaching_at        = COALESCE(%s, approaching_at),
                confirmed_at          = COALESCE(%s, confirmed_at),
                invalidated_at        = COALESCE(%s, invalidated_at),
                key_levels            = %s,
                entry_price           = %s,
                stop_price            = %s,
                target_price          = %s,
                vol_ratio             = %s,
                chart_bars            = %s,
                alert_sent_approaching = COALESCE(%s, alert_sent_approaching),
                alert_sent_confirmed   = COALESCE(%s, alert_sent_confirmed),
                notes                 = COALESCE(%s, notes)
            WHERE id = %s
            """,
            (
                setup.get("stage"),
                setup.get("approaching_at"),
                setup.get("confirmed_at"),
                setup.get("invalidated_at"),
                key_levels_json,
                setup.get("entry_price"),
                setup.get("stop_price"),
                setup.get("target_price"),
                setup.get("vol_ratio"),
                setup.get("chart_bars"),
                setup.get("alert_sent_approaching"),
                setup.get("alert_sent_confirmed"),
                setup.get("notes"),
                row_id,
            ),
        )
    else:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute(
            """
            INSERT INTO pattern_setups
                (ticker, pattern_type, stage, detected_at, approaching_at,
                 confirmed_at, invalidated_at, key_levels, entry_price,
                 stop_price, target_price, vol_ratio, chart_bars,
                 alert_sent_approaching, alert_sent_confirmed, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                setup["ticker"],
                setup["pattern_type"],
                setup.get("stage", "DETECTED"),
                setup.get("detected_at", now),
                setup.get("approaching_at"),
                setup.get("confirmed_at"),
                setup.get("invalidated_at"),
                key_levels_json,
                setup.get("entry_price"),
                setup.get("stop_price"),
                setup.get("target_price"),
                setup.get("vol_ratio"),
                setup.get("chart_bars"),
                setup.get("alert_sent_approaching", 0),
                setup.get("alert_sent_confirmed", 0),
                setup.get("notes"),
            ),
        )
        row_id = cur.fetchone()[0]

    conn.commit()
    cur.close()
    conn.close()
    return row_id


def get_active_setups(stage: str | None = None) -> list:
    """Return active (non-invalidated) setups, optionally filtered by stage."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if stage:
        cur.execute(
            """
            SELECT * FROM pattern_setups
            WHERE invalidated_at IS NULL AND stage = %s
            ORDER BY confirmed_at DESC NULLS LAST, approaching_at DESC NULLS LAST, detected_at DESC
            """,
            (stage,),
        )
    else:
        cur.execute(
            """
            SELECT * FROM pattern_setups
            WHERE invalidated_at IS NULL
            ORDER BY confirmed_at DESC NULLS LAST, approaching_at DESC NULLS LAST, detected_at DESC
            """
        )
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    for r in rows:
        if isinstance(r.get("key_levels"), str):
            try:
                r["key_levels"] = json.loads(r["key_levels"])
            except Exception:
                r["key_levels"] = {}
    return rows


def mark_alert_sent(row_id: int, stage: str):
    """Set alert_sent_approaching or alert_sent_confirmed = 1 for a setup."""
    conn = get_conn()
    cur = conn.cursor()
    col = "alert_sent_approaching" if stage == "APPROACHING" else "alert_sent_confirmed"
    cur.execute(f"UPDATE pattern_setups SET {col} = 1 WHERE id = %s", (row_id,))
    conn.commit()
    cur.close()
    conn.close()


def invalidate_setup(row_id: int, reason: str = ""):
    """Mark a setup as invalidated."""
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "UPDATE pattern_setups SET invalidated_at = %s, notes = COALESCE(notes || ' | ' || %s, %s) WHERE id = %s",
        (now, reason, reason, row_id),
    )
    conn.commit()
    cur.close()
    conn.close()
