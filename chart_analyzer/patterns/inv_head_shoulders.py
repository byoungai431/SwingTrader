"""
Inverse Head & Shoulders detector (live-scan mode).

Proximity threshold  : 1.5% below neckline → APPROACHING
Regime gate          : none (reversal pattern)
Volume on breakout   : ≥ 1.2× 20-bar average
Stop                 : 2% below right-shoulder low
Target               : neckline + (neckline − head low)
"""

from __future__ import annotations


import numpy as np
from chart_analyzer.patterns.base import BasePattern, DETECTED, WATCHING, APPROACHING, CONFIRMED, CONFIRM_LOOKBACK


class InvHeadShoulders(BasePattern):
    PATTERN_TYPE   = "InvHnS"
    WINDOW         = 150
    APPROACHING_PCT = 0.015   # 1.5% below neckline triggers APPROACHING
    _ORDER         = 8        # argrelextrema sensitivity

    def scan(self, ticker: str, df, spy_above_ma50: bool = True) -> dict | None:
        df = self._trim(df)
        if len(df) < 50:
            return None

        c, h, lo, v, vma = self._arrays(df)
        n = len(c)

        local_mins = self._local_mins(lo, order=self._ORDER)
        if len(local_mins) < 3:
            return None

        # Iterate triplets from most recent backward; return first valid setup
        for i in range(len(local_mins) - 3, -1, -1):
            ls_idx = local_mins[i]
            h_idx  = local_mins[i + 1]
            rs_idx = local_mins[i + 2]

            ls_low = lo[ls_idx]
            h_low  = lo[h_idx]
            rs_low = lo[rs_idx]

            # Head must be the deepest
            if not (h_low < ls_low and h_low < rs_low):
                continue

            # Shoulders within 5% of each other
            if abs(ls_low - rs_low) / ls_low > 0.05:
                continue

            # Neckline = average of the two inter-swing peaks
            seg1 = h[ls_idx : h_idx]
            seg2 = h[h_idx  : rs_idx]
            if len(seg1) == 0 or len(seg2) == 0:
                continue
            peak1 = float(np.max(seg1))
            peak2 = float(np.max(seg2))
            neckline = (peak1 + peak2) / 2.0

            stop   = rs_low * 0.98
            target = neckline + (neckline - h_low)

            key_levels = {
                "neckline":  neckline,
                "ls_low":    ls_low,
                "head_low":  h_low,
                "rs_low":    rs_low,
            }
            chart_bars = rs_idx - ls_idx

            current_close = float(c[-1])
            current_vol   = float(v[-1])
            current_vma   = float(vma[-1]) if not np.isnan(vma[-1]) else 0.0

            # Check for recent confirmed breakout (within CONFIRM_LOOKBACK bars after RS)
            for bar in range(rs_idx + 1, n):
                bar_vol = float(v[bar])
                bar_vma = float(vma[bar]) if not np.isnan(vma[bar]) else 0.0
                if c[bar] > neckline and bar_vma > 0 and bar_vol >= bar_vma * 1.2:
                    # Confirmed breakout found
                    # Still active if price hasn't fallen through stop
                    if current_close > stop:
                        vr = self._vol_ratio(bar_vol, bar_vma)
                        return self._make_setup(
                            ticker, CONFIRMED, key_levels, stop, target,
                            chart_bars, entry=float(c[bar]), vol_ratio=vr,
                            notes=f"Neckline={neckline:.2f} Head={h_low:.2f}"
                        )
                    else:
                        # Stop was hit after breakout — pattern exhausted
                        return None

            # No confirmed breakout yet
            if current_close >= neckline * (1 - self.APPROACHING_PCT):
                stage = APPROACHING
            elif rs_idx < n - 3:
                # RS formed, neckline drawn — watching for approach
                stage = WATCHING
            else:
                stage = DETECTED

            return self._make_setup(
                ticker, stage, key_levels, stop, target, chart_bars,
                notes=f"Neckline={neckline:.2f} Head={h_low:.2f}"
            )

        return None
