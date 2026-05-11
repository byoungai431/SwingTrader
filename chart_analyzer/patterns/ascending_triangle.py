"""
Ascending Triangle detector (live-scan mode).

Proximity threshold  : 1.5% below flat resistance → APPROACHING
Regime gate          : SPY > 50-day MA (continuation pattern)
Volume on breakout   : ≥ 1.3× 20-bar average
Stop                 : 1% below last rising trendline low
Target               : resistance + (resistance − first rising low)
"""

from __future__ import annotations


import numpy as np
import pandas as pd
from chart_analyzer.patterns.base import BasePattern, DETECTED, WATCHING, APPROACHING, CONFIRMED


class AscendingTriangle(BasePattern):
    PATTERN_TYPE    = "AscTriangle"
    WINDOW          = 120
    APPROACHING_PCT = 0.015   # 1.5% below resistance
    _RES_TOL        = 0.025   # resistance highs within 2.5%
    _MIN_TOUCHES    = 3

    def scan(self, ticker: str, df, spy_above_ma50: bool = True) -> dict | None:
        df = self._trim(df)
        if len(df) < 40:
            return None

        c, h, lo, v, vma = self._arrays(df)
        n = len(c)

        local_highs = self._local_maxs(h, order=5)
        local_lows  = self._local_mins(lo, order=5)

        if len(local_highs) < self._MIN_TOUCHES or len(local_lows) < self._MIN_TOUCHES:
            return None

        # Use last N touches
        lh = local_highs[-self._MIN_TOUCHES:]
        ll = local_lows[-self._MIN_TOUCHES:]

        h_vals = h[lh]
        l_vals = lo[ll]

        resistance = float(np.max(h_vals))

        # Flat resistance: all highs within 2.5% of the top
        if (resistance - float(np.min(h_vals))) / resistance > self._RES_TOL:
            return None

        # Rising lows: each successive low must be higher
        if not all(l_vals[i] < l_vals[i + 1] for i in range(len(l_vals) - 1)):
            return None

        stop   = float(l_vals[-1]) * 0.99
        target = resistance + (resistance - float(l_vals[0]))
        chart_bars = int(lh[-1] - ll[0])

        key_levels = {
            "resistance":     resistance,
            "last_rising_low": float(l_vals[-1]),
            "first_rising_low": float(l_vals[0]),
        }

        current_close = float(c[-1])
        current_vol   = float(v[-1])
        current_vma   = float(vma[-1]) if not np.isnan(vma[-1]) else 0.0

        # Confirmed: recent breakout above resistance with vol
        if current_close > resistance and current_vma > 0 and current_vol >= current_vma * 1.3:
            if spy_above_ma50 and current_close > stop:
                vr = self._vol_ratio(current_vol, current_vma)
                return self._make_setup(
                    ticker, CONFIRMED, key_levels, stop, target, chart_bars,
                    entry=current_close, vol_ratio=vr,
                    notes=f"Resistance={resistance:.2f}"
                )
            return None  # regime gate failed

        # Approaching: within 1.5% of resistance, below it
        if current_close >= resistance * (1 - self.APPROACHING_PCT) and spy_above_ma50:
            return self._make_setup(
                ticker, APPROACHING, key_levels, stop, target, chart_bars,
                notes=f"Resistance={resistance:.2f}"
            )

        # Watching: structure valid, price not near trigger yet
        stage = WATCHING if len(local_highs) >= self._MIN_TOUCHES else DETECTED
        return self._make_setup(
            ticker, stage, key_levels, stop, target, chart_bars,
            notes=f"Resistance={resistance:.2f}"
        )
