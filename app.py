import json
import math
import os
import psycopg2
import psycopg2.extras
import subprocess
import sys
import yfinance as yf
import streamlit as st
import streamlit.components.v1 as components
from streamlit_autorefresh import st_autorefresh
import plotly.graph_objects as go
from datetime import date as _date
from fetcher import fetch_price_data
from indicators import compute_indicators
from fundamentals import fetch_fundamentals
from history import (save_signal, get_history, dismiss_signal,
                      enter_position, get_my_open_positions,
                      get_my_position_history, close_my_position, get_conn,
                      get_watchlist, save_watchlist,
                      get_user_settings, save_user_settings)
from config import WATCHLIST, ANTHROPIC_API_KEY
from notify import send_exit_alert
from signal_engine import (compute_hot_sectors, get_sector_hunter_signal,
                           get_index_fade_signal, get_leveraged_signal,
                           get_regime_signal, get_chop_signal, get_pattern_signal)

st.set_page_config(page_title="Nexus Edge", layout="wide", page_icon="📊")

# ── Position sizing (mirrors backtest config) ────────────────────────────────
_SIZING_START_YEAR  = 1994   # year 1 of the model
_SIZING_BASE        = 2_000  # $ per trade in year 1
_SIZING_INCREMENT   = 300    # flat $ increase per subsequent year

def get_trade_size(date_str, confidence=None) -> int:
    """Return the model trade size for a given date, matching the backtest formula.
    5★ and 6★ (5★ MAX DIAMOND) trades are sized 30% larger, matching FIVE_STAR_SIZE_MULT=1.30."""
    try:
        year = int(str(date_str)[:4])
    except Exception:
        year = _SIZING_START_YEAR
    base = _SIZING_BASE + _SIZING_INCREMENT * max(0, year - _SIZING_START_YEAR)
    if confidence is not None and int(confidence) >= 5:
        base = round(base * 1.30)
    return base

@st.cache_data(ttl=3600)
def _get_sp500_sector_map_cached() -> dict:
    """Fetch S&P 500 GICS sector map from Wikipedia (cached 1 hour). Returns {ticker: sector}."""
    import urllib.request
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    try:
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        import pandas as pd
        table = pd.read_html(html)[0]
        table["Symbol"] = table["Symbol"].str.replace(".", "-", regex=False)
        return dict(zip(table["Symbol"], table["GICS Sector"]))
    except Exception:
        return {}


@st.cache_data(ttl=3600)
def _get_hot_sectors_cached() -> set:
    """Compute today's hot sectors (cached 1 hour)."""
    return compute_hot_sectors()


# Auto-refresh every 2 minutes so new scan results appear without manual reload
st_autorefresh(interval=2 * 60 * 1000, key="autorefresh")

# ── Auth gate ───────────────────────────────────────────────────────────────────
try:
    _st_user = getattr(st, "user", None) or getattr(st, "experimental_user", None)
    _auth_enabled = _st_user is not None and hasattr(_st_user, "is_logged_in")
except Exception:
    _st_user = None
    _auth_enabled = False

