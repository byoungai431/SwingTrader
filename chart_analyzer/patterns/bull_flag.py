"""
Bull Flag detector (live-scan mode).

Proximity threshold  : flag complete + price within 1.0% of flag high → APPROACHING
Regime gate          : SPY > 50-day MA (continuation pattern)
Pole                 : ≥ 10% gain in ≤ 8 bars
Flag                 : 3–8 bars, range ≤ 5%, volume dried ≥ 40% vs pole
Volume on breakout   : ≥ 1.3× 20-bar average
Stop                 : 1% below flag low
Target               : pole length × 2 measured from pole low
"""

from __future__ import annotations


import numpy as np
import pandas as pd
from chart_analyzer.patterns.base import BasePattern, DETECTED, WATCHING, APPROACHING, CONFIRMED


class BullFlag(BasePattern):
    PATTERN_TYPE    = "BullFlag"
    WINDOW          = 60        # flags form fast; 60 bars is enough
    APPROACHING_PCT = 0.010     # 1.0% below flag high

    def scan(self, ticker: str, df, spy_above_ma50: bool = True) -> dict | None:
        df = self._trim(df)
        if len(df) < 20:
            return None

        c, h, lo, v, vma = self._arrays(df)
        n = len(c)

        # Scan from the most recent possible pole backwards
        # Leave room for at least a 3-bar flag + breakout bar
        best = None
        for pole_start in range(n - 12, -1, -1):
            # Pole: ≥10% gain in ≤8 bars
            pole_end = None
            for plen in range(2, 9):
                pe = pole_start + plen
                if pe >= n:
                    break
                gain = (c[pe] - c[pole_start]) / c[pole_start]
                if gain >= 0.10:
                    pole_end = pe
                    break

            if pole_end is None:
                continue

            pole_low  = float(c[pole_start])
            pole_high = float(c[pole_end])
            pole_vol  = float(np.mean(v[pole_start : pole_end + 1]))

            # Flag: 3–8 bars of tight, declining-volume consolidation
            flag_end_best   = None
            flag_high_best  = None
            flag_low_best   = None

            for flen in range(3, 9):
                fe = pole_end + flen
                if fe >= n:
                    break

                flag_hi = float(np.max(h[pole_end : fe]))
                flag_lo = float(np.min(lo[pole_end : fe]))
                flag_range = (flag_hi - flag_lo) / float(c[pole_end])

                # Tight range ≤ 5%
                if flag_range > 0.05:
                    break  # loosening, stop

                # Volume contraction ≥ 40%
                flag_vol = float(np.mean(v[pole_end : fe]))
                if pole_vol > 0 and flag_vol / pole_vol <= 0.60:
                    flag_end_best  = fe
                    flag_high_best = flag_hi
                    flag_low_best  = flag_lo

            if flag_end_best is None:
                continue

            # Valid pole + flag structure found
            flag_end  = flag_end_best
            flag_high = flag_high_best
            flag_low  = flag_low_best
            pole_len  = pole_high - pole_low

            stop   = flag_low * 0.99
            target = pole_low + pole_len * 2.0
            chart_bars = flag_end - pole_start

            key_levels = {
                "pole_low":   pole_low,
                "pole_high":  pole_high,
                "flag_high":  flag_high,
                "flag_low":   flag_low,
            }

            current_close = float(c[-1])
            current_vol   = float(v[-1])
            current_vma   = float(vma[-1]) if not np.isnan(vma[-1]) else 0.0

            # Confirmed: if breakout bar falls within CONFIRM_LOOKBACK bars before now
            if flag_end < n:
                recent_close = float(c[flag_end])
                recent_vol   = float(v[flag_end])
                recent_vma   = float(vma[flag_end]) if not np.isnan(vma[flag_end]) else 0.0
                if recent_close > flag_high and recent_vma > 0 and recent_vol >= recent_vma * 1.3:
                    if spy_above_ma50 and current_close > stop:
                        vr = self._vol_ratio(recent_vol, recent_vma)
                        best = self._make_setup(
                            ticker, CONFIRMED, key_levels, stop, target, chart_bars,
                            entry=recent_close, vol_ratio=vr,
                            notes=f"Pole +{(pole_high/pole_low-1)*100:.1f}% Flag ≤5%"
                        )
                        break

            # Approaching: flag complete, price within 1% of flag high
            if flag_end >= n - 1:  # flag end is very recent
                if current_close >= flag_high * (1 - self.APPROACHING_PCT) and spy_above_ma50:
                    best = self._make_setup(
                        ticker, APPROACHING, key_levels, stop, target, chart_bars,
                        notes=f"Flag high={flag_high:.2f}"
                    )
                    break

                # Flag formed but price not near top yet
                best = self._make_setup(
                    ticker, WATCHING, key_levels, stop, target, chart_bars,
                    notes=f"Flag forming, high={flag_high:.2f}"
                )
                break

            # Flag found but it's older; keep scanning for a more recent one
            # (don't break, try to find a more recent pole/flag pair)

        return best
