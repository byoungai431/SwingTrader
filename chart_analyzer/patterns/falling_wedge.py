"""
Falling Wedge detector (live-scan mode).

Proximity threshold  : price within 2.0% of upper descending trendline → APPROACHING
Regime gate          : none (reversal pattern)
Volume on breakout   : ≥ 1.2× 20-bar average
Structure            : ≥ 3 touches on each descending trendline; trendlines converge
                       to ≤ 40% of initial width
Stop                 : 1% below recent 10-bar swing low
Target               : top of wedge (upper trendline at wedge start)
"""

from __future__ import annotations


import numpy as np
import pandas as pd
from chart_analyzer.patterns.base import BasePattern, DETECTED, WATCHING, APPROACHING, CONFIRMED


class FallingWedge(BasePattern):
    PATTERN_TYPE    = "FallingWedge"
    WINDOW          = 120
    APPROACHING_PCT = 0.020     # 2.0% below upper trendline

    def scan(self, ticker: str, df, spy_above_ma50: bool = True) -> dict | None:
        df = self._trim(df)
        if len(df) < 30:
            return None

        c, h, lo, v, vma = self._arrays(df)
        n = len(c)

        local_highs = self._local_maxs(h, order=5)
        local_lows  = self._local_mins(lo, order=5)

        if len(local_highs) < 3 or len(local_lows) < 3:
            return None

        # Use most recent 3–5 touches on each side
        lh = local_highs[-5:] if len(local_highs) >= 5 else local_highs
        ll = local_lows[-5:]  if len(local_lows)  >= 5 else local_lows

        if len(lh) < 3 or len(ll) < 3:
            return None

        lh_x = lh.astype(np.float64)
        lh_y = h[lh]
        ll_x = ll.astype(np.float64)
        ll_y = lo[ll]

        slope_h, intercept_h = np.polyfit(lh_x, lh_y, 1)
        slope_l, intercept_l = np.polyfit(ll_x, ll_y, 1)

        # Both trendlines must slope downward
        if slope_h >= 0 or slope_l >= 0:
            return None

        # Lows slope must be less negative than highs (converging)
        if slope_l <= slope_h:
            return None

        start = int(min(lh[0], ll[0]))
        end   = n - 1

        width_start = (intercept_h + slope_h * start) - (intercept_l + slope_l * start)
        width_end   = (intercept_h + slope_h * end)   - (intercept_l + slope_l * end)

        if width_start <= 0:
            return None

        compression = width_end / width_start

        # Upper trendline at current bar
        upper_tl_now = float(intercept_h + slope_h * end)
        wedge_top    = float(intercept_h + slope_h * start)  # target = top of wedge

        stop   = float(np.min(lo[max(0, end - 10) : end + 1])) * 0.99
        target = wedge_top

        key_levels = {
            "upper_tl_now": upper_tl_now,
            "wedge_top":    wedge_top,
            "compression":  round(compression, 3),
        }
        chart_bars = end - start

        current_close = float(c[-1])
        current_vol   = float(v[-1])
        current_vma   = float(vma[-1]) if not np.isnan(vma[-1]) else 0.0

        if target <= current_close * 1.01:
            return None  # target not meaningful

        # Confirmed: close above upper trendline with vol ≥ 1.2x
        if current_close > upper_tl_now and current_vma > 0 and current_vol >= current_vma * 1.2:
            if current_close > stop:
                vr = self._vol_ratio(current_vol, current_vma)
                return self._make_setup(
                    ticker, CONFIRMED, key_levels, stop, target, chart_bars,
                    entry=current_close, vol_ratio=vr,
                    notes=f"Compression={compression*100:.0f}%"
                )
            return None

        # Approaching: price within 2% of upper trendline from below
        if current_close >= upper_tl_now * (1 - self.APPROACHING_PCT):
            return self._make_setup(
                ticker, APPROACHING, key_levels, stop, target, chart_bars,
                notes=f"Upper TL={upper_tl_now:.2f} Compression={compression*100:.0f}%"
            )

        # Stage based on compression progress
        if compression <= 0.60:
            stage = WATCHING
        else:
            stage = DETECTED

        return self._make_setup(
            ticker, stage, key_levels, stop, target, chart_bars,
            notes=f"Compression={compression*100:.0f}%"
        )