if _auth_enabled and not _st_user.is_logged_in:
    st.markdown(
        '<div style="display:flex;flex-direction:column;align-items:center;'
        'justify-content:center;height:80vh;gap:24px;">'
        '<div style="font-size:40px;letter-spacing:0.08em;font-weight:900;color:#c8d8f8;">NEXUS EDGE</div>'
        '<div style="font-size:13px;color:#4a5a7a;letter-spacing:0.22em;text-transform:uppercase;margin-top:6px;">Systematic Swing Signals</div>'
        '<div style="font-size:14px;color:#8888bb;margin-top:12px;">Sign in to access your signals and positions.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        st.button("Sign in with Google", on_click=st.login, args=("google",), use_container_width=True)
    st.stop()

if _auth_enabled and _st_user is not None:
    user_id: str = getattr(_st_user, "email", None) or getattr(_st_user, "name", None) or "unknown"
else:
    user_id: str = "unknown"

ADMIN_EMAIL = "byoungai431@gmail.com"
is_admin = user_id == ADMIN_EMAIL

DEMO_API_KEY = "your-api-key-here"
def is_demo_mode(): return ANTHROPIC_API_KEY == DEMO_API_KEY

if "analyzed" not in st.session_state:
    st.session_state.analyzed = False
if "show_recommended" not in st.session_state:
    st.session_state.show_recommended = False
if "show_positions" not in st.session_state:
    st.session_state.show_positions = False
if "auto_run" not in st.session_state:
    st.session_state.auto_run = False
if "watchlist" not in st.session_state or st.session_state.get("_wl_user") != user_id:
    st.session_state.watchlist = get_watchlist(user_id) or list(WATCHLIST)
    st.session_state._wl_user  = user_id
def badge(label, color): return f'<span class="badge badge-{color}">{label}</span>'
def stars(n):
    n = int(n)
    if n >= 6:
        return "Tier 1 · High Conviction"
    elif n >= 5:
        return "Tier 2 · Confident"
    elif n >= 4:
        return "Tier 3"
    return "—"
def conf_stars_html(n):
    """Return HTML tier badge; confidence=6 renders as Tier 1 High Conviction."""
    n = int(n)
    if n >= 6:
        return (
            '<span style="color:#40E0FF;font-weight:900;letter-spacing:3px;font-size:1em;'
            'text-shadow:0 0 8px rgba(64,224,255,1.0),0 0 20px rgba(0,191,255,0.7);">TIER 1</span>'
            '<span style="color:#40E0FF;font-size:0.72em;font-weight:700;'
            'letter-spacing:0.14em;text-shadow:0 0 8px rgba(64,224,255,0.8);'
            'margin-left:8px;">HIGH CONVICTION</span>'
        )
    if n >= 5:
        return (
            '<span style="color:#f9c846;font-weight:900;letter-spacing:3px;font-size:1em;">TIER 2</span>'
            '<span style="color:#f9c846;font-size:0.72em;font-weight:700;letter-spacing:0.14em;margin-left:8px;">CONFIDENT</span>'
        )
    if n >= 4:
        return '<span style="color:#a8a8c0;font-weight:700;letter-spacing:3px;font-size:1em;">TIER 3</span>'
    if n >= 3:
        return '<span style="color:#cd7f32;font-weight:600;letter-spacing:2px;font-size:1em;">TIER 4</span>'
    return '<span style="color:#5a5a7a;font-weight:600;">—</span>'


# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>

@import url('https://fonts.googleapis.com/css2?family=Exo+2:wght@300;400;600;700;800;900&display=swap');

/* ── Hide Streamlit toolbar / header / manage-app for non-admin users ── */
/* (admin byoungai431@gmail.com sees these natively — no CSS override) */

/* ── Base font — explicit elements only; spans excluded to protect icon fonts ── */
html, body, .stApp,
div, p, h1, h2, h3, h4, h5, h6,
label, input, textarea, select, button,
a, li, td, th, caption, pre {
    font-family: 'Exo 2', sans-serif !important;
}

/* ── Animations ── */
@-webkit-keyframes twinkle-a {
    0%, 100% { opacity: 0.15; }
    50%       { opacity: 0.90; }
}
@keyframes twinkle-a {
    0%, 100% { opacity: 0.15; }
    50%       { opacity: 0.90; }
}
@-webkit-keyframes twinkle-b {
    0%, 100% { opacity: 0.80; }
    50%       { opacity: 0.08; }
}
@keyframes twinkle-b {
    0%, 100% { opacity: 0.80; }
    50%       { opacity: 0.08; }
}
@-webkit-keyframes float-planet {
    0%, 100% { -webkit-transform: translateY(0px) rotate(-2deg); transform: translateY(0px) rotate(-2deg); }
    50%       { -webkit-transform: translateY(-10px) rotate(2deg); transform: translateY(-10px) rotate(2deg); }
}
@keyframes float-planet {
    0%, 100% { -webkit-transform: translateY(0px) rotate(-2deg); transform: translateY(0px) rotate(-2deg); }
    50%       { -webkit-transform: translateY(-10px) rotate(2deg); transform: translateY(-10px) rotate(2deg); }
}
@-webkit-keyframes nebula-pulse {
    0%, 100% { opacity: 0.35; }
    50%       { opacity: 0.55; }
}
@keyframes nebula-pulse {
    0%, 100% { opacity: 0.35; }
    50%       { opacity: 0.55; }
}
@-webkit-keyframes panel-glow {
    0%, 100% { box-shadow: 0 0 14px rgba(0,60,100,0.12), inset 0 0 30px rgba(5,10,20,0.5); }
    50%       { box-shadow: 0 0 24px rgba(0,80,130,0.20), inset 0 0 40px rgba(5,10,20,0.6); }
}
@keyframes panel-glow {
    0%, 100% { box-shadow: 0 0 14px rgba(0,60,100,0.12), inset 0 0 30px rgba(5,10,20,0.5); }
    50%       { box-shadow: 0 0 24px rgba(0,80,130,0.20), inset 0 0 40px rgba(5,10,20,0.6); }
}
@-webkit-keyframes title-shimmer {
    0%   { background-position: 0% 50%; }
    50%  { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}
@keyframes title-shimmer {
    0%   { background-position: 0% 50%; }
    50%  { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
}

/* ── Reusable animation classes ── */
.anim-float    { -webkit-animation: float-planet 7s ease-in-out infinite; animation: float-planet 7s ease-in-out infinite; display:inline-block; }
.anim-float-r  { -webkit-animation: float-planet 9s ease-in-out infinite reverse; animation: float-planet 9s ease-in-out infinite reverse; display:inline-block; }
.anim-shimmer  {
    background: linear-gradient(90deg, #c8d8f8, #90b8e0, #00C9B1, #90b8e0, #c8d8f8);
    background-size: 300% 300%;
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    -webkit-animation: title-shimmer 6s ease infinite;
    animation: title-shimmer 6s ease infinite;
}

/* ── Dark institutional background ── */
.stApp {
    background:
        radial-gradient(ellipse at 15% 85%, rgba(0,40,80,0.12) 0%, transparent 45%),
        radial-gradient(ellipse at 85% 15%, rgba(0,30,60,0.10) 0%, transparent 45%),
        radial-gradient(ellipse at top, #080e1c 0%, #050a14 60%, #030810 100%);
    min-height: 100vh;
}

/* ── No decorative background layers ── */
.stApp::before { content: none; }
.stApp::after  { content: none; }

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg,
        #060c18 0%,
        #070d1c 30%,
        #080e20 70%,
        #060c18 100%) !important;
    border-right: 1px solid #0d2035 !important;
    box-shadow: 3px 0 25px rgba(0,40,80,0.18) !important;
}
section[data-testid="stSidebar"] > div {
    background: transparent !important;
}
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] span {
    color: #c0d0e8 !important;
}
section[data-testid="stSidebar"] .stButton > button {
    background: linear-gradient(135deg, #003a5a 0%, #002240 100%) !important;
    color: #c8e0f0 !important;
    border: 1px solid #005580 !important;
    font-weight: 800 !important;
    letter-spacing: 0.12em !important;
    text-transform: uppercase !important;
    font-size: 13px !important;
    box-shadow: 0 0 14px rgba(0,80,120,0.30), 0 0 28px rgba(0,80,120,0.12) !important;
    -webkit-transition: all 0.3s ease !important;
    transition: all 0.3s ease !important;
    border-radius: 8px !important;
}
section[data-testid="stSidebar"] .stButton > button:hover {
    box-shadow: 0 0 22px rgba(0,100,150,0.55), 0 0 44px rgba(0,100,150,0.22) !important;
    -webkit-transform: translateY(-2px) !important;
    transform: translateY(-2px) !important;
}

/* ── Watchlist ticker buttons (classes applied via JS for cross-browser support) ── */
button.wl-ticker {
    background: rgba(255,255,255,0.04) !important;
    color: #c0c0ff !important;
    border: 1px solid rgba(120,120,200,0.25) !important;
    font-weight: 500 !important;
    letter-spacing: 0.10em !important;
    text-transform: uppercase !important;
    font-size: 13px !important;
    box-shadow: none !important;
    border-radius: 6px !important;
    -webkit-transition: all 0.2s ease !important;
    transition: all 0.2s ease !important;
}
button.wl-ticker:hover {
    background: rgba(100,80,220,0.18) !important;
    border-color: rgba(150,130,255,0.55) !important;
    box-shadow: 0 0 12px rgba(100,80,220,0.25) !important;
    -webkit-transform: translateX(3px) !important;
    transform: translateX(3px) !important;
}
button.wl-ticker-active {
    background: linear-gradient(90deg,#3b2fa0,#5b4fcf) !important;
    color: #fff !important;
    border: 1.5px solid #7c6fff !important;
    font-weight: 700 !important;
    box-shadow: 0 0 16px rgba(100,80,220,0.45) !important;
    border-radius: 6px !important;
    -webkit-transform: translateX(3px) !important;
    transform: translateX(3px) !important;
}

/* ── Return to Base button ── */
section[data-testid="stSidebar"] .stButton.home-btn > button {
    background: transparent !important;
    color: #00C9B1 !important;
    border: 1px solid #00C9B1 !important;
    box-shadow: 0 0 10px rgba(0,201,177,0.18), 0 0 20px rgba(0,201,177,0.07) !important;
    font-size: 12px !important;
}
section[data-testid="stSidebar"] .stButton.home-btn > button:hover {
    background: rgba(0,201,177,0.07) !important;
    box-shadow: 0 0 20px rgba(0,201,177,0.40), 0 0 40px rgba(0,201,177,0.16) !important;
    -webkit-transform: translateY(-2px) !important;
    transform: translateY(-2px) !important;
}

/* ── Top-align columns so signal expand doesn't shift neighbours ── */
[data-testid="stHorizontalBlock"] {
    align-items: flex-start !important;
}

/* ── Signal expand/collapse ── */
.signal-details summary {
    list-style: none;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: #3a5a6a;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-weight: 600;
    margin-top: 14px;
    padding: 4px 10px;
    border: 1px solid #0e2a38;
    border-radius: 20px;
    -webkit-transition: color 0.2s, border-color 0.2s;
    transition: color 0.2s, border-color 0.2s;
}
.signal-details summary::-webkit-details-marker { display: none; }
.signal-details summary:hover { color: #00C9B1; border-color: #00604a; }
.signal-details[open] summary { color: #00C9B1; border-color: #00604a; }

/* ── Badges ── */
.badge {
    display: inline-block;
    padding: 5px 14px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 600;
    margin: 4px 3px;
}
.badge-green  { background: #071f11; color: #39d98a; border: 1px solid #1e7a4a; }
.badge-yellow { background: #1a1200; color: #f9c846; border: 1px solid #7a5f00; }
.badge-red    { background: #200808; color: #ff6b6b; border: 1px solid #7a2020; }
.badge-grey   { background: #0d0d20; color: #8888bb; border: 1px solid #2a2a5a; }

/* ── Panels ── */
.panel {
    background: linear-gradient(135deg,
        rgba(8,14,30,0.97) 0%,
        rgba(5,9,20,0.99) 100%);
    border: 1px solid #0e2040;
    border-radius: 16px;
    padding: 20px 22px;
    height: 100%;
    -webkit-animation: panel-glow 7s ease-in-out infinite;
    animation: panel-glow 7s ease-in-out infinite;
    position: relative;
    overflow: hidden;
}
.panel::before {
    content: "";
    position: absolute;
    top: -50%; left: -50%;
    width: 200%; height: 200%;
    background: radial-gradient(ellipse at 30% 20%, rgba(0,60,100,0.04) 0%, transparent 60%);
    pointer-events: none;
}
.panel-title {
    font-size: 13px;
    color: #c0d0e8;
    font-weight: 800;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin-bottom: 16px;
    padding: 0 0 10px 14px;
    border-bottom: 1px solid #0e2a40;
    border-left: 3px solid #00C9B1;
    text-shadow: 0 0 10px rgba(0,180,160,0.30);
}

/* ── Signal text ── */
.signal-text-buy  { color:#39d98a; font-size:30px; font-weight:800; text-shadow:0 0 20px rgba(57,217,138,0.65); letter-spacing:0.06em; }
.signal-text-sell { color:#ff6b6b; font-size:30px; font-weight:800; text-shadow:0 0 20px rgba(255,107,107,0.65); letter-spacing:0.06em; }
.signal-text-none { color:#aaaacc; font-size:22px; font-weight:700; }
.conf-stars       { font-size:18px; letter-spacing:3px; margin:8px 0; font-weight:700; }
.conf-stars-max   { font-size:18px; letter-spacing:3px; margin:8px 0; color:#40E0FF; font-weight:900; text-shadow:0 0 10px rgba(64,224,255,0.9), 0 0 24px rgba(0,191,255,0.6), 0 0 40px rgba(0,140,255,0.3); }
.rationale        { font-size:13px; color:#c8c8e8; line-height:1.80; margin-top:10px; }

/* ── Range ── */
.range-label { font-size:10px; color:#aaaacc; margin-top:4px; letter-spacing:0.10em; text-transform:uppercase; }
.range-price { font-size:21px; font-weight:700; margin:2px 0; color:#eeeeff; }

/* ── Stock header ── */
.stock-header {
    background: linear-gradient(90deg, rgba(0,20,40,0.85), rgba(0,10,25,0.40));
    border-left: 3px solid #00C9B1;
    padding: 12px 18px;
    margin-bottom: 18px;
    border-radius: 0 10px 10px 0;
}

/* ── Entry/Stop/Target row ── */
.trade-levels {
    display: flex;
    gap: 14px;
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px solid #161640;
}
.trade-level-item { flex: 1; }
.trade-level-label { font-size: 10px; color: #aaaacc; letter-spacing: 0.12em; text-transform: uppercase; margin-bottom: 2px; }
.trade-level-value { font-size: 15px; font-weight: 700; color: #eeeeff; }

/* ── Fundamentals metric cards ── */
.fund-row {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin-top: 6px;
}
.fund-card {
    flex: 1;
    min-width: 90px;
    background: rgba(5,12,25,0.80);
    border: 1px solid #0e2040;
    border-radius: 12px;
    padding: 12px 10px;
    text-align: center;
}
.fund-card-label {
    font-size: 9px;
    color: #9999cc;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    margin-bottom: 6px;
}
.fund-card-value {
    font-size: 20px;
    font-weight: 700;
    color: #ddddf8;
    line-height: 1.1;
}
.fund-card-sub {
    font-size: 10px;
    color: #8888bb;
    margin-top: 3px;
}
.fund-bonus-badge {
    display: inline-block;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    margin-top: 10px;
    background: #0b1a0e;
    color: #39d98a;
    border: 1px solid #1e6a3a;
}
.fund-bonus-none {
    background: #0d0d20;
    color: #5555aa;
    border: 1px solid #1e1e4a;
}

/* ── Gauge panels — bordered containers ── */
[data-testid="stVerticalBlockBorderWrapper"] {
    background: linear-gradient(135deg,
        rgba(8,14,30,0.97) 0%,
        rgba(5,9,20,0.99) 100%) !important;
    border: 1px solid #0e2040 !important;
    border-radius: 16px !important;
    -webkit-animation: panel-glow 7s ease-in-out infinite;
    animation: panel-glow 7s ease-in-out infinite;
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
    background: transparent !important;
}

/* ── Selectbox cursors ── */
[data-testid="stSidebar"] input[type="text"],
[data-testid="stSidebar"] [data-baseweb="input"] input {
    cursor: text !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] [role="option"],
[data-testid="stSidebar"] [data-baseweb="menu"] [role="option"],
[data-testid="stSidebar"] [data-baseweb="select"] > div:first-child,
[data-testid="stSidebar"] .stButton > button,
[data-testid="stSidebar"] .home-btn > button {
    cursor: pointer !important;
}

/* ── Space-themed tabs ── */
@-webkit-keyframes tab-glow {
    0%, 100% { box-shadow: 0 0 8px rgba(120,100,255,0.25); }
    50%       { box-shadow: 0 0 18px rgba(120,100,255,0.55); }
}
@keyframes tab-glow {
    0%, 100% { box-shadow: 0 0 8px rgba(120,100,255,0.25); }
    50%       { box-shadow: 0 0 18px rgba(120,100,255,0.55); }
}

/* Tab bar background */
[data-testid="stTabs"] > div:first-child {
    background: linear-gradient(180deg, rgba(10,10,40,0.95) 0%, rgba(6,6,24,0.98) 100%) !important;
    border-radius: 14px 14px 0 0 !important;
    border: 1px solid #1c1c52 !important;
    border-bottom: none !important;
    padding: 6px 8px 0 8px !important;
    gap: 6px !important;
}

/* Individual tab buttons */
[data-testid="stTabs"] button[role="tab"] {
    background: rgba(15,15,45,0.6) !important;
    border: 1px solid #2a2a6a !important;
    border-bottom: none !important;
    border-radius: 10px 10px 0 0 !important;
    color: #6666bb !important;
    font-family: 'Exo 2', sans-serif !important;
    font-size: 13px !important;
    font-weight: 700 !important;
    letter-spacing: 0.12em !important;
    text-transform: uppercase !important;
    padding: 10px 22px !important;
    -webkit-transition: all 0.25s ease !important;
    transition: all 0.25s ease !important;
    position: relative !important;
}

/* Hover state */
[data-testid="stTabs"] button[role="tab"]:hover {
    background: rgba(40,30,100,0.7) !important;
    border-color: #5050bb !important;
    color: #c0c0ff !important;
}

/* Active / selected tab */
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
    background: linear-gradient(180deg, rgba(50,35,130,0.95) 0%, rgba(30,20,90,0.98) 100%) !important;
    border-color: #7060dd !important;
    color: #e0e0ff !important;
    -webkit-animation: tab-glow 3s ease-in-out infinite !important;
    animation: tab-glow 3s ease-in-out infinite !important;
}

/* Active tab glowing underline accent */
[data-testid="stTabs"] button[role="tab"][aria-selected="true"]::after {
    content: '';
    position: absolute;
    bottom: 0;
    left: 10%;
    width: 80%;
    height: 2px;
    background: linear-gradient(90deg, transparent, #a090ff, #ffd700, #a090ff, transparent);
    border-radius: 2px;
}

/* Tab content panel */
[data-testid="stTabs"] > div:last-child {
    background: linear-gradient(180deg, rgba(8,8,30,0.98) 0%, rgba(5,5,20,0.99) 100%) !important;
    border: 1px solid #1c1c52 !important;
    border-top: none !important;
    border-radius: 0 0 14px 14px !important;
    padding: 20px 16px !important;
}

/* Hide default Streamlit tab underline indicator */
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {
    display: none !important;
}
[data-testid="stTabs"] [data-baseweb="tab-border"] {
    display: none !important;
}

/* ── "View Chart" buttons inside tab content ── */
@-webkit-keyframes view-btn-pulse {
    0%, 100% { box-shadow: 0 0 6px rgba(100,80,220,0.30), 0 0 14px rgba(80,60,200,0.12); }
    50%       { box-shadow: 0 0 14px rgba(140,110,255,0.60), 0 0 28px rgba(100,80,220,0.25); }
}
@keyframes view-btn-pulse {
    0%, 100% { box-shadow: 0 0 6px rgba(100,80,220,0.30), 0 0 14px rgba(80,60,200,0.12); }
    50%       { box-shadow: 0 0 14px rgba(140,110,255,0.60), 0 0 28px rgba(100,80,220,0.25); }
}
[data-testid="stTabs"] > div:last-child .stButton > button {
    background: linear-gradient(135deg, rgba(40,28,110,0.90) 0%, rgba(25,16,80,0.95) 100%) !important;
    border: 1px solid #5a48cc !important;
    border-radius: 8px !important;
    color: #c0b8ff !important;
    font-family: 'Exo 2', sans-serif !important;
    font-size: 10px !important;
    font-weight: 700 !important;
    letter-spacing: 0.14em !important;
    text-transform: uppercase !important;
    padding: 5px 10px !important;
    min-height: 0 !important;
    height: auto !important;
    line-height: 1.4 !important;
    -webkit-animation: view-btn-pulse 3.5s ease-in-out infinite !important;
    animation: view-btn-pulse 3.5s ease-in-out infinite !important;
    -webkit-transition: all 0.2s ease !important;
    transition: all 0.2s ease !important;
    width: 100% !important;
}
[data-testid="stTabs"] > div:last-child .stButton > button:hover {
    background: linear-gradient(135deg, rgba(70,50,180,0.95) 0%, rgba(45,30,130,0.98) 100%) !important;
    border-color: #9080ff !important;
    color: #ffffff !important;
    -webkit-transform: translateY(-1px) !important;
    transform: translateY(-1px) !important;
}

</style>
""", unsafe_allow_html=True)

# Hide Streamlit chrome (toolbar, header, manage-app button) for non-admin users
if not is_admin:
    st.markdown("""
<style>
[data-testid="stToolbar"]          { display: none !important; }
[data-testid="stDecoration"]       { display: none !important; }
[data-testid="stHeader"]           { display: none !important; }
.stApp > header                    { display: none !important; }
[data-testid="stAppDeployButton"]      { display: none !important; }
[data-testid="stBottom"]               { display: none !important; }
[data-testid="stBottomBlockContainer"] { display: none !important; }
</style>
""", unsafe_allow_html=True)


# ── Price level parser ─────────────────────────────────────────────────────────
def _parse_level(s):
    """Parse '$123.45' or '$120 - $130' → float (lower bound) or None."""
    if not s:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    if "-" in s:
        s = s.split("-")[0].strip()
    try:
        return float(s)
    except Exception:
        return None


# ── Price chart helper ─────────────────────────────────────────────────────────
def price_chart(df, sig, ind):
    """Candlestick chart (last 90 days) with MA50/MA200 and signal levels."""
    chart_df = df.tail(90).copy()

    fig = go.Figure()

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=chart_df.index,
        open=chart_df["Open"],
        high=chart_df["High"],
        low=chart_df["Low"],
        close=chart_df["Close"],
        name="Price",
        increasing_line_color="#39d98a",
        decreasing_line_color="#ff6b6b",
        increasing_fillcolor="rgba(57,217,138,0.6)",
        decreasing_fillcolor="rgba(255,107,107,0.6)",
        showlegend=False,
    ))

    # MA50
    ma50 = chart_df["Close"].rolling(50).mean()
    fig.add_trace(go.Scatter(
        x=chart_df.index, y=ma50, name="MA50",
        line=dict(color="#f9c846", width=1.5), opacity=0.85,
    ))

    # MA200
    ma200 = chart_df["Close"].rolling(200).mean()
    fig.add_trace(go.Scatter(
        x=chart_df.index, y=ma200, name="MA200",
        line=dict(color="#a090ff", width=1.5), opacity=0.85,
    ))

    # Entry / Stop / Target horizontal lines (only for BUY/SELL signals)
    if sig.get("signal") in ("BUY", "SELL"):
        entry_val  = _parse_level(sig.get("entry_zone")) or ind["latest_close"]
        stop_val   = _parse_level(sig.get("stop_loss"))
        target_val = _parse_level(sig.get("target"))

        if entry_val:
            fig.add_hline(y=entry_val, line=dict(color="#c0c0ff", dash="dash", width=1.5),
                          annotation_text="Entry", annotation_font_color="#c0c0ff",
                          annotation_position="right")
        if stop_val:
            fig.add_hline(y=stop_val, line=dict(color="#ff6b6b", dash="dot", width=1.5),
                          annotation_text="Stop", annotation_font_color="#ff6b6b",
                          annotation_position="right")
        if target_val:
            fig.add_hline(y=target_val, line=dict(color="#39d98a", dash="dot", width=1.5),
                          annotation_text="Target", annotation_font_color="#39d98a",
                          annotation_position="right")

    fig.update_layout(
        paper_bgcolor="#07071a",
        plot_bgcolor="#07071a",
        font=dict(color="#8888bb", size=11),
        xaxis=dict(showgrid=False, color="#3d3d88", rangeslider_visible=False,
                   showline=False, tickfont=dict(color="#3d3d88")),
        yaxis=dict(showgrid=True, gridcolor="#12124a", color="#5555aa",
                   tickfont=dict(color="#5555aa"), side="right"),
        margin=dict(l=0, r=60, t=8, b=8),
        legend=dict(orientation="h", y=1.06, x=0, bgcolor="rgba(0,0,0,0)",
                    font=dict(size=11, color="#8888bb")),
        height=300,
    )
    return fig


# ── Gauge helpers ──────────────────────────────────────────────────────────────
def rsi_gauge(rsi_value):
    if rsi_value <= 30:
        bar_color = "#39d98a"   # green — oversold, good buy zone
    elif rsi_value >= 70:
        bar_color = "#ff6b6b"   # red — overbought, caution zone
    else:
        t = (rsi_value - 30) / 40
        # interpolate green → yellow → red
        if t < 0.5:
            r = int(57  + (249 - 57)  * (t * 2))
            g = int(217 + (200 - 217) * (t * 2))
            b = int(138 + (70  - 138) * (t * 2))
        else:
            r = int(249 + (255 - 249) * ((t - 0.5) * 2))
            g = int(200 + (107 - 200) * ((t - 0.5) * 2))
            b = int(70  + (107 - 70)  * ((t - 0.5) * 2))
        bar_color = f"rgb({r},{g},{b})"

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=rsi_value,
        number={"font": {"size": 38, "color": "#c0c0ff"}, "suffix": ""},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#1c1c52",
                     "tickfont": {"color": "#3d3d88", "size": 11}},
            "bar":  {"color": bar_color, "thickness": 0.24},
            "bgcolor": "#07071c",
            "borderwidth": 0,
            "steps": [
                {"range": [0,  30], "color": "#081a0d"},   # green tint — oversold
                {"range": [30, 70], "color": "#09091e"},   # neutral
                {"range": [70,100], "color": "#1e0a0a"},   # red tint — overbought
            ],
            "threshold": {
                "line": {"color": bar_color, "width": 3},
                "thickness": 0.85,
                "value": rsi_value,
            }
        },
        domain={"x": [0, 1], "y": [0.22, 1]}
    ))
    fig.add_annotation(x=0.10, y=0.08, text="OVERSOLD", showarrow=False,
        font=dict(size=10, color="#39d98a", family="Exo 2"), xref="paper", yref="paper")
    fig.add_annotation(x=0.90, y=0.08, text="OVERBOUGHT", showarrow=False,
        font=dict(size=10, color="#ff6b6b", family="Exo 2"), xref="paper", yref="paper")
    fig.update_layout(
        height=190,
        margin=dict(l=18, r=18, t=18, b=25),
        paper_bgcolor="#07071c",
        font={"color": "#c0c0ff"},
    )
    return fig


def _fmt_vol(v):
    if v >= 1_000_000_000: return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:     return f"{v/1_000_000:.1f}M"
    if v >= 1_000:         return f"{v/1_000:.0f}K"
    return str(v)

def volume_gauge(rel_vol, vol_avg20=0, vol_today=0):
    clamped = min(rel_vol, 3.0)
    t = clamped / 3.0
    if t < 0.33:
        tt = t / 0.33
        r = 255
        g = int(107 + (200 - 107) * tt)
        b = int(107 + (70  - 107) * tt)
    else:
        tt = (t - 0.33) / 0.67
        r = int(255 + (57  - 255) * tt)
        g = int(200 + (217 - 200) * tt)
        b = int(70  + (138 - 70)  * tt)
    bar_color = f"rgb({r},{g},{b})"

    fig = go.Figure(go.Indicator(
        mode="gauge",
        value=clamped,
        gauge={
            "axis": {"range": [0, 3], "tickwidth": 1, "tickcolor": "#1c1c52",
                     "tickvals": [0, 1, 2, 3],
                     "tickfont": {"color": "#3d3d88", "size": 11}},
            "bar":  {"color": bar_color, "thickness": 0.24},
            "bgcolor": "#07071c",
            "borderwidth": 0,
            "steps": [
                {"range": [0,    0.75], "color": "#1e0a0a"},  # red tint — low volume
                {"range": [0.75, 1.5],  "color": "#09091e"},  # neutral
                {"range": [1.5,  3.0],  "color": "#081a0d"},  # green tint — high volume
            ],
            "threshold": {
                "line": {"color": bar_color, "width": 3},
                "thickness": 0.85,
                "value": clamped,
            }
        },
        domain={"x": [0, 1], "y": [0.22, 1]}
    ))
    fig.add_annotation(x=0.10, y=0.08, text="LOW", showarrow=False,
        font=dict(size=10, color="#ff6b6b", family="Exo 2"), xref="paper", yref="paper")
    fig.add_annotation(x=0.90, y=0.08, text="HIGH", showarrow=False,
        font=dict(size=10, color="#39d98a", family="Exo 2"), xref="paper", yref="paper")
    if vol_today:
        fig.add_annotation(x=0.06, y=0.60, text=_fmt_vol(vol_today), showarrow=False,
            font=dict(size=13, color=bar_color, family="Exo 2"), xref="paper", yref="paper",
            xanchor="center")
        fig.add_annotation(x=0.06, y=0.46, text="TODAY", showarrow=False,
            font=dict(size=8, color="#4a4a88", family="Exo 2"), xref="paper", yref="paper",
            xanchor="center")
    if vol_avg20:
        fig.add_annotation(x=0.5, y=0.46, text=_fmt_vol(vol_avg20), showarrow=False,
            font=dict(size=18, color="#555577", family="Exo 2"), xref="paper", yref="paper")
        fig.add_annotation(x=0.5, y=0.32, text="AVG VOL", showarrow=False,
            font=dict(size=9, color="#333355", family="Exo 2"), xref="paper", yref="paper")
    fig.update_layout(
        height=190,
        margin=dict(l=42, r=18, t=18, b=25),
        paper_bgcolor="#07071c",
        font={"color": "#c0c0ff"},
    )
    return fig



def mock_signal(ticker):
    return {
        "ticker": ticker,
        "signal": "NO TRADE",
        "confidence_stars": 0,
        "strategies_aligned": [],
        "fundamentals_bonus": False,
        "rationale": "Demo mode — add your Anthropic API key in config.py to generate real AI signals.",
        "entry_zone": None,
        "stop_loss": None,
        "target": None,
    }


# ── Enter-position confirmation dialog ───────────────────────────────────────
@st.dialog("Enter Position")
def _enter_position_dialog(ticker, signal_id, default_price, confidence, stop_loss, target, user_id):
    st.markdown(f"Set your fill price for **{ticker}**, then confirm.")
    price = st.number_input("Entry price ($)", min_value=0.01, value=float(default_price), step=0.01)
    _settings = get_user_settings(user_id)
    _bal_val = float(_settings.get("starting_balance") or 0)
    _default_amt = _bal_val * 0.2 if _bal_val > 0 else 2_000.0  # 20% of balance, or $2k default
    amount = st.number_input("Amount to invest ($)", min_value=0.01, value=_default_amt, step=100.0,
                             help="How much $ you're putting into this trade")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅  Confirm", use_container_width=True, type="primary"):
            enter_position(
                signal_id=signal_id, ticker=ticker, entry_price=price,
                confidence=confidence, stop_loss=stop_loss, target=target,
                user_id=user_id, position_amount=amount,
            )
            st.toast(f"✅ {ticker} added to My Positions at ${price:.2f}")
            st.rerun()
    with c2:
        if st.button("✖  Cancel", use_container_width=True):
            st.rerun()


# ── Manual enter-position dialog (watchlist / user picks) ────────────────────
@st.dialog("Enter Position")
def _enter_position_manual_dialog(ticker, default_price, user_id):
    st.markdown(f"Track your own position in **{ticker}**.")
    price = st.number_input("Entry price ($)", min_value=0.01, value=float(default_price), step=0.01)
    _settings = get_user_settings(user_id)
    _bal_val = float(_settings.get("starting_balance") or 0)
    _default_amt = _bal_val * 0.2 if _bal_val > 0 else 2_000.0  # 20% of balance, or $2k default
    amount = st.number_input("Amount to invest ($)", min_value=0.01, value=_default_amt, step=100.0,
                             help="How much $ you're putting into this trade")
    col_sl, col_tp = st.columns(2)
    with col_sl:
        stop_loss = st.number_input("Stop loss ($)", min_value=0.0, value=0.0, step=0.01,
                                    help="Optional — leave 0 to skip")
    with col_tp:
        target = st.number_input("Target ($)", min_value=0.0, value=0.0, step=0.01,
                                 help="Optional — leave 0 to skip")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅  Confirm", use_container_width=True, type="primary"):
            enter_position(
                signal_id=None, ticker=ticker, entry_price=price,
                confidence=0,
                stop_loss=stop_loss if stop_loss > 0 else None,
                target=target if target > 0 else None,
                user_id=user_id, position_amount=amount,
            )
            st.toast(f"✅ {ticker} added to My Positions at ${price:.2f}")
            st.rerun()
    with c2:
        if st.button("✖  Cancel", use_container_width=True):
            st.rerun()


# ── Recommended view ────────────────────────────────────────────────────────
def _render_signal_cards(rows, current_prices, user_id=""):
    """Render open-signal cards for a list of DB rows."""
    five_star_max = [r for r in rows if int(r["confidence"]) >= 6]
    five_star     = [r for r in rows if int(r["confidence"]) == 5]
    four_star     = [r for r in rows if int(r["confidence"]) == 4]

    for section_label, icon, section_rows in [
        ("Tier 1 · High Conviction", "◆", five_star_max),
        ("Tier 2 · Confident", "◇", five_star),
        ("Tier 3 · Qualified", "▪", four_star),
    ]:
        if not section_rows:
            continue
        is_max_section = section_label == "Tier 1 · High Conviction"
        hdr_color  = "#40E0FF" if is_max_section else "#8888bb"
        hdr_border = "#003a4a" if is_max_section else "#1c1c4a"
        st.markdown(
            f'<div style="font-size:11px;color:{hdr_color};letter-spacing:0.20em;text-transform:uppercase;'
            f'margin:24px 0 12px 0;padding-bottom:8px;border-bottom:1px solid {hdr_border};">'
            f'{icon} &nbsp; {section_label} &nbsp;'
            f'<span style="color:#33336a;">({len(section_rows)} open)</span></div>',
            unsafe_allow_html=True
        )
        for r in section_rows:
            entry = r["price"]
            cur   = current_prices.get(r["ticker"])
            conf_html = conf_stars_html(int(r["confidence"]))

            if entry and cur:
                pnl_pct   = (cur - entry) / entry * 100 if r["signal"] == "BUY" else (entry - cur) / entry * 100
                pnl_color = "#39d98a" if pnl_pct >= 0 else "#ff6b6b"
                pnl_str   = f'<span style="color:{pnl_color};font-weight:700;">{pnl_pct:+.2f}%</span>'
                cur_str   = f"${cur:.2f}"
            else:
                pnl_str = '<span style="color:#5555aa;">—</span>'
                cur_str = "—"

            sig_badge = badge("▲ BUY", "green") if r["signal"] == "BUY" else badge("▼ SELL", "red")

            # Staleness badge
            try:
                sig_date  = _date.fromisoformat(r["date"])
                days_old  = (_date.today() - sig_date).days
                if days_old == 0:
                    age_color, age_label = "#39d98a", "Today"
                elif days_old == 1:
                    age_color, age_label = "#39d98a", "1d old"
                elif days_old <= 3:
                    age_color, age_label = "#f9c846", f"{days_old}d old"
                else:
                    age_color, age_label = "#ff6b6b", f"⚠️ {days_old}d old"
                staleness_html = (
                    f'<span style="font-size:11px;color:{age_color};background:rgba(0,0,0,0.25);'
                    f'border:1px solid {age_color};padding:1px 7px;border-radius:8px;'
                    f'font-weight:600;margin-left:8px;">{age_label}</span>'
                )
            except Exception:
                staleness_html = ""

            st.markdown(
                f'<div class="panel" style="margin-bottom:6px;">'
                f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;">'
                f'<div>'
                f'<span style="font-size:24px;font-weight:900;color:#c0c0ff;">{r["ticker"]}</span>'
                f'<span style="font-size:12px;color:#44447a;margin-left:12px;">Signal date: {r["date"]}</span>'
                f'{staleness_html}'
                f'</div>'
                f'<div style="display:flex;gap:8px;align-items:center;">'
                f'<span style="font-size:16px;letter-spacing:2px;">{conf_html}</span>'
                f'{sig_badge}'
                f'</div>'
                f'</div>'
                f'<div style="display:flex;gap:20px;flex-wrap:wrap;">'
                f'<div style="min-width:70px;">'
                f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Entry</div>'
                f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">${f"{entry:.2f}" if entry else "—"}</div>'
                f'</div>'
                f'<div style="min-width:70px;">'
                f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Current</div>'
                f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">{cur_str}</div>'
                f'</div>'
                f'<div style="min-width:70px;">'
                f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">P&amp;L</div>'
                f'<div style="font-size:17px;font-weight:600;">{pnl_str}</div>'
                f'</div>'
                f'<div style="min-width:80px;">'
                f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Stop Loss</div>'
                f'<div style="font-size:14px;color:#8888bb;">{r["stop_loss"] or "—"}</div>'
                f'</div>'
                f'<div style="min-width:80px;">'
                f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Target</div>'
                f'<div style="font-size:14px;color:#8888bb;">{r["target"] or "—"}</div>'
                f'</div>'
                f'</div>'
                + (
                    f'<details class="signal-details" style="margin-top:10px;">'
                    f'<summary>▸ &nbsp; Rationale</summary>'
                    f'<div class="rationale">{r["rationale"] or "—"}</div>'
                    f'</details>'
                    if r["rationale"] else ""
                )
                + f'</div>',
                unsafe_allow_html=True
            )
            btn_c1, btn_c2 = st.columns([2, 1])
            with btn_c1:
                if st.button("🔭  See Analysis", key=f"view_open_{r['ticker']}_{r['date']}", use_container_width=True):
                    st.session_state.selected_ticker = r["ticker"]
                    st.session_state.analyzed = False
                    st.session_state.show_recommended = False
                    st.session_state.results_tickers = []
                    st.session_state.auto_run = True
                    st.rerun()
            with btn_c2:
                _already_in = any(
                    p["ticker"] == r["ticker"] and str(p.get("signal_id")) == str(r["id"])
                    for p in (get_my_open_positions(user_id) or [])
                )
                if _already_in:
                    st.button("✅ In Positions", key=f"in_pos_{r['ticker']}_{r['date']}", use_container_width=True, disabled=True)
                else:
                    if st.button("📈  Enter Position", key=f"enter_{r['ticker']}_{r['date']}", use_container_width=True):
                        _default_price = float(current_prices.get(r["ticker"]) or r["price"])
                        _enter_position_dialog(
                            ticker=r["ticker"],
                            signal_id=r["id"],
                            default_price=_default_price,
                            confidence=int(r["confidence"]),
                            stop_loss=r.get("stop_loss"),
                            target=r.get("target"),
                            user_id=user_id,
                        )

            st.markdown('<div style="margin-bottom:14px;"></div>', unsafe_allow_html=True)


def show_recommended_view(user_id=""):
    st.markdown(
        '<div style="text-align:center;padding:30px 0 20px 0;">'
        '<div class="anim-shimmer" style="font-size:32px;font-weight:900;letter-spacing:0.12em;text-transform:uppercase;">Active Signals</div>'
        '</div>',
        unsafe_allow_html=True
    )

    tab_open, tab_history = st.tabs(["◆  LIVE SIGNALS", "▶  TRADE HISTORY"])

    # ── Tab 1: Open Tier 1–3 signals ─────────────────────────────────────────
    with tab_open:
        try:
            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, ticker, date, signal, confidence,
                           entry_zone, stop_loss, target, price, rationale
                    FROM signals
                    WHERE exit_date IS NULL AND signal = 'BUY' AND confidence >= 3
                      AND NOT EXISTS (
                          SELECT 1 FROM signal_dismissals sd
                          WHERE sd.signal_id = signals.id AND sd.user_id = %s
                      )
                    ORDER BY confidence DESC, date DESC
                """, (user_id,))
                open_rows = cur.fetchall()
            conn.close()
        except Exception as e:
            st.error(f"Could not read signals database: {e}")
            return

        if not open_rows:
            st.markdown(
                '<div style="text-align:center;padding:60px 20px;">'
                '<div style="font-size:40px;">🔭</div>'
                '<div style="font-size:15px;color:#5555aa;margin-top:14px;letter-spacing:0.10em;">'
                'No open Tier 1–4 signals on record.<br>'
                '<span style="font-size:12px;">Run the signal engine to generate new signals.</span>'
                '</div></div>',
                unsafe_allow_html=True
            )
        else:
            tickers = list({r["ticker"] for r in open_rows})
            current_prices = {}
            with st.spinner("Fetching live prices..."):
                try:
                    raw = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
                    if len(tickers) == 1:
                        try:
                            current_prices[tickers[0]] = float(raw["Close"].dropna().iloc[-1].item())
                        except Exception:
                            pass
                    else:
                        for t in tickers:
                            try:
                                current_prices[t] = float(raw["Close"][t].dropna().iloc[-1].item())
                            except Exception:
                                pass
                except Exception:
                    pass
            _render_signal_cards(open_rows, current_prices, user_id)

    # ── Tab 2: Signal Log (all tiers, entered or passed) ─────────────────────
    with tab_history:
        try:
            conn = get_conn()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.id, s.ticker, s.date, s.signal, s.confidence, s.price,
                           s.exit_price, s.exit_date, s.stop_loss, s.target, s.rationale,
                           p.id as position_id
                    FROM signals s
                    LEFT JOIN positions p ON p.signal_id = s.id AND p.user_id = %s
                    WHERE s.confidence >= 3
                      AND s.signal = 'BUY'
                    ORDER BY s.date DESC
                """, (user_id,))
                hist_rows = cur.fetchall()
            conn.close()
        except Exception as e:
            st.error(f"Could not read trade history: {e}")
            return

        if not hist_rows:
            st.markdown(
                '<div style="text-align:center;padding:60px 20px;">'
                '<div style="font-size:40px;">📭</div>'
                '<div style="font-size:15px;color:#5555aa;margin-top:14px;letter-spacing:0.10em;">'
                'No historical signals yet.<br>'
                '<span style="font-size:12px;">History will appear here after the daily run fires signals.</span>'
                '</div></div>',
                unsafe_allow_html=True
            )
        else:
            # Summary stats — P&L only counts closed trades
            wins = closed_count = 0
            total_pnl = 0.0
            total_dollar_pnl = 0.0
            entered_wins = entered_closed = 0
            entered_pnl = 0.0
            entered_dollar_pnl = 0.0
            entered_count = sum(1 for r in hist_rows if r["position_id"])
            passed_count  = sum(1 for r in hist_rows if not r["position_id"])
            for r in hist_rows:
                if r["price"] and r["exit_price"]:
                    pnl = (r["exit_price"] - r["price"]) / r["price"] * 100 if r["signal"] == "BUY" \
                          else (r["price"] - r["exit_price"]) / r["price"] * 100
                    ts  = get_trade_size(r.get("date", ""), r.get("confidence"))
                    dollar_pnl = ts * pnl / 100
                    total_pnl += pnl
                    total_dollar_pnl += dollar_pnl
                    closed_count += 1
                    if pnl >= 0:
                        wins += 1
                    if r["position_id"]:
                        entered_pnl += pnl
                        entered_dollar_pnl += dollar_pnl
                        entered_closed += 1
                        if pnl >= 0:
                            entered_wins += 1
            win_rate      = wins / closed_count * 100 if closed_count else 0
            avg_pnl       = total_pnl / closed_count if closed_count else 0
            entered_wr    = entered_wins / entered_closed * 100 if entered_closed else 0
            entered_avg   = entered_pnl / entered_closed if entered_closed else 0

            pnl_color      = "#39d98a" if total_pnl  >= 0 else "#ff6b6b"
            avg_color      = "#39d98a" if avg_pnl     >= 0 else "#ff6b6b"
            ent_pnl_color  = "#39d98a" if entered_pnl >= 0 else "#ff6b6b"
            ent_avg_color  = "#39d98a" if entered_avg >= 0 else "#ff6b6b"
            ent_wr_color   = "#39d98a" if entered_wr  >= 50 else "#ff6b6b"
            dollar_color   = "#39d98a" if total_dollar_pnl >= 0 else "#ff6b6b"
            ent_dollar_color = "#39d98a" if entered_dollar_pnl >= 0 else "#ff6b6b"

            def _fmt_dollar(v):
                sign = "+" if v >= 0 else "-"
                return f"{sign}${abs(v):,.0f}"

            st.markdown(
                f'<div style="font-size:11px;color:#a090ff;letter-spacing:0.18em;text-transform:uppercase;'
                f'font-weight:700;margin:16px 0 6px 2px;padding:4px 10px;background:rgba(160,144,255,0.08);'
                f'border-left:3px solid #a090ff;border-radius:0 4px 4px 0;">📊 Overall Recommended</div>'
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;margin:0 0 8px 0;">'
                f'<div class="fund-card" style="flex:1;min-width:80px;">'
                f'<div class="fund-card-label">Signals</div>'
                f'<div class="fund-card-value">{len(hist_rows)}</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;">'
                f'<div class="fund-card-label">Entered</div>'
                f'<div class="fund-card-value" style="color:#39d98a;">{entered_count}</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;">'
                f'<div class="fund-card-label">Passed</div>'
                f'<div class="fund-card-value" style="color:#8888bb;">{passed_count}</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;">'
                f'<div class="fund-card-label">Win Rate</div>'
                f'<div class="fund-card-value" style="color:{"#39d98a" if win_rate >= 50 else "#ff6b6b"};">{win_rate:.0f}%</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;">'
                f'<div class="fund-card-label">Avg P&amp;L</div>'
                f'<div class="fund-card-value" style="color:{avg_color};">{avg_pnl:+.2f}%</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;">'
                f'<div class="fund-card-label">Total P&amp;L</div>'
                f'<div class="fund-card-value" style="color:{dollar_color};">{_fmt_dollar(total_dollar_pnl)}</div>'
                f'<div style="font-size:10px;color:#5050aa;margin-top:2px;">{total_pnl:+.1f}% sum</div>'
                f'</div>'
                f'</div>'
                f'<div style="font-size:11px;color:#39d98a;letter-spacing:0.18em;text-transform:uppercase;'
                f'font-weight:700;margin:0 0 6px 2px;padding:4px 10px;background:rgba(57,217,138,0.08);'
                f'border-left:3px solid #39d98a;border-radius:0 4px 4px 0;">📈 Entered Trades Only</div>'
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;margin:0 0 24px 0;">'
                f'<div class="fund-card" style="flex:1;min-width:80px;border-color:#2a3a6a;">'
                f'<div class="fund-card-label">Win Rate</div>'
                f'<div class="fund-card-value" style="color:{ent_wr_color};">{entered_wr:.0f}%</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;border-color:#2a3a6a;">'
                f'<div class="fund-card-label">Avg P&amp;L</div>'
                f'<div class="fund-card-value" style="color:{ent_avg_color};">{entered_avg:+.2f}%</div>'
                f'</div>'
                f'<div class="fund-card" style="flex:1;min-width:80px;border-color:#2a3a6a;">'
                f'<div class="fund-card-label">Total P&amp;L</div>'
                f'<div class="fund-card-value" style="color:{ent_dollar_color};">{_fmt_dollar(entered_dollar_pnl)}</div>'
                f'<div style="font-size:10px;color:#5050aa;margin-top:2px;">{entered_pnl:+.1f}% sum</div>'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True
            )

            def _render_hist_tier(tier_rows, tier_label, tier_subtitle, tier_color):
                if not tier_rows:
                    return
                t_entered = sum(1 for r in tier_rows if r["position_id"])
                t_wins = t_closed = 0
                t_pnl = 0.0
                for r in tier_rows:
                    if r["price"] and r["exit_price"]:
                        pnl = (r["exit_price"] - r["price"]) / r["price"] * 100 if r["signal"] == "BUY" \
                              else (r["price"] - r["exit_price"]) / r["price"] * 100
                        t_pnl += pnl
                        t_closed += 1
                        if pnl >= 0:
                            t_wins += 1
                t_wr  = t_wins / t_closed * 100 if t_closed else 0
                t_avg = t_pnl / t_closed if t_closed else 0
                wr_c  = "#39d98a" if t_wr >= 50 else "#ff6b6b"
                avg_c = "#39d98a" if t_avg >= 0 else "#ff6b6b"
                sub_html = (f'<span style="font-size:0.80em;letter-spacing:0.10em;opacity:0.70;margin-left:6px;">'
                            f'{tier_subtitle}</span>') if tier_subtitle else ''
                st.markdown(
                    f'<div style="font-size:11px;color:{tier_color};letter-spacing:0.18em;text-transform:uppercase;'
                    f'font-weight:700;margin:22px 0 8px 0;padding:5px 12px;'
                    f'background:rgba(160,144,255,0.05);border-left:3px solid {tier_color};border-radius:0 4px 4px 0;">'
                    f'{tier_label}{sub_html} &nbsp;·&nbsp; {len(tier_rows)} signals'
                    f'<span style="font-size:10px;color:#5a5a88;font-weight:600;margin-left:14px;">'
                    f'Entered: {t_entered} &nbsp;·&nbsp; '
                    f'W/R: <span style="color:{wr_c}">{t_wr:.0f}%</span>'
                    f' &nbsp;·&nbsp; Avg: <span style="color:{avg_c}">{t_avg:+.2f}%</span>'
                    f'</span></div>',
                    unsafe_allow_html=True
                )
                for r in tier_rows:
                    entry      = r["price"]
                    exit_price = r["exit_price"]
                    conf_html  = conf_stars_html(int(r["confidence"]))
                    sig_badge  = badge("▲ BUY", "green") if r["signal"] == "BUY" else badge("▼ SELL", "red")
                    if r["position_id"]:
                        status_badge = (
                            '<span style="background:rgba(57,217,138,0.15);color:#39d98a;'
                            'border:1px solid rgba(57,217,138,0.4);padding:2px 8px;border-radius:12px;'
                            'font-size:10px;font-weight:700;letter-spacing:1px;">ENTERED</span>'
                        )
                    else:
                        status_badge = (
                            '<span style="background:rgba(100,100,170,0.15);color:#8888bb;'
                            'border:1px solid rgba(100,100,170,0.3);padding:2px 8px;border-radius:12px;'
                            'font-size:10px;font-weight:700;letter-spacing:1px;">PASSED</span>'
                        )
                    if entry and exit_price:
                        pnl_pct    = (exit_price - entry) / entry * 100 if r["signal"] == "BUY" \
                                     else (entry - exit_price) / entry * 100
                        ts         = get_trade_size(r.get("date", ""), r.get("confidence"))
                        dollar_pnl = ts * pnl_pct / 100
                        pnl_color  = "#39d98a" if pnl_pct >= 0 else "#ff6b6b"
                        result_icon = "✅" if pnl_pct >= 0 else "❌"
                        d_sign     = "+" if dollar_pnl >= 0 else "-"
                        pnl_str    = (
                            f'<span style="color:{pnl_color};font-weight:700;font-size:20px;">'
                            f'{result_icon} {pnl_pct:+.2f}%</span>'
                            f'<span style="color:{pnl_color};font-size:13px;font-weight:500;margin-left:6px;">'
                            f'({d_sign}${abs(dollar_pnl):,.0f})</span>'
                        )
                        try:
                            from datetime import date as _date
                            d0 = _date.fromisoformat(r["date"])
                            d1 = _date.fromisoformat(r["exit_date"])
                            hold_days = (d1 - d0).days
                            date_meta = f'{r["date"]} → {r["exit_date"]} &nbsp;·&nbsp; held {hold_days}d'
                        except Exception:
                            date_meta = f'{r["date"]} → {r["exit_date"]}'
                    else:
                        pnl_str   = '<span style="color:#8888bb;font-size:13px;">Open / no exit recorded</span>'
                        date_meta = r["date"]
                    st.markdown(
                        f'<div class="panel" style="margin-bottom:6px;opacity:0.92;">'
                        f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;">'
                        f'<div>'
                        f'<span style="font-size:24px;font-weight:900;color:#c0c0ff;">{r["ticker"]}</span>'
                        f'<span style="font-size:12px;color:#44447a;margin-left:12px;">{date_meta}</span>'
                        f'</div>'
                        f'<div style="display:flex;gap:8px;align-items:center;">'
                        f'{status_badge}'
                        f'<span style="font-size:16px;letter-spacing:2px;">{conf_html}</span>'
                        f'{sig_badge}'
                        f'</div>'
                        f'</div>'
                        f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center;">'
                        f'<div style="min-width:80px;">'
                        f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Entry</div>'
                        f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">${f"{entry:.2f}" if entry else "—"}</div>'
                        f'</div>'
                        f'<div style="min-width:80px;">'
                        f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Exit</div>'
                        f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">${f"{exit_price:.2f}" if exit_price else "—"}</div>'
                        f'</div>'
                        f'<div style="min-width:100px;">'
                        f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Result</div>'
                        f'<div>{pnl_str}</div>'
                        f'</div>'
                        f'</div>'
                        + (
                            f'<details class="signal-details" style="margin-top:10px;">'
                            f'<summary>▸ &nbsp; Rationale</summary>'
                            f'<div class="rationale">{r["rationale"] or "—"}</div>'
                            f'</details>'
                            if r["rationale"] else ""
                        )
                        + f'</div>',
                        unsafe_allow_html=True
                    )
                    _, btn_col = st.columns([3, 1])
                    with btn_col:
                        if st.button("🔭  See Analysis", key=f"view_hist_{r['ticker']}_{r['date']}", use_container_width=True):
                            st.session_state.selected_ticker = r["ticker"]
                            st.session_state.analyzed = False
                            st.session_state.show_recommended = False
                            st.session_state.results_tickers = []
                            st.rerun()
                    st.markdown('<div style="margin-bottom:14px;"></div>', unsafe_allow_html=True)

            _render_hist_tier(
                [r for r in hist_rows if int(r["confidence"] or 0) >= 6],
                "TIER 1", "HIGH CONVICTION", "#40E0FF"
            )
            _render_hist_tier(
                [r for r in hist_rows if int(r["confidence"] or 0) == 5],
                "TIER 2", "CONFIDENT", "#f9c846"
            )
            _render_hist_tier(
                [r for r in hist_rows if int(r["confidence"] or 0) == 4],
                "TIER 3", "QUALIFIED", "#a8a8c0"
            )
            _render_hist_tier(
                [r for r in hist_rows if int(r["confidence"] or 0) == 3],
                "TIER 4", "RANGE REVERSION (E6)", "#cd7f32"
            )


# ── My Positions view ─────────────────────────────────────────────────────────
def show_positions_view(user_id=""):
    import yfinance as yf

    st.markdown(
        '<div style="text-align:center;padding:30px 0 16px 0;">'
        '<div class="anim-shimmer" style="font-size:38px;font-weight:900;letter-spacing:0.06em;">📈 My Positions</div>'
        '<div style="font-size:12px;color:#8888bb;letter-spacing:0.20em;text-transform:uppercase;margin-top:8px;">'
        'Trades you entered &nbsp;·&nbsp; live P&amp;L &amp; history</div>'
        '</div>',
        unsafe_allow_html=True
    )

    tab_open, tab_hist, tab_settings = st.tabs(["📂  OPEN POSITIONS", "🏆  CLOSED TRADES", "⚙️  SETTINGS"])

    # ── Tab 1: Open positions ─────────────────────────────────────────────────
    with tab_open:
        open_pos = get_my_open_positions(user_id)

        if not open_pos:
            st.markdown(
                '<div style="text-align:center;padding:60px 20px;">'
                '<div style="font-size:40px;">📭</div>'
                '<div style="font-size:15px;color:#5555aa;margin-top:14px;letter-spacing:0.10em;">'
                'No open positions yet.<br>'
                '<span style="font-size:12px;">Click "Enter Position" on a Recommended signal to add one.</span>'
                '</div></div>',
                unsafe_allow_html=True
            )
        else:
            # Fetch live prices
            tickers = list({p["ticker"] for p in open_pos})
            current_prices = {}
            with st.spinner("Fetching live prices..."):
                try:
                    raw = yf.download(tickers, period="5d", progress=False, auto_adjust=True)
                    if len(tickers) == 1:
                        try:
                            current_prices[tickers[0]] = float(raw["Close"].dropna().iloc[-1].item())
                        except Exception:
                            pass
                    else:
                        for t in tickers:
                            try:
                                current_prices[t] = float(raw["Close"][t].dropna().iloc[-1].item())
                            except Exception:
                                pass
                except Exception:
                    pass

            for p in open_pos:
                entry = p["entry_price"]
                cur   = current_prices.get(p["ticker"])
                conf  = int(p.get("confidence") or 0)
                conf_html = conf_stars_html(conf)

                if cur and entry:
                    pnl_pct   = (cur - entry) / entry * 100
                    pnl_color = "#39d98a" if pnl_pct >= 0 else "#ff6b6b"
                    _pamt     = p.get("position_amount")
                    if _pamt:
                        _dpnl = _pamt * pnl_pct / 100
                        _ds   = "+" if _dpnl >= 0 else "-"
                        pnl_str = (
                            f'<span style="color:{pnl_color};font-weight:700;">{pnl_pct:+.2f}%</span>'
                            f'<span style="color:{pnl_color};font-size:12px;font-weight:500;margin-left:5px;">'
                            f'({_ds}${abs(_dpnl):,.0f})</span>'
                        )
                    else:
                        pnl_str = f'<span style="color:{pnl_color};font-weight:700;">{pnl_pct:+.2f}%</span>'
                    cur_str   = f"${cur:.2f}"
                else:
                    pnl_str = '<span style="color:#5555aa;">—</span>'
                    cur_str = "—"

                st.markdown(
                    f'<div class="panel" style="margin-bottom:6px;">'
                    f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;">'
                    f'<div>'
                    f'<span style="font-size:24px;font-weight:900;color:#c0c0ff;">{p["ticker"]}</span>'
                    f'<span style="font-size:12px;color:#44447a;margin-left:12px;">Entered: {p["entry_date"]}</span>'
                    f'</div>'
                    f'<span style="font-size:16px;letter-spacing:2px;">{conf_html}</span>'
                    f'</div>'
                    f'<div style="display:flex;gap:20px;flex-wrap:wrap;">'
                    f'<div style="min-width:70px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Entry</div>'
                    f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">${entry:.2f}</div>'
                    f'</div>'
                    f'<div style="min-width:70px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Current</div>'
                    f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">{cur_str}</div>'
                    f'</div>'
                    f'<div style="min-width:70px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">P&amp;L</div>'
                    f'<div style="font-size:17px;font-weight:600;">{pnl_str}</div>'
                    f'</div>'
                    f'<div style="min-width:80px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Stop Loss</div>'
                    f'<div style="font-size:14px;color:#8888bb;">{p.get("stop_loss") or "—"}</div>'
                    f'</div>'
                    f'<div style="min-width:80px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Target</div>'
                    f'<div style="font-size:14px;color:#8888bb;">{p.get("target") or "—"}</div>'
                    f'</div>'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )
                col_close_price, col_close_btn = st.columns([2, 1])
                with col_close_price:
                    close_price_input = st.number_input(
                        "Close at price ($)",
                        min_value=0.01,
                        value=float(cur or entry),
                        step=0.01,
                        key=f"close_price_{p['id']}",
                        label_visibility="collapsed",
                    )
                with col_close_btn:
                    if st.button("🔴  Close Position", key=f"close_{p['id']}", use_container_width=True):
                        hit = close_my_position(p["id"], close_price_input, user_id=user_id)
                        if hit:
                            send_exit_alert([hit])
                        st.toast(f"✅ {p['ticker']} manually closed at ${close_price_input:.2f} — model still tracking")
                        st.rerun()
                st.markdown('<div style="margin-bottom:14px;"></div>', unsafe_allow_html=True)

    # ── Tab 2: Closed trades ──────────────────────────────────────────────────
    with tab_hist:
        hist = get_my_position_history(user_id)

        if not hist:
            st.markdown(
                '<div style="text-align:center;padding:60px 20px;">'
                '<div style="font-size:40px;">📊</div>'
                '<div style="font-size:15px;color:#5555aa;margin-top:14px;letter-spacing:0.10em;">'
                'No closed trades yet.</div></div>',
                unsafe_allow_html=True
            )
        else:
            wins = 0
            total_pnl = 0.0
            total_dollar_pnl = 0.0
            has_custom_amounts = False
            for t in hist:
                if t["exit_price"]:
                    _p = (t["exit_price"] - t["entry_price"]) / t["entry_price"] * 100
                    total_pnl += _p
                    _amt = t.get("position_amount") or get_trade_size(t.get("entry_date", ""), t.get("confidence"))
                    if t.get("position_amount"):
                        has_custom_amounts = True
                    total_dollar_pnl += _amt * _p / 100
                    if _p >= 0:
                        wins += 1
            total = len(hist)
            win_rate = wins / total * 100 if total else 0
            avg_pnl  = total_pnl / total if total else 0

            pnl_color = "#39d98a" if total_dollar_pnl >= 0 else "#ff6b6b"
            avg_color = "#39d98a" if avg_pnl  >= 0 else "#ff6b6b"
            wr_color  = "#39d98a" if win_rate >= 50 else "#ff6b6b"

            def _fmt_dollar(v):
                sign = "+" if v >= 0 else "-"
                return f"{sign}${abs(v):,.0f}"

            st.markdown(
                f'<div style="display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 24px 0;">'
                f'<div style="background:#0d0d2e;border:1px solid #2a1e78;border-radius:10px;padding:12px 20px;text-align:center;flex:1;min-width:90px;">'
                f'<div style="font-size:22px;font-weight:700;color:{wr_color};">{win_rate:.0f}%</div>'
                f'<div style="font-size:10px;color:#5050aa;letter-spacing:0.12em;text-transform:uppercase;margin-top:3px;">Win Rate</div></div>'
                f'<div style="background:#0d0d2e;border:1px solid #2a1e78;border-radius:10px;padding:12px 20px;text-align:center;flex:1;min-width:90px;">'
                f'<div style="font-size:22px;font-weight:700;color:{avg_color};">{avg_pnl:+.1f}%</div>'
                f'<div style="font-size:10px;color:#5050aa;letter-spacing:0.12em;text-transform:uppercase;margin-top:3px;">Avg P&amp;L</div></div>'
                f'<div style="background:#0d0d2e;border:1px solid #2a1e78;border-radius:10px;padding:12px 20px;text-align:center;flex:1;min-width:90px;">'
                f'<div style="font-size:22px;font-weight:700;color:{pnl_color};">{_fmt_dollar(total_dollar_pnl)}</div>'
                f'<div style="font-size:10px;color:#5050aa;letter-spacing:0.12em;text-transform:uppercase;margin-top:3px;">Total P&amp;L</div></div>'
                f'<div style="background:#0d0d2e;border:1px solid #2a1e78;border-radius:10px;padding:12px 20px;text-align:center;flex:1;min-width:90px;">'
                f'<div style="font-size:22px;font-weight:700;color:#a090ff;">{total}</div>'
                f'<div style="font-size:10px;color:#5050aa;letter-spacing:0.12em;text-transform:uppercase;margin-top:3px;">Trades</div></div>'
                f'</div>',
                unsafe_allow_html=True
            )

            for t in hist:
                entry = t["entry_price"]
                ep    = t["exit_price"]
                pnl   = (ep - entry) / entry * 100 if ep else 0
                ts    = t.get("position_amount") or get_trade_size(t.get("entry_date", ""), t.get("confidence"))
                dollar_pnl = ts * pnl / 100 if ep else None
                pnl_color = "#39d98a" if pnl >= 0 else "#ff6b6b"
                conf  = int(t.get("confidence") or 0)
                conf_html = conf_stars_html(conf)
                sig_badge = badge("▲ BUY", "green")
                dollar_str = f' <span style="font-size:13px;font-weight:500;">({_fmt_dollar(dollar_pnl)})</span>' if dollar_pnl is not None else ""

                st.markdown(
                    f'<div class="panel" style="margin-bottom:6px;">'
                    f'<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;">'
                    f'<div>'
                    f'<span style="font-size:24px;font-weight:900;color:#c0c0ff;">{t["ticker"]}</span>'
                    f'<span style="font-size:12px;color:#44447a;margin-left:12px;">{t["entry_date"]} → {t["exit_date"]}</span>'
                    f'</div>'
                    f'<div style="display:flex;gap:8px;align-items:center;">'
                    f'<span style="font-size:16px;letter-spacing:2px;">{conf_html}</span>'
                    f'{sig_badge}'
                    f'</div>'
                    f'</div>'
                    f'<div style="display:flex;gap:20px;flex-wrap:wrap;align-items:center;">'
                    f'<div style="min-width:80px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Entry</div>'
                    f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">${entry:.2f}</div>'
                    f'</div>'
                    f'<div style="min-width:80px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Exit</div>'
                    f'<div style="font-size:17px;color:#c0c0ff;font-weight:600;">${ep:.2f if ep else "—"}</div>'
                    f'</div>'
                    f'<div style="min-width:100px;">'
                    f'<div style="font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;">Result</div>'
                    f'<div style="font-size:17px;font-weight:700;color:{pnl_color};">{pnl:+.2f}%{dollar_str}</div>'
                    f'</div>'
                    f'{"<div style=min-width:120px;><div style=font-size:9px;color:#5555aa;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:3px;>Exit Reason</div><div style=font-size:13px;color:#8888bb;>" + (t.get("exit_reason") or "—") + "</div></div>" if t.get("exit_reason") else ""}'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True
                )
                st.markdown('<div style="margin-bottom:8px;"></div>', unsafe_allow_html=True)

    # ── Tab 3: Settings ───────────────────────────────────────────────────────
    with tab_settings:
        st.markdown(
            '<div style="font-size:11px;color:#a090ff;letter-spacing:0.18em;text-transform:uppercase;'
            'font-weight:700;margin:16px 0 16px 2px;padding:4px 10px;background:rgba(160,144,255,0.08);'
            'border-left:3px solid #a090ff;border-radius:0 4px 4px 0;">💰 Account Settings</div>',
            unsafe_allow_html=True
        )
        _s = get_user_settings(user_id)
        _bal = st.number_input(
            "Starting balance ($)",
            min_value=0.0, max_value=10_000_000.0,
            value=float(_s.get("starting_balance") or 10_000),
            step=500.0,
            help="Used to suggest default trade sizes when entering positions",
            key="main_starting_balance",
        )
        st.caption("Used to pre-fill the suggested trade size when you enter a position (20% of balance). Signals are always generated regardless of your balance.")
        if st.button("💾  Save Settings", type="primary"):
            save_user_settings(user_id, _bal)
            st.toast("✅ Settings saved")


# ── Sidebar ────────────────────────────────────────────────────────────────────
st.sidebar.markdown(
    '<div style="text-align:center;padding:18px 0 6px 0;">'
    '<div style="font-size:11px;letter-spacing:0.28em;color:#2a3d55;text-transform:uppercase;font-weight:700;margin-bottom:14px;">SIGNAL DASHBOARD</div>'
    '<div style="font-size:30px;font-weight:900;letter-spacing:0.10em;color:#c8d8f0;line-height:1;">NEXUS</div>'
    '<div style="font-size:13px;font-weight:700;letter-spacing:0.35em;color:#00C9B1;text-transform:uppercase;margin-bottom:8px;">EDGE</div>'
    '<div style="font-size:9px;color:#2a3d55;letter-spacing:0.18em;text-transform:uppercase;margin-top:4px;">Systematic Swing Signals · S&amp;P 500</div>'
    '</div>',
    unsafe_allow_html=True
)

st.sidebar.divider()

# ── User info + logout ─────────────────────────────────────────────────────────
_display_name = (getattr(_st_user, "name", None) or getattr(_st_user, "email", None) or "User") if _st_user else "User"
st.sidebar.markdown(
    f'<div style="font-size:11px;color:#5555aa;text-align:center;padding:2px 0 6px 0;">'
    f'Signed in as<br><span style="color:#c0c0ff;font-weight:600;">{_display_name}</span></div>',
    unsafe_allow_html=True,
)
st.sidebar.button("Sign out", on_click=st.logout, use_container_width=True)
st.sidebar.divider()

if is_demo_mode():
    st.sidebar.warning("Demo mode — real signals require API key in config.py")

if "selected_ticker" not in st.session_state:
    st.session_state.selected_ticker = None

# JS — styles watchlist buttons
_active_ticker = st.session_state.get("selected_ticker") or ""
_wl_json       = json.dumps(st.session_state.watchlist)
components.html(f"""
<script>
(function() {{
    var doc = window.parent.document;
    var watchlist = {_wl_json};
    var active    = {json.dumps(_active_ticker)};

    function applyAll() {{
        var sidebar = doc.querySelector('[data-testid="stSidebar"]');
        if (!sidebar) {{ setTimeout(applyAll, 150); return; }}
        var wlFound = 0;
        sidebar.querySelectorAll('button').forEach(function(btn) {{
            var txt = btn.textContent.trim();
            if (watchlist.indexOf(txt) !== -1) {{
                btn.classList.remove('wl-ticker', 'wl-ticker-active');
                btn.classList.add(txt === active ? 'wl-ticker-active' : 'wl-ticker');
                wlFound++;
            }}
        }});
        if (wlFound < watchlist.length) setTimeout(applyAll, 150);
    }}

    applyAll();
    new MutationObserver(applyAll).observe(doc.body, {{ childList: true, subtree: true }});
}})();
</script>
""", height=0)

st.sidebar.markdown(
    '<div style="padding:4px 0 8px 0;">'
    '<div style="font-size:9px;color:#2a3d55;letter-spacing:0.22em;text-transform:uppercase;margin-bottom:8px;text-align:center;">Signal Tiers</div>'
    '<div style="display:flex;flex-direction:column;gap:5px;">'
    '<div style="display:flex;align-items:center;gap:8px;padding:4px 8px;border-radius:8px;background:rgba(255,255,255,0.02);">'
    '<span style="color:#a0a0c0;font-size:11px;font-weight:700;">TIER 3</span>'
    '<span style="color:#3a4a5a;font-size:10px;">Qualified</span>'
    '</div>'
    '<div style="display:flex;align-items:center;gap:8px;padding:4px 8px;border-radius:8px;background:rgba(255,255,255,0.02);">'
    '<span style="color:#f9c846;font-size:11px;font-weight:700;">TIER 2</span>'
    '<span style="color:#3a4a5a;font-size:10px;">Confident</span>'
    '</div>'
    '<div style="display:flex;align-items:center;gap:8px;padding:4px 8px;border-radius:8px;background:rgba(64,224,255,0.04);border:1px solid rgba(64,224,255,0.15);">'
    '<span style="color:#40E0FF;font-size:11px;font-weight:900;text-shadow:0 0 8px rgba(64,224,255,0.8);">TIER 1</span>'
    '<span style="color:#3a4a5a;font-size:10px;">High Conviction</span>'
    '</div>'
    '</div>'
    '</div>',
    unsafe_allow_html=True
)

st.sidebar.divider()
st.sidebar.write("")
st.sidebar.markdown(
    '<div style="font-size:12px;color:#e8e8f8;letter-spacing:0.18em;'
    'text-transform:uppercase;font-weight:700;margin-bottom:8px;">'
    '<span style="font-size:15px;">🎯</span> &nbsp; Select Target</div>',
    unsafe_allow_html=True
)

for _t in st.session_state.watchlist:
    if st.sidebar.button(_t, key=f"wl_btn_{_t}", use_container_width=True):
        st.session_state.selected_ticker = _t
        st.session_state.analyzed = False
        st.session_state.auto_run = True
        st.session_state.show_recommended = False
        st.session_state.show_positions = False
        st.rerun()

selected_ticker = st.session_state.selected_ticker
selected_tickers = [selected_ticker] if selected_ticker else []

if selected_ticker:
    st.sidebar.write("")
    home = st.sidebar.button("🌌  Return to Base", use_container_width=True, key="home_btn")
    if home:
        st.session_state.selected_ticker = None
        st.session_state.analyzed = False
        st.session_state.show_recommended = False
        st.session_state.show_positions = False
        st.rerun()

st.sidebar.write("")
with st.sidebar.expander("⚙️  Manage Watchlist", expanded=False):
    # ── Add ticker ──
    with st.form("add_ticker_form", clear_on_submit=True):
        new_ticker = st.text_input(
            "Add ticker",
            placeholder="e.g. TSLA",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("＋  Add", use_container_width=True)
        if submitted and new_ticker:
            t = new_ticker.strip().upper()
            if t and t not in st.session_state.watchlist:
                st.session_state.watchlist.append(t)
                save_watchlist(user_id, st.session_state.watchlist)
                st.rerun()

    # ── Current watchlist with multi-select remove ──
    st.markdown(
        '<div style="font-size:10px;color:#5555aa;letter-spacing:0.12em;'
        'text-transform:uppercase;margin:8px 0 4px 0;">Current Watchlist</div>',
        unsafe_allow_html=True
    )
    if "wl_selected" not in st.session_state:
        st.session_state.wl_selected = set()

    # Select All / Deselect All toggle
    all_selected = set(st.session_state.watchlist) == st.session_state.wl_selected and len(st.session_state.watchlist) > 0
    if st.button("☑  Select All" if not all_selected else "☐  Deselect All", use_container_width=True):
        if not all_selected:
            st.session_state.wl_selected = set(st.session_state.watchlist)
        else:
            st.session_state.wl_selected = set()
        st.rerun()

    for t in list(st.session_state.watchlist):
        checked = st.checkbox(t, key=f"chk_{t}", value=(t in st.session_state.wl_selected))
        if checked:
            st.session_state.wl_selected.add(t)
        else:
            st.session_state.wl_selected.discard(t)

    if st.session_state.wl_selected:
        if st.button(f"🗑  Remove Selected ({len(st.session_state.wl_selected)})", use_container_width=True):
            for t in st.session_state.wl_selected:
                if t in st.session_state.watchlist:
                    st.session_state.watchlist.remove(t)
            save_watchlist(user_id, st.session_state.watchlist)
            st.session_state.wl_selected = set()
            st.rerun()

    st.markdown(
        '<div style="font-size:10px;color:#33335a;margin-top:6px;">'
        f'{len(st.session_state.watchlist)} stocks tracked</div>',
        unsafe_allow_html=True
    )

st.sidebar.divider()

st.sidebar.divider()

st.sidebar.markdown(
    '<div style="text-align:center;padding:8px 0 4px 0;">'
    '<div style="font-size:10px;color:#2a3d55;letter-spacing:0.15em;text-transform:uppercase;">Data: yfinance &nbsp;·&nbsp; AI: Claude</div>'
    '<div style="margin-top:8px;font-size:11px;letter-spacing:5px;color:#1a2d40;">· · · · ·</div>'
    '</div>',
    unsafe_allow_html=True
)


# ── Main header ────────────────────────────────────────────────────────────────
st.markdown(
    '<div style="text-align:center;padding:40px 0 16px 0;">'
    '<div class="anim-shimmer" style="font-size:52px;font-weight:900;letter-spacing:0.08em;margin:0 0 6px 0;line-height:1.0;">NEXUS EDGE</div>'
    '<div style="font-size:11px;color:#2a3d55;letter-spacing:0.28em;text-transform:uppercase;margin-bottom:18px;">Systematic Swing Signals &nbsp;·&nbsp; S&amp;P 500</div>'
    '<div style="display:inline-flex;align-items:center;gap:14px;'
    'background:rgba(255,255,255,0.02);border:1px solid #0e2040;border-radius:20px;padding:7px 22px;">'
    '<span style="font-size:9px;color:#2a3d55;letter-spacing:0.20em;text-transform:uppercase;font-weight:700;margin-right:4px;">Signal Tiers</span>'
    '<span style="color:#a8a8c0;font-size:10px;font-weight:700;">TIER 3</span>'
    '<span style="color:#2a3d55;font-size:10px;">·</span>'
    '<span style="color:#f9c846;font-size:10px;font-weight:700;">TIER 2 &nbsp;CONFIDENT</span>'
    '<span style="color:#2a3d55;font-size:10px;">·</span>'
    '<span style="color:#40E0FF;font-size:10px;font-weight:900;text-shadow:0 0 8px rgba(64,224,255,0.8);">TIER 1 &nbsp;HIGH CONVICTION</span>'
    '</div>'
    '</div>',
    unsafe_allow_html=True
)

# ── Mobile nav bar ─────────────────────────────────────────────────────────────
_mc1, _mc2, _mc3 = st.columns(3)
with _mc1:
    if st.button("◆  Signals", use_container_width=True, key="mob_rec"):
        st.session_state.show_recommended = not st.session_state.show_recommended
        st.session_state.show_positions = False
        st.rerun()
with _mc2:
    if st.button("📈  Positions", use_container_width=True, key="mob_pos"):
        st.session_state.show_positions = not st.session_state.show_positions
        st.session_state.show_recommended = False
        st.rerun()
with _mc3:
    _wl = st.session_state.watchlist
    if _wl:
        _mob_pick = st.selectbox("Stock", ["— Select —"] + _wl, label_visibility="collapsed", key="mob_ticker_sel")
        if _mob_pick and _mob_pick != "— Select —" and _mob_pick != st.session_state.get("selected_ticker"):
            st.session_state.selected_ticker = _mob_pick
            st.session_state.analyzed = False
            st.session_state.auto_run = True
            st.session_state.show_recommended = False
            st.session_state.show_positions = False
            st.rerun()

if st.session_state.show_recommended:
    show_recommended_view(user_id)
    st.stop()

if st.session_state.show_positions:
    show_positions_view(user_id)
    st.stop()

auto_run = st.session_state.auto_run
if auto_run:
    st.session_state.auto_run = False

if not auto_run and not st.session_state.get("results"):
    st.markdown(
        '<div style="text-align:center;padding:40px 20px;">'
        '<div style="font-size:15px;letter-spacing:0.12em;text-transform:uppercase;color:#4a5a7a;margin-bottom:8px;">◈ &nbsp; Select a ticker from the watchlist to begin analysis.</div>'
        '</div>',
        unsafe_allow_html=True
    )
    st.stop()

if not selected_tickers:
    st.warning("Please select at least one stock.")
    st.stop()

cached_tickers = st.session_state.get("results_tickers", [])
if auto_run or selected_tickers != cached_tickers:
    results = []
    # Load sector data once for E5 (cached, fast on repeat runs)
    _sp500_sectors = _get_sp500_sector_map_cached()
    _hot_etfs      = _get_hot_sectors_cached()
    try:
        _spy_df = fetch_price_data("SPY")
    except Exception:
        _spy_df = None
    progress = st.progress(0, text="Initializing analysis...")
    for i, ticker in enumerate(selected_tickers):
        progress.progress((i + 1) / len(selected_tickers), text=f"🔭 Scanning {ticker}...")
        try:
            df   = fetch_price_data(ticker)
            ind  = compute_indicators(df, ticker)
            fund = fetch_fundamentals(ticker)
            extra_sigs = []
            if is_demo_mode():
                sig = mock_signal(ticker)
            else:
                from signal_engine import get_signal
                sig = get_signal(ticker, ind)
                # SELL signals only apply to open BUY positions — suppress if none exists
                if sig["signal"] == "SELL":
                    open_buys = [p for p in get_my_open_positions(user_id) if p["ticker"] == ticker]
                    if not open_buys:
                        sig["signal"] = "NO TRADE"
                if sig["signal"] in ("BUY", "SELL"):
                    save_signal(ticker, sig, ind["latest_close"])
                # Engine 2: Index Fade
                sig_e2 = get_index_fade_signal(ticker, ind)
                if sig_e2.get("signal") == "BUY":
                    save_signal(sig_e2["ticker"], sig_e2, ind["latest_close"])
                    extra_sigs.append(sig_e2)

                # Engine 3: Leveraged Shock-Bounce
                sig_e3 = get_leveraged_signal(ticker, ind)
                if sig_e3.get("signal") == "BUY":
                    save_signal(sig_e3["ticker"], sig_e3, ind["latest_close"])
                    extra_sigs.append(sig_e3)

                # Engine 4: SPY Regime Momentum — only when scanning SPY
                if ticker == "SPY":
                    sig_e4 = get_regime_signal(ind)
                    if sig_e4.get("signal") == "BUY":
                        save_signal(sig_e4["ticker"], sig_e4, ind["latest_close"])
                        extra_sigs.append(sig_e4)

                # Engine 5: Sector Hunter
                _sector = _sp500_sectors.get(ticker)
                if _sector and _hot_etfs:
                    sig_e5 = get_sector_hunter_signal(ticker, _sector, ind, _hot_etfs)
                    if sig_e5.get("signal") == "BUY":
                        save_signal(ticker, sig_e5, ind["latest_close"])
                        extra_sigs.append(sig_e5)

                # Engine 6: Range Reversion
                _e6_skip = {"SPY", "QQQ", "RWM", "PSQ", "SPXU", "SQQQ", "UPRO", "TQQQ", "TSLL", "NVDL", "IWM"}
                if ticker not in _e6_skip and _spy_df is not None:
                    sig_e6 = get_chop_signal(ticker, df, _spy_df)
                    if sig_e6.get("signal") == "BUY":
                        save_signal(ticker, sig_e6, ind["latest_close"])
                        extra_sigs.append(sig_e6)

                # Engine 7: Double Bottom Pattern
                _e7_skip = {"SPY", "QQQ", "RWM", "PSQ", "SPXU", "SQQQ", "UPRO", "TQQQ", "TSLL", "NVDL", "IWM"}
                if ticker not in _e7_skip and _spy_df is not None:
                    sig_e7 = get_pattern_signal(ticker, df, _spy_df)
                    if sig_e7.get("signal") == "BUY":
                        save_signal(ticker, sig_e7, ind["latest_close"])
                        extra_sigs.append(sig_e7)
            results.append({"ticker": ticker, "ind": ind, "sig": sig, "df": df, "fund": fund,
                            "extra_sigs": extra_sigs})
        except Exception as _scan_err:
            results.append({
                "ticker": ticker, "ind": None, "sig": {
                    "ticker": ticker, "signal": "ERROR", "confidence_stars": 0,
                    "rationale": f"Analysis failed: {_scan_err}",
                    "strategies_aligned": [], "fundamentals_bonus": False,
                    "entry_zone": None, "stop_loss": None, "target": None,
                }, "df": None, "fund": None, "extra_sigs": [],
            })
    progress.empty()
    st.session_state.results         = results
    st.session_state.results_tickers = selected_tickers
    st.session_state.analyzed        = True
    st.success(f"✅ Analysis complete — {len(results)} ticker(s) processed.")
    st.rerun()
else:
    results = st.session_state.results

st.markdown('<hr style="border:none;height:1px;background:linear-gradient(90deg,transparent,#0e2040,#00C9B1,#0e2040,transparent);margin:24px 0;">', unsafe_allow_html=True)


# ── Per-stock display ──────────────────────────────────────────────────────────
for item in results:
    ticker = item["ticker"]
    ind    = item["ind"]
    sig    = item["sig"]
    df     = item["df"]
    fund   = item["fund"]

    # Stock header
    if ind is None:
        err_detail = sig.get("rationale", "Analysis failed.")
        st.markdown(
            f'<div class="stock-header">'
            f'<span style="font-size:24px;font-weight:900;color:#c0c0ff;">{ticker}</span>'
            f'</div>',
            unsafe_allow_html=True
        )
        st.markdown(
            f'<div style="background:rgba(255,160,50,0.08);border:1px solid rgba(255,160,50,0.35);'
            f'border-radius:10px;padding:16px 20px;margin-bottom:18px;">'
            f'<div style="font-size:16px;font-weight:700;color:#ffb347;margin-bottom:6px;">'
            f'⚠️ &nbsp; Could not compute indicators for {ticker}</div>'
            f'<div style="font-size:13px;color:#d0c0a0;line-height:1.6;">'
            f'This can happen during extreme market volatility when price data is incomplete or '
            f'statistical calculations encounter unusual values. Try again in a few minutes, '
            f'or check that the ticker symbol is valid.'
            f'</div>'
            f'<div style="font-size:11px;color:#887766;margin-top:8px;font-family:monospace;">'
            f'{err_detail}</div>'
            f'</div>',
            unsafe_allow_html=True
        )
        continue

    st.markdown(
        f'<div class="stock-header">'
        f'<span style="font-size:24px;font-weight:900;color:#c0c0ff;">{ticker}</span>'
        f'<span style="font-size:15px;color:#4444aa;font-weight:400;margin-left:14px;">'
        f'${ind["latest_close"]} &nbsp;·&nbsp; {ind["latest_date"]}</span>'
        f'</div>',
        unsafe_allow_html=True
    )

    col1, col2 = st.columns(2)

    # ── Panel 1: RSI & Indicators ─────────────────────────────────────────────
    with col1:
        rsi_color  = "green"  if ind["rsi"] < 30 else "red"   if ind["rsi"] > 70 else "yellow"
        macd_color = "green"  if ind["macd_crossover"] == "bullish" else \
                     "red"    if ind["macd_crossover"] == "bearish"  else "grey"
        ma_color   = "green"  if ind["golden_cross"] else "yellow"
        ma_label   = "Golden Cross ✓" if ind["golden_cross"] else "No Golden Cross"
        rsi_label  = f"RSI {ind['rsi']} — {ind['rsi_label']}"
        macd_label = f"MACD {ind['macd_crossover'].title()}"

        with st.container(border=True):
            st.markdown('<div class="panel-title">RSI Indicator</div>', unsafe_allow_html=True)
            st.plotly_chart(rsi_gauge(ind["rsi"]), use_container_width=True, config={"displayModeBar": False})
            st.markdown(
                f'<div style="text-align:center;margin-top:2px;">'
                f'{badge(rsi_label, rsi_color)}'
                f'{badge(macd_label, macd_color)}'
                f'{badge(ma_label, ma_color)}'
                f'</div>',
                unsafe_allow_html=True
            )

    # ── Panel 2: Signal ───────────────────────────────────────────────────────
    with col2:
        signal_val = sig["signal"]
        conf       = sig["confidence_stars"]
        rationale  = sig.get("rationale", "")

        if signal_val == "BUY":
            signal_html = '<div class="signal-text-buy">▲ &nbsp; BUY</div>'
        elif signal_val == "SELL":
            signal_html = '<div class="signal-text-sell">▼ &nbsp; SELL</div>'
        else:
            signal_html = f'<div class="signal-text-none">— &nbsp; {signal_val}</div>'

        if conf >= 6:
            conf_html = f'<div class="conf-stars-max">{conf_stars_html(conf)}</div>'
        elif conf > 0:
            conf_html = f'<div class="conf-stars">{conf_stars_html(conf)}</div>'
        else:
            conf_html = ""

        entry_html = ""
        if signal_val in ("BUY", "SELL") and sig.get("entry_zone"):
            entry_html = (
                f'<div class="trade-levels">'
                f'<div class="trade-level-item">'
                f'<div class="trade-level-label">Entry</div>'
                f'<div class="trade-level-value">{sig["entry_zone"] or "—"}</div>'
                f'</div>'
                f'<div class="trade-level-item">'
                f'<div class="trade-level-label">Stop</div>'
                f'<div class="trade-level-value">{sig["stop_loss"] or "—"}</div>'
                f'</div>'
                f'<div class="trade-level-item">'
                f'<div class="trade-level-label">Target</div>'
                f'<div class="trade-level-value">{sig["target"] or "—"}</div>'
                f'</div>'
                f'</div>'
            )

        details_content = f'<div class="rationale" style="margin-top:10px;">{rationale}</div>{entry_html}'
        st.markdown(
            f'<div class="panel">'
            f'<div class="panel-title">Signal</div>'
            f'{signal_html}'
            f'{conf_html}'
            f'<details class="signal-details">'
            f'<summary>▸ &nbsp; See Analysis</summary>'
            f'{details_content}'
            f'</details>'
            f'</div>',
            unsafe_allow_html=True
        )

    # ── Engine 5 Sector Hunter signal (if fired alongside E1) ────────────────
    for _e5 in item.get("extra_sigs", []):
        if _e5.get("signal") == "BUY":
            _e5_stars = _e5["confidence_stars"]
            _e5_star_lbl = "TIER 1" if _e5_stars >= 6 else "TIER 2" if _e5_stars >= 5 else "TIER 3"
            st.markdown(
                f'<div style="margin:10px 0 4px 0;padding:12px 18px;'
                f'background:rgba(80,200,120,0.08);border:1px solid rgba(80,200,120,0.35);'
                f'border-radius:10px;">'
                f'<div style="font-weight:800;color:#80ffaa;font-size:13px;margin-bottom:4px;">'
                f'🔍 &nbsp; SECTOR HUNTER &nbsp;{_e5_star_lbl}</div>'
                f'<div style="font-size:12px;color:#b0e8c8;line-height:1.55;">{_e5["rationale"]}</div>'
                f'<div style="margin-top:8px;font-size:11px;color:#607870;">'
                f'Entry {_e5["entry_zone"]} &nbsp;·&nbsp; Stop {_e5["stop_loss"]} '
                f'&nbsp;·&nbsp; Target {_e5["target"]}</div>'
                f'</div>',
                unsafe_allow_html=True
            )

    # ── Panel 3: 52-Week Range ────────────────────────────────────────────────
    pct_from_high = (ind["week52_high"] - ind["latest_close"]) / ind["latest_close"] * 100
    pct_from_low  = (ind["week52_low"]  - ind["latest_close"]) / ind["latest_close"] * 100
    high_color    = "green"  if pct_from_high < 10  else "yellow" if pct_from_high < 25  else "red"
    low_color     = "green"  if pct_from_low  > -20 else "yellow" if pct_from_low  > -50 else "grey"

    col_range, col_pad = st.columns(2)
    with col_range:
        rel_vol   = ind.get("rel_vol", 1.0)
        vol_label = "High Volume" if rel_vol >= 1.5 else "Low Volume" if rel_vol < 0.75 else "Avg Volume"
        vol_color = "green" if rel_vol >= 1.5 else "red" if rel_vol < 0.75 else "yellow"
        with st.container(border=True):
            st.markdown('<div class="panel-title">📊 &nbsp; Daily Volume</div>', unsafe_allow_html=True)
            st.plotly_chart(volume_gauge(rel_vol, ind.get("vol_avg20", 0), ind.get("vol_today", 0)), use_container_width=True, config={"displayModeBar": False})
            st.markdown(
                f'<div style="text-align:center;margin-top:2px;">'
                f'{badge(vol_label, vol_color)}'
                f'<br><span style="font-size:11px;color:#3d3d88;display:block;margin-top:4px;">'
                f'{ind["vol_today"]:,} shares &nbsp;·&nbsp; avg {ind["vol_avg20"]:,}</span>'
                f'</div>',
                unsafe_allow_html=True
            )

    with col_pad:
        st.markdown(
            f'<div class="panel">'
            f'<div class="panel-title">🌌 &nbsp; 52-Week Range</div>'
            f'<div style="display:flex;gap:32px;align-items:flex-start;flex-wrap:wrap;">'
            f'<div>'
            f'<div class="range-label">Yearly High &nbsp;·&nbsp; {ind["week52_high_date"]}</div>'
            f'<div class="range-price">${ind["week52_high"]}</div>'
            f'{badge(f"{pct_from_high:+.1f}% from current", high_color)}'
            f'</div>'
            f'<div>'
            f'<div class="range-label">Yearly Low &nbsp;·&nbsp; {ind["week52_low_date"]}</div>'
            f'<div class="range-price">${ind["week52_low"]}</div>'
            f'{badge(f"{pct_from_low:+.1f}% from current", low_color)}'
            f'</div>'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True
        )

    st.write("")

    # ── Price Chart ───────────────────────────────────────────────────────────
    with st.container(border=True):
        st.markdown('<div class="panel-title">📈 &nbsp; Price Chart &nbsp;<span style="font-size:10px;color:#33336a;font-weight:400;">90-day · MA50 · MA200</span></div>', unsafe_allow_html=True)
        st.plotly_chart(price_chart(df, sig, ind), use_container_width=True, config={"displayModeBar": False})

    st.write("")

    # ── Panel 4: Fundamentals ─────────────────────────────────────────────────
    def fund_val(v, suffix="", prefix="", na="—"):
        if v is None:
            return na
        if isinstance(v, float):
            return f"{prefix}{v:.2f}{suffix}"
        return f"{prefix}{v}{suffix}"

    def fund_color(metric, val):
        if val is None:
            return "#5555aa"
        if metric == "pe":
            return "#39d98a" if val < 20 else "#f9c846" if val < 35 else "#ff6b6b"
        if metric == "fwd_pe":
            return "#39d98a" if val < 18 else "#f9c846" if val < 30 else "#ff6b6b"
        if metric == "peg":
            return "#39d98a" if val < 1 else "#f9c846" if val < 2 else "#ff6b6b"
        if metric == "roe":
            return "#39d98a" if val > 15 else "#f9c846" if val > 5 else "#ff6b6b"
        if metric == "de":
            return "#39d98a" if val < 0.5 else "#f9c846" if val < 1.5 else "#ff6b6b"
        if metric == "fcf":
            return "#39d98a" if val > 0 else "#ff6b6b"
        return "#c0c0ff"

    metrics = [
        ("pe",     "P/E Ratio",   fund_val(fund.get("pe")),               fund_color("pe",     fund.get("pe")),     "How much you pay per $1 of earnings. Lower = cheaper stock."),
        ("fwd_pe", "Forward P/E", fund_val(fund.get("fwd_pe")),            fund_color("fwd_pe", fund.get("fwd_pe")), "P/E using next year's expected earnings. Lower = better value."),
        ("peg",    "PEG Ratio",   fund_val(fund.get("peg")),               fund_color("peg",    fund.get("peg")),    "P/E adjusted for growth rate. Under 1 often signals undervalued."),
        ("roe",    "ROE",         fund_val(fund.get("roe"), "%"),           fund_color("roe",    fund.get("roe")),    "Return on equity — how efficiently the company generates profit."),
        ("de",     "Debt / Equity", fund_val(fund.get("de")),              fund_color("de",     fund.get("de")),     "How much debt vs shareholder equity. Lower = stronger balance sheet."),
        ("fcf",    "Free Cash Flow", fund_val(fund.get("fcf_b"), "B", "$"), fund_color("fcf",   fund.get("fcf_b")), "Cash left after operations & capex. Positive = company funds itself."),
    ]

    cards_html = ""
    for _, label, value, color, sub in metrics:
        cards_html += (
            f'<div class="fund-card">'
            f'<div class="fund-card-label">{label}</div>'
            f'<div class="fund-card-value" style="color:{color};">{value}</div>'
            f'<div class="fund-card-sub">{sub}</div>'
            f'</div>'
        )

    if fund.get("fundamentals_strong"):
        bonus_html = '<div class="fund-bonus-badge">◆ Strong Fundamentals · +1 Tier Bonus Active</div>'
    else:
        bonus_html = '<div class="fund-bonus-badge fund-bonus-none">Fundamentals: No bonus triggered</div>'

    # ── Build rule-based fundamentals analysis ────────────────────────────────
    def fund_analysis(f):
        notes = []
        pe, fwd, peg, roe, de, fcf = (
            f.get("pe"), f.get("fwd_pe"), f.get("peg"),
            f.get("roe"), f.get("de"), f.get("fcf_b")
        )
        # Valuation
        if pe is not None and fwd is not None:
            if fwd < pe:
                notes.append(f"forward P/E ({fwd:.1f}) below trailing ({pe:.1f}), suggesting earnings are expected to grow")
            elif pe < 18:
                notes.append(f"P/E of {pe:.1f} is attractively valued")
            elif pe > 40:
                notes.append(f"P/E of {pe:.1f} is elevated — growth expectations are priced in")
        elif pe is not None:
            if pe < 18:   notes.append(f"P/E of {pe:.1f} looks undervalued")
            elif pe > 40: notes.append(f"P/E of {pe:.1f} is rich — momentum-dependent")
        if peg is not None:
            if peg < 1:   notes.append(f"PEG of {peg:.2f} signals the stock may be undervalued relative to growth")
            elif peg > 2: notes.append(f"PEG of {peg:.2f} suggests the market is pricing in significant growth")
        # Profitability
        if roe is not None:
            if roe > 30:   notes.append(f"ROE of {roe:.1f}% reflects exceptional capital efficiency")
            elif roe > 15: notes.append(f"ROE of {roe:.1f}% is solid")
            elif roe < 5:  notes.append(f"ROE of {roe:.1f}% is weak — limited profit generation from equity")
        # Balance sheet
        if de is not None:
            if de < 0.3:   notes.append("clean balance sheet with minimal debt")
            elif de > 2.0: notes.append(f"debt/equity of {de:.1f} is high — leverage adds risk")
        # Cash flow
        if fcf is not None:
            if fcf > 5:    notes.append(f"${fcf:.1f}B in free cash flow gives strong financial flexibility")
            elif fcf > 0:  notes.append("positive free cash flow — self-funding business")
            else:          notes.append("negative free cash flow — watch cash burn closely")

        if not notes:
            return "Insufficient data to assess fundamentals."
        first = notes[0].capitalize()
        rest  = "; ".join(notes[1:])
        return f"{first}{('; ' + rest) if rest else ''}."

    analysis_text = fund_analysis(fund)
    analysis_html = (
        f'<div style="font-size:12px;color:#8888bb;line-height:1.7;margin-bottom:14px;'
        f'padding:10px 14px;background:rgba(10,10,40,0.6);border-radius:10px;'
        f'border-left:3px solid #2a2a6a;">{analysis_text}</div>'
    )

    st.markdown(
        f'<div class="panel" style="margin-bottom:18px;">'
        f'<div class="panel-title">🔬 &nbsp; Fundamentals</div>'
        f'{analysis_html}'
        f'<div class="fund-row">{cards_html}</div>'
        f'<div style="text-align:center;">{bonus_html}</div>'
        f'</div>',
        unsafe_allow_html=True
    )

    # ── Pattern selector ──────────────────────────────────────────────────────
    patterns = ind.get("patterns", [])
    sel_pat  = None
    if patterns:
        sk = f"pat_{ticker}"
        if sk not in st.session_state:
            st.session_state[sk] = 0

        st.markdown(
            '<div style="font-size:10px;color:#5555aa;letter-spacing:0.15em;'
            'text-transform:uppercase;margin-bottom:6px;">Detected Patterns — select to overlay</div>',
            unsafe_allow_html=True
        )
        btn_cols = st.columns(len(patterns))
        for idx, (p, col) in enumerate(zip(patterns, btn_cols)):
            is_sel   = st.session_state[sk] == idx
            icon     = "✦" if p.get("confirmed") else "◎"
            status   = "confirmed" if p.get("confirmed") else "forming"
            btn_lbl  = f"{icon} {p['pattern']} ({status})"
            if col.button(btn_lbl, key=f"pat_{ticker}_{idx}", use_container_width=True):
                st.session_state[sk] = idx

        sel_idx = st.session_state[sk]
        if sel_idx < len(patterns):
            sel_pat = patterns[sel_idx]
            # Description + measured move info bar
            mm      = sel_pat.get("measured_move")
            mm_str  = f" &nbsp;·&nbsp; Target <b>${mm}</b>" if mm else ""
            pat_name = sel_pat["pattern"]
            if sel_pat.get("bearish"):
                conf_str = (f'<span style="color:#f87171;">✦ {pat_name} Confirmed breakdown</span>'
                            if sel_pat.get("confirmed") else
                            f'<span style="color:#fb923c;">◎ {pat_name} Forming — not yet confirmed</span>')
            else:
                conf_str = (f'<span style="color:#39d98a;">✦ {pat_name} Confirmed breakout</span>'
                            if sel_pat.get("confirmed") else
                            f'<span style="color:#7c3aed;">◎ {pat_name} Forming — not yet confirmed</span>')
            # Brief investor description per pattern type
            _plain = {
                "Double Bottom": "Bullish reversal — support held twice, signaling exhausted sellers. Entry on neckline breakout; target = pattern depth added above neckline.",
                "Inverse Head & Shoulders": "Bullish reversal — three lows with the middle deepest, indicating fading selling pressure. Entry on neckline breakout; target = head-to-neckline distance projected above breakout.",
                "Cup & Handle": "Bullish continuation — rounded base followed by tight consolidation before breakout. Entry on rim breakout; target = cup depth added above rim.",
                "Head & Shoulders": "Bearish reversal — three peaks with the middle highest, signaling exhausted buyers. Entry on neckline breakdown; target = head-to-neckline distance projected below breakdown.",
                "Double Top": "Bearish reversal — resistance tested twice, signaling failed breakout. Entry on neckline breakdown; target = pattern depth subtracted below neckline.",
            }
            plain_str = _plain.get(sel_pat["pattern"], "")

            # Historical outcomes for this pattern type
            outcomes = ind.get("pattern_outcomes", {}).get(sel_pat["pattern"])
            if outcomes:
                pct      = int(outcomes["hit_rate"] * 100)
                pct_col  = "#39d98a" if pct >= 60 else "#fb923c" if pct >= 40 else "#ef4444"
                avg_days = f" · avg {outcomes['avg_bars']}d to target" if outcomes.get("avg_bars") else ""
                hist_str = (f'<span style="color:{pct_col};font-weight:600;">'
                            f'{pct}% hit rate</span>'
                            f'<span style="color:#555577;"> ({outcomes["hits"]}/{outcomes["total"]}'
                            f' past instances reached target{avg_days})</span>')
            else:
                hist_str = ""
            st.markdown(
                f'<div style="margin:4px 0 10px 0;padding:10px 14px;'
                f'background:#0a0a20;border-left:3px solid #6d28d9;border-radius:6px;">'
                # Status + technicals row
                f'<div style="font-size:12px;color:#9090cc;margin-bottom:6px;">'
                f'{conf_str} &nbsp;·&nbsp; {sel_pat["description"]}{mm_str}'
                f'</div>'
                # Plain-English explanation
                f'<div style="font-size:12px;color:#c0c0e0;line-height:1.65;margin-bottom:{"6px" if hist_str else "0"};">'
                f'{plain_str}'
                f'</div>'
                # Historical hit rate (only if available)
                + (f'<div style="font-size:11px;margin-top:4px;">{hist_str}</div>' if hist_str else '')
                + f'</div>',
                unsafe_allow_html=True
            )

    # ── Daily Candlestick Chart ───────────────────────────────────────────────
    fig = go.Figure()

    # Candles — clean green/red
    fig.add_trace(go.Candlestick(
        x=df.index,
        open=df["Open"], high=df["High"],
        low=df["Low"],   close=df["Close"],
        name=ticker,
        increasing_line_color="#22c55e",
        decreasing_line_color="#ef4444",
        increasing_fillcolor="#22c55e",
        decreasing_fillcolor="#ef4444",
    ))

    # MA50 — sky blue (distinct from candles and everything else)
    if ind["ma50"]:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["Close"].rolling(50).mean(),
            line=dict(color="#38bdf8", width=1.4),
            name="MA 50", opacity=0.85,
        ))
    # MA200 — amber/orange (distinct from all)
    if ind["ma200"]:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["Close"].rolling(200).mean(),
            line=dict(color="#fb923c", width=1.4),
            name="MA 200", opacity=0.85,
        ))

    latest_idx = df.index[-1]
    conf = sig["confidence_stars"]

    if sig["signal"] == "BUY":
        fig.add_trace(go.Scatter(
            x=[latest_idx],
            y=[df["Low"].iloc[-1] * 0.984],
            mode="markers+text",
            marker=dict(symbol="triangle-up", size=22, color="#39d98a",
                        line=dict(color="#ffffff", width=1)),
            text=[f" BUY  {stars(conf)}"],
            textposition="bottom center",
            textfont=dict(color="#39d98a", size=12, family="monospace"),
            name="BUY Signal",
            hovertemplate=(
                "<b>▲ BUY Signal</b><br>"
                f"Confidence: {stars(conf)}<br>"
                f"Entry: {sig.get('entry_zone', '—')}<br>"
                f"Stop: {sig.get('stop_loss', '—')}<br>"
                f"Target: {sig.get('target', '—')}"
                "<extra></extra>"
            )
        ))
    elif sig["signal"] == "SELL":
        fig.add_trace(go.Scatter(
            x=[latest_idx],
            y=[df["High"].iloc[-1] * 1.016],
            mode="markers+text",
            marker=dict(symbol="triangle-down", size=22, color="#ff6b6b",
                        line=dict(color="#ffffff", width=1)),
            text=[f" SELL  {stars(conf)}"],
            textposition="top center",
            textfont=dict(color="#ff6b6b", size=12, family="monospace"),
            name="SELL Signal",
            hovertemplate=(
                "<b>▼ SELL Signal</b><br>"
                f"Confidence: {stars(conf)}<br>"
                f"Entry: {sig.get('entry_zone', '—')}<br>"
                f"Stop: {sig.get('stop_loss', '—')}<br>"
                f"Target: {sig.get('target', '—')}"
                "<extra></extra>"
            )
        ))

    # ── Pattern overlay — only the selected pattern, full extent + target ────
    if sel_pat:
        p      = sel_pat
        p_dash = "solid" if p.get("confirmed") else "dash"
        if p.get("bearish"):
            p_color = "#f87171" if p.get("confirmed") else "#fb923c"
            t_color = "rgba(248,113,113,0.45)"
        else:
            p_color = "#e879f9" if p.get("confirmed") else "#a855f7"
            t_color = "rgba(232,121,249,0.45)"

        if p["pattern"] == "Double Bottom":
            # W-shape: low1 → neckline peak → low2
            fig.add_trace(go.Scatter(
                x=[p["low1_x"], p["peak_x"], p["low2_x"]],
                y=[p["low1_y"], p["peak_y"], p["low2_y"]],
                mode="lines",
                line=dict(color=p_color, width=2, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["low1_x"], p["low2_x"]],
                y=[p["low1_y"], p["low2_y"]],
                mode="markers+text",
                marker=dict(symbol="circle-open", size=16, color=p_color,
                            line=dict(color=p_color, width=2.5)),
                text=["W1", "W2"],
                textposition="bottom center",
                textfont=dict(size=9, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            # Forward projection: W2 → expected neckline breakout → measured-move target
            if not p.get("confirmed") and "proj_neckline_x" in p:
                fig.add_trace(go.Scatter(
                    x=[p["low2_x"], p["proj_neckline_x"], p["target_x"]],
                    y=[p["low2_y"], p["neckline"],         p["target_y"]],
                    mode="lines",
                    line=dict(color=t_color, width=1.5, dash="dot"),
                    showlegend=False, hoverinfo="skip",
                ))
            # Neckline — spans full pattern width to today
            fig.add_trace(go.Scatter(
                x=[p["neckline_x0"], p["neckline_x1"]],
                y=[p["neckline"], p["neckline"]],
                mode="lines+text",
                line=dict(color=p_color, width=2, dash=p_dash),
                text=["", f"Neckline ${p['neckline']}"],
                textposition="top right",
                textfont=dict(size=10, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            # Measured-move target marker
            fig.add_trace(go.Scatter(
                x=[p["target_x"]],
                y=[p["target_y"]],
                mode="markers+text",
                marker=dict(symbol="diamond", size=10, color=t_color),
                text=[f"Target ${p['target_y']}"],
                textposition="top right",
                textfont=dict(size=10, color=t_color),
                showlegend=False, hoverinfo="skip",
            ))

        elif p["pattern"] == "Inverse Head & Shoulders":
            # Full pattern: LS → LP → Head → RP → RS
            fig.add_trace(go.Scatter(
                x=[p["ls_x"], p["lp_x"], p["hd_x"], p["rp_x"], p["rs_x"]],
                y=[p["ls_y"], p["lp_y"], p["hd_y"], p["rp_y"], p["rs_y"]],
                mode="lines",
                line=dict(color=p_color, width=2, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["ls_x"], p["hd_x"], p["rs_x"]],
                y=[p["ls_y"], p["hd_y"], p["rs_y"]],
                mode="markers+text",
                marker=dict(symbol="circle-open", size=16, color=p_color,
                            line=dict(color=p_color, width=2.5)),
                text=["LS", "H", "RS"],
                textposition="bottom center",
                textfont=dict(size=9, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            # Forward projection: RS → expected neckline breakout → target
            if not p.get("confirmed") and "proj_neckline_x" in p:
                fig.add_trace(go.Scatter(
                    x=[p["rs_x"], p["proj_neckline_x"], p["target_x"]],
                    y=[p["rs_y"], p["neckline"],         p["target_y"]],
                    mode="lines",
                    line=dict(color=t_color, width=1.5, dash="dot"),
                    showlegend=False, hoverinfo="skip",
                ))
            # Neckline — spans full pattern width to today
            fig.add_trace(go.Scatter(
                x=[p["neckline_x0"], p["neckline_x1"]],
                y=[p["neckline"], p["neckline"]],
                mode="lines+text",
                line=dict(color=p_color, width=2, dash=p_dash),
                text=["", f"Neckline ${p['neckline']}"],
                textposition="top right",
                textfont=dict(size=10, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            # Measured-move target marker
            fig.add_trace(go.Scatter(
                x=[p["target_x"]],
                y=[p["target_y"]],
                mode="markers+text",
                marker=dict(symbol="diamond", size=10, color=t_color),
                text=[f"Target ${p['target_y']}"],
                textposition="top right",
                textfont=dict(size=10, color=t_color),
                showlegend=False, hoverinfo="skip",
            ))

        elif p["pattern"] == "Cup & Handle":
            # U-shape: left rim → cup → right rim
            fig.add_trace(go.Scatter(
                x=[p["lr_x"], p["cup_x"], p["rr_x"]],
                y=[p["lr_y"], p["cup_y"], p["rr_y"]],
                mode="lines",
                line=dict(color=p_color, width=2, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["lr_x"], p["cup_x"], p["rr_x"]],
                y=[p["lr_y"], p["cup_y"], p["rr_y"]],
                mode="markers+text",
                marker=dict(symbol="circle-open", size=16, color=p_color,
                            line=dict(color=p_color, width=2.5)),
                text=["L", "C", "R"],
                textposition="bottom center",
                textfont=dict(size=9, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            # Forward projection: RR → handle consolidation → breakout → target
            if not p.get("confirmed") and "proj_neckline_x" in p:
                fig.add_trace(go.Scatter(
                    x=[p["rr_x"], p["proj_neckline_x"], p["target_x"]],
                    y=[p["rr_y"], p["neckline"],          p["target_y"]],
                    mode="lines",
                    line=dict(color=t_color, width=1.5, dash="dot"),
                    showlegend=False, hoverinfo="skip",
                ))
            # Breakout line — left rim to today
            fig.add_trace(go.Scatter(
                x=[p["neckline_x0"], p["neckline_x1"]],
                y=[p["neckline"], p["neckline"]],
                mode="lines+text",
                line=dict(color=p_color, width=2, dash=p_dash),
                text=["", f"Breakout ${p['neckline']}"],
                textposition="top right",
                textfont=dict(size=10, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            # Measured-move target marker
            fig.add_trace(go.Scatter(
                x=[p["target_x"]],
                y=[p["target_y"]],
                mode="markers+text",
                marker=dict(symbol="diamond", size=10, color=t_color),
                text=[f"Target ${p['target_y']}"],
                textposition="top right",
                textfont=dict(size=10, color=t_color),
                showlegend=False, hoverinfo="skip",
            ))

        elif p["pattern"] == "Head & Shoulders":
            # M-shape: LS → left trough → HD → right trough → RS
            fig.add_trace(go.Scatter(
                x=[p["ls_x"], p["lt_x"], p["hd_x"], p["rt_x"], p["rs_x"]],
                y=[p["ls_y"], p["lt_y"], p["hd_y"], p["rt_y"], p["rs_y"]],
                mode="lines",
                line=dict(color=p_color, width=2, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["ls_x"], p["hd_x"], p["rs_x"]],
                y=[p["ls_y"], p["hd_y"], p["rs_y"]],
                mode="markers+text",
                marker=dict(symbol="circle-open", size=16, color=p_color,
                            line=dict(color=p_color, width=2.5)),
                text=["LS", "H", "RS"],
                textposition="top center",
                textfont=dict(size=9, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            if not p.get("confirmed") and "proj_neckline_x" in p:
                fig.add_trace(go.Scatter(
                    x=[p["rs_x"], p["proj_neckline_x"], p["target_x"]],
                    y=[p["rs_y"], p["neckline"],          p["target_y"]],
                    mode="lines",
                    line=dict(color=t_color, width=1.5, dash="dot"),
                    showlegend=False, hoverinfo="skip",
                ))
            fig.add_trace(go.Scatter(
                x=[p["neckline_x0"], p["neckline_x1"]],
                y=[p["neckline"], p["neckline"]],
                mode="lines+text",
                line=dict(color=p_color, width=2, dash=p_dash),
                text=["", f"Neckline ${p['neckline']}"],
                textposition="bottom right",
                textfont=dict(size=10, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["target_x"]],
                y=[p["target_y"]],
                mode="markers+text",
                marker=dict(symbol="diamond", size=10, color=t_color),
                text=[f"Target ${p['target_y']}"],
                textposition="bottom right",
                textfont=dict(size=10, color=t_color),
                showlegend=False, hoverinfo="skip",
            ))

        elif p["pattern"] == "Double Top":
            # M-shape: H1 → trough → H2
            fig.add_trace(go.Scatter(
                x=[p["high1_x"], p["trough_x"], p["high2_x"]],
                y=[p["high1_y"], p["trough_y"], p["high2_y"]],
                mode="lines",
                line=dict(color=p_color, width=2, dash="dot"),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["high1_x"], p["high2_x"]],
                y=[p["high1_y"], p["high2_y"]],
                mode="markers+text",
                marker=dict(symbol="circle-open", size=16, color=p_color,
                            line=dict(color=p_color, width=2.5)),
                text=["M1", "M2"],
                textposition="top center",
                textfont=dict(size=9, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            if not p.get("confirmed") and "proj_neckline_x" in p:
                fig.add_trace(go.Scatter(
                    x=[p["high2_x"], p["proj_neckline_x"], p["target_x"]],
                    y=[p["high2_y"], p["neckline"],          p["target_y"]],
                    mode="lines",
                    line=dict(color=t_color, width=1.5, dash="dot"),
                    showlegend=False, hoverinfo="skip",
                ))
            fig.add_trace(go.Scatter(
                x=[p["neckline_x0"], p["neckline_x1"]],
                y=[p["neckline"], p["neckline"]],
                mode="lines+text",
                line=dict(color=p_color, width=2, dash=p_dash),
                text=["", f"Neckline ${p['neckline']}"],
                textposition="bottom right",
                textfont=dict(size=10, color=p_color),
                showlegend=False, hoverinfo="skip",
            ))
            fig.add_trace(go.Scatter(
                x=[p["target_x"]],
                y=[p["target_y"]],
                mode="markers+text",
                marker=dict(symbol="diamond", size=10, color=t_color),
                text=[f"Target ${p['target_y']}"],
                textposition="bottom right",
                textfont=dict(size=10, color=t_color),
                showlegend=False, hoverinfo="skip",
            ))

    # ── Support & Resistance lines — teal / rose ─────────────────────────────
    for level in ind.get("sr_levels", []):
        is_support = level["type"] == "support"
        line_color = "rgba(45,212,191,0.50)" if is_support else "rgba(251,113,133,0.50)"
        label      = f"{'S' if is_support else 'R'} ${level['price']}"
        fig.add_shape(
            type="line",
            xref="paper", x0=0, x1=1,
            yref="y", y0=level["price"], y1=level["price"],
            line=dict(color=line_color, width=1, dash="dot"),
        )
        fig.add_annotation(
            xref="paper", x=1.01,
            yref="y", y=level["price"],
            text=label,
            showarrow=False,
            font=dict(size=9, color=line_color, family="Exo 2"),
            xanchor="left",
        )

    # Extend x-axis to show the measured-move target when a pattern is selected
    x_range_end = (
        sel_pat["target_x"].strftime("%Y-%m-%d") if sel_pat and "target_x" in sel_pat
        else df.index[-1].strftime("%Y-%m-%d")
    )

    fig.update_layout(
        height=520,
        template="plotly_dark",
        paper_bgcolor="#03030e",
        plot_bgcolor="#04040c",
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=80, t=36, b=8),
        legend=dict(
            orientation="h", y=1.04, x=0,
            font=dict(size=11, color="#4a4a77"),
            bgcolor="rgba(0,0,0,0)",
            itemsizing="constant",
            traceorder="normal",
        ),
        hovermode="closest",
        hoverlabel=dict(
            bgcolor="#0d0d22",
            bordercolor="#22224a",
            font=dict(color="#c0c0ee", size=12, family="Exo 2"),
        ),
        xaxis=dict(
            type="date",
            range=[df.index[0].strftime("%Y-%m-%d"), x_range_end],
            rangebreaks=[dict(bounds=["sat", "mon"])],  # hide weekends
            dtick="M1",
            tickformat="%b '%y",
            showgrid=False,
            showline=False,
            zeroline=False,
            tickfont=dict(color="#333355", size=10, family="Exo 2"),
            ticklen=0,
        ),
        yaxis=dict(
            side="right",                      # price axis on the right like Robinhood
            showgrid=True,
            gridcolor="#09091a",
            gridwidth=1,
            zeroline=False,
            showline=False,
            tickprefix="$",
            tickfont=dict(color="#333355", size=10, family="Exo 2"),
            ticklen=0,
        ),
    )

    st.plotly_chart(fig, use_container_width=True)

    # ── Signal History ────────────────────────────────────────────────────────
    history = get_history(ticker)
    if history:
        sig_colors = {"BUY": "#39d98a", "SELL": "#ff6b6b"}
        st.markdown(
            f'<div class="panel" style="margin-bottom:6px;">'
            f'<div class="panel-title">📜 &nbsp; Signal History &nbsp;·&nbsp; {ticker}</div>'
            f'</div>',
            unsafe_allow_html=True
        )
        for h in history:
            color     = sig_colors.get(h["signal"], "#c0c0ff")
            sig_label = h["signal"]
            conf_str  = stars(h["confidence"] or 0)
            price_str = f'${h["price"]:.2f}' if h.get("price") else "—"

            # P&L summary for closed trades
            pnl_str = ""
            if h.get("exit_price") and h.get("price"):
                pnl      = (h["exit_price"] - h["price"]) / h["price"] * 100
                pnl_sign = "+" if pnl >= 0 else ""
                pnl_str  = f'  ·  {pnl_sign}{pnl:.1f}%'

            # Expander label: date · signal · stars · price · P&L
            label = f'{h["date"]}  ·  {sig_label}  ·  {conf_str}  ·  {price_str}{pnl_str}'

            with st.expander(label):
                # Trade levels
                c1, c2, c3 = st.columns(3)
                c1.metric("Entry Zone",  h.get("entry_zone") or "—")
                c2.metric("Stop Loss",   h.get("stop_loss")  or "—")
                c3.metric("Target",      h.get("target")     or "—")

                # Exit / P&L row (only if closed)
                if h.get("exit_price") and h.get("price"):
                    pnl      = (h["exit_price"] - h["price"]) / h["price"] * 100
                    pnl_sign = "+" if pnl >= 0 else ""
                    e1, e2, _ = st.columns(3)
                    e1.metric("Exit Price", f'${h["exit_price"]:.2f}',
                              delta=f'{pnl_sign}{pnl:.1f}%')
                    e2.metric("Exit Date",  h.get("exit_date") or "—")

                # AI rationale
                if h.get("rationale"):
                    st.markdown(
                        f'<div style="margin-top:10px;padding:10px 14px;'
                        f'background:#07071a;border-left:3px solid {color};'
                        f'border-radius:6px;font-size:12px;color:#9090cc;line-height:1.6;">'
                        f'{h["rationale"]}'
                        f'</div>',
                        unsafe_allow_html=True
                    )

    # ── Enter Position button ──────────────────────────────────────────────────
    _already_in_pos = any(p["ticker"] == ticker for p in (get_my_open_positions(user_id) or []))
    ep_col, _ = st.columns([1, 2])
    with ep_col:
        if _already_in_pos:
            st.button("✅  In Positions", key=f"in_pos_{ticker}", use_container_width=True, disabled=True)
        else:
            if st.button("📈  Enter Position", key=f"enter_pos_{ticker}", use_container_width=True):
                _enter_position_manual_dialog(
                    ticker=ticker,
                    default_price=float(ind["latest_close"]),
                    user_id=user_id,
                )

    st.markdown(
        '<hr style="border:none;height:1px;background:linear-gradient('
        '90deg,transparent,#1a1a55,#3322aa,#1a1a55,transparent);margin:28px 0;">',
        unsafe_allow_html=True
    )
