"""
chart_analyzer/charts.py — Annotated Plotly thumbnail charts for each pattern setup.

build_pattern_chart(ticker, df, setup) -> go.Figure
  - OHLC candlestick base (last 90 bars)
  - Pattern-specific overlays: trendlines, key-level markers, shading
  - Compact layout suitable for a two-column Streamlit grid
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ── Colour palette ────────────────────────────────────────────────────────────
_BG        = "#0a0a1e"
_PAPER     = "#0d0d22"
_GRID      = "rgba(40,40,80,0.4)"
_UP        = "#26a69a"
_DOWN      = "#ef5350"
_TEXT      = "#c8c8ff"
_NECKLINE  = "#f9c846"    # yellow
_SUPPORT   = "#40e0ff"    # cyan
_RESIST    = "#ff6b6b"    # red/coral
_CONFIRM   = "#00e676"    # green
_APPROACH  = "#ffd54f"    # amber
_WEDGE_UP  = "#b388ff"    # purple — upper wedge trendline
_WEDGE_LO  = "#80cbc4"    # teal   — lower wedge trendline
_SHADE     = "rgba(100,80,220,0.10)"
_STAGE_COL = {
    "CONFIRMED":  _CONFIRM,
    "APPROACHING": _APPROACH,
    "WATCHING":   "#7e57c2",
    "DETECTED":   "#546e7a",
}

_WINDOW = 90   # bars shown in thumbnail


def _base_fig(df: pd.DataFrame) -> go.Figure:
    """Candlestick base chart, last _WINDOW bars."""
    df = df.tail(_WINDOW).copy().reset_index()
    dates = df["Date"] if "Date" in df.columns else df.index

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.78, 0.22],
        vertical_spacing=0.02,
    )

    # Candles
    fig.add_trace(go.Candlestick(
        x=dates,
        open=df["Open"], high=df["High"],
        low=df["Low"],  close=df["Close"],
        increasing_line_color=_UP,  decreasing_line_color=_DOWN,
        increasing_fillcolor=_UP,   decreasing_fillcolor=_DOWN,
        line_width=1,
        showlegend=False,
        name="Price",
    ), row=1, col=1)

    # Volume bars
    vol_colors = [
        _UP if df["Close"].iloc[i] >= df["Open"].iloc[i] else _DOWN
        for i in range(len(df))
    ]
    fig.add_trace(go.Bar(
        x=dates,
        y=df["Volume"],
        marker_color=vol_colors,
        marker_opacity=0.5,
        showlegend=False,
        name="Volume",
    ), row=2, col=1)

    fig.update_layout(
        paper_bgcolor=_PAPER,
        plot_bgcolor=_BG,
        margin=dict(l=4, r=4, t=32, b=4),
        height=320,
        xaxis_rangeslider_visible=False,
        font=dict(color=_TEXT, size=10),
        showlegend=False,
    )
    for axis in ("xaxis", "xaxis2", "yaxis", "yaxis2"):
        fig.update_layout(**{axis: dict(
            gridcolor=_GRID,
            zerolinecolor=_GRID,
            showgrid=True,
        )})

    return fig, df, dates


def _hline(fig, y: float, color: str, dash: str = "dash",
           label: str = "", row: int = 1):
    fig.add_hline(
        y=y, line_color=color, line_dash=dash,
        line_width=1.2,
        annotation_text=label,
        annotation_font_color=color,
        annotation_font_size=9,
        row=row, col=1,
    )


def _trendline(fig, x_start, x_end, y_start: float, y_end: float,
               color: str, dash: str = "solid", width: float = 1.2):
    fig.add_shape(dict(
        type="line",
        x0=x_start, x1=x_end,
        y0=y_start, y1=y_end,
        line=dict(color=color, width=width, dash=dash),
        xref="x", yref="y",
        row=1, col=1,
    ))


def _scatter_marker(fig, x, y: float, color: str, symbol: str = "circle",
                    size: int = 8, label: str = ""):
    fig.add_trace(go.Scatter(
        x=[x], y=[y],
        mode="markers+text" if label else "markers",
        marker=dict(color=color, size=size, symbol=symbol,
                    line=dict(color="#ffffff", width=1)),
        text=[label] if label else None,
        textposition="top center",
        textfont=dict(color=color, size=9),
        showlegend=False,
    ), row=1, col=1)


# ── Per-pattern annotation logic ──────────────────────────────────────────────

def _annotate_inv_hns(fig, df: pd.DataFrame, dates, key_levels: dict):
    neckline = key_levels.get("neckline")
    ls_low   = key_levels.get("ls_low")
    head_low = key_levels.get("head_low")
    rs_low   = key_levels.get("rs_low")

    if neckline:
        _hline(fig, neckline, _NECKLINE, label=f"Neckline {neckline:.2f}")

    # Mark the three lows on the chart by locating bars closest to their values
    lows = df["Low"].values
    for low_val, sym, col, lbl in [
        (ls_low, "circle", _SUPPORT, "LS"),
        (head_low, "triangle-down", _DOWN, "H"),
        (rs_low, "circle", _SUPPORT, "RS"),
    ]:
        if low_val is None:
            continue
        # Find bar closest in value
        idx = int(np.argmin(np.abs(lows - low_val)))
        _scatter_marker(fig, dates.iloc[idx], lows[idx], col, sym, 9, lbl)


def _annotate_asc_triangle(fig, df: pd.DataFrame, dates, key_levels: dict):
    resistance = key_levels.get("resistance")
    last_low   = key_levels.get("last_rising_low")
    first_low  = key_levels.get("first_rising_low")

    if resistance:
        _hline(fig, resistance, _RESIST, label=f"Resistance {resistance:.2f}")

    # Rising lows trendline — approximate with first and last date
    if first_low and last_low and first_low != last_low:
        _trendline(fig, dates.iloc[0], dates.iloc[-1], first_low, last_low,
                   _SUPPORT, dash="dot")


def _annotate_cup_handle(fig, df: pd.DataFrame, dates, key_levels: dict):
    left_rim   = key_levels.get("left_rim")
    cup_bottom = key_levels.get("cup_bottom")
    right_rim  = key_levels.get("right_rim")
    handle_low = key_levels.get("handle_low")

    if left_rim:
        _hline(fig, left_rim, _RESIST, dash="dot", label=f"L.Rim {left_rim:.2f}")
    if right_rim:
        _hline(fig, right_rim, _CONFIRM, label=f"R.Rim {right_rim:.2f}")
    if cup_bottom:
        # Mark cup bottom
        lows = df["Low"].values
        idx = int(np.argmin(np.abs(lows - cup_bottom)))
        _scatter_marker(fig, dates.iloc[idx], cup_bottom, _DOWN, "triangle-down", 9, "Cup")
    if handle_low:
        _hline(fig, handle_low, _APPROACH, dash="dot", label=f"Handle {handle_low:.2f}")


def _annotate_bull_flag(fig, df: pd.DataFrame, dates, key_levels: dict):
    pole_low  = key_levels.get("pole_low")
    pole_high = key_levels.get("pole_high")
    flag_high = key_levels.get("flag_high")
    flag_low  = key_levels.get("flag_low")

    if flag_high:
        _hline(fig, flag_high, _CONFIRM, label=f"Flag High {flag_high:.2f}")
    if flag_low:
        _hline(fig, flag_low, _RESIST, dash="dot", label=f"Flag Low {flag_low:.2f}")

    # Shade the flag region on the volume pane
    if flag_high and flag_low:
        closes = df["Close"].values
        # Find approximate pole end (closest to pole_high)
        if pole_high:
            pole_idx = int(np.argmin(np.abs(closes - pole_high)))
            fig.add_vrect(
                x0=dates.iloc[max(0, pole_idx)],
                x1=dates.iloc[-1],
                fillcolor=_SHADE,
                line_width=0,
                row=1, col=1,
            )


def _annotate_falling_wedge(fig, df: pd.DataFrame, dates, key_levels: dict):
    upper_tl  = key_levels.get("upper_tl_now")
    wedge_top = key_levels.get("wedge_top")

    n = len(dates)
    if upper_tl and wedge_top and n > 1:
        # Upper trendline: from wedge_top at bar 0 to upper_tl at last bar
        _trendline(fig, dates.iloc[0], dates.iloc[-1], wedge_top, upper_tl,
                   _WEDGE_UP, width=1.5)

    # Lower trendline: approximate from price action
    lows = df["Low"].values
    if n > 10:
        # Simple linear fit to the lower quartile of lows
        x = np.arange(n, dtype=float)
        lo_fit = np.polyfit(x, lows, 1)
        y_start = float(np.polyval(lo_fit, 0))
        y_end   = float(np.polyval(lo_fit, n - 1))
        _trendline(fig, dates.iloc[0], dates.iloc[-1], y_start, y_end,
                   _WEDGE_LO, dash="dot", width=1.0)


# ── Annotator dispatch ────────────────────────────────────────────────────────

_ANNOTATORS = {
    "InvHnS":      _annotate_inv_hns,
    "AscTriangle": _annotate_asc_triangle,
    "CupHandle":   _annotate_cup_handle,
    "BullFlag":    _annotate_bull_flag,
    "FallingWedge":_annotate_falling_wedge,
}

_PATTERN_LABELS = {
    "InvHnS":      "Inv Head & Shoulders",
    "AscTriangle": "Ascending Triangle",
    "CupHandle":   "Cup & Handle",
    "BullFlag":    "Bull Flag",
    "FallingWedge":"Falling Wedge",
}


# ── Public API ────────────────────────────────────────────────────────────────

def build_pattern_chart(
    ticker: str,
    df: pd.DataFrame,
    setup: dict,
) -> go.Figure:
    """
    Build an annotated Plotly candlestick thumbnail for a pattern setup.

    Parameters
    ----------
    ticker  : e.g. "AAPL"
    df      : OHLCV DataFrame with DatetimeIndex
    setup   : dict from chart_analyzer DB (pattern_type, stage, key_levels, stop_price, target_price)

    Returns
    -------
    go.Figure
    """
    df = df.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df = df.reset_index()
    if "Date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "Date"})

    pattern_type = setup.get("pattern_type", "")
    stage        = setup.get("stage", "DETECTED")
    key_levels   = setup.get("key_levels") or {}
    stop         = setup.get("stop_price")
    target       = setup.get("target_price")
    label        = _PATTERN_LABELS.get(pattern_type, pattern_type)
    stage_color  = _STAGE_COL.get(stage, "#555588")

    fig, df_trim, dates = _base_fig(df)

    # Stop and target levels
    if stop:
        fig.add_hline(
            y=stop, line_color="#ff5252", line_dash="longdash",
            line_width=1, annotation_text=f"Stop {stop:.2f}",
            annotation_font_color="#ff5252", annotation_font_size=8,
            row=1, col=1,
        )
    if target:
        fig.add_hline(
            y=target, line_color="#69f0ae", line_dash="longdash",
            line_width=1, annotation_text=f"Target {target:.2f}",
            annotation_font_color="#69f0ae", annotation_font_size=8,
            row=1, col=1,
        )

    # Pattern-specific annotations
    annotate_fn = _ANNOTATORS.get(pattern_type)
    if annotate_fn:
        annotate_fn(fig, df_trim, dates, key_levels)

    # Chart title with stage badge
    fig.update_layout(
        title=dict(
            text=f"<b>{ticker}</b>  <span style='color:{stage_color}'>{stage}</span>"
                 f"  <span style='color:#555588;font-size:10px'>{label}</span>",
            font=dict(size=13, color=_TEXT),
            x=0.01, xanchor="left",
        ),
    )

    return fig


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_ticker_df(ticker: str) -> pd.DataFrame | None:
    """Fetch 6 months OHLCV for a ticker, cached 5 minutes."""
    import yfinance as yf
    try:
        df = yf.download(ticker, period="6mo", auto_adjust=True, progress=False)
        return df if not df.empty else None
    except Exception:
        return None


def build_setup_card_figure(setup: dict) -> go.Figure | None:
    """
    Convenience wrapper: fetches fresh price data for setup["ticker"] and
    calls build_pattern_chart().  Returns None if data unavailable.
    """
    import streamlit as st
    ticker = setup.get("ticker", "")
    df = _fetch_ticker_df(ticker)
    if df is None or df.empty:
        return None
    return build_pattern_chart(ticker, df, setup)
