"""
Cup & Handle detector (live-scan mode).

Proximity threshold  : handle formed + price within 1.5% of right rim → APPROACHING
Regime gate          : SPY > 50-day MA (continuation pattern)
Volume on breakout   : ≥ 1.3× 20-bar average
Cup depth            : ≥ 12%
Handle retrace       : ≤ 35% of cup rise, 3–15 bars, declining volume
Stop                 : 1% below handle low
Target               : right rim + cup depth
"""

from __future__ import annotations


import numpy as np
import pandas as pd
from chart_analyzer.patterns.base import BasePattern, DETECTED, WATCHING, APPROACHING, CONFIRMED


class CupHandle(BasePattern):
    PATTERN_TYPE    = "CupHandle"
    WINDOW          = 180        # cups can be large
    APPROACHING_PCT = 0.015      # 1.5% below right rim

    def scan(self, ticker: str, df, spy_above_ma50: bool = True) -> dict | None:
        df = self._trim(df)
        if len(df) < 60:
            return None

        c, h, lo, v, vma = self._arrays(df)
        n = len(c)

        local_highs = self._local_maxs(h, order=8)
        if len(local_highs) < 2:
            return None

        # Try each local high as the left rim, most recent first
        for li in range(len(local_highs) - 1, -1, -1):
            left_rim_idx = int(local_highs[li])
            left_rim     = float(h[left_rim_idx])

            # Cup region: bars after left rim
            cup_end = min(left_rim_idx + 120, n - 10)
            if cup_end - left_rim_idx < 15:
                continue

            cup_lo_seg = lo[left_rim_idx : cup_end]
            cup_bottom_offset = int(np.argmin(cup_lo_seg))
            cup_bottom_idx    = left_rim_idx + cup_bottom_offset
            cup_bottom        = float(lo[cup_bottom_idx])

            # Cup depth must be ≥ 12%
            if (left_rim - cup_bottom) / left_rim < 0.12:
                continue

            # Right rim: first bar where close recovers to within 5% of left rim
            recovery_seg = c[cup_bottom_idx : cup_end]
            right_rim_offset = next(
                (j for j, val in enumerate(recovery_seg) if val >= left_rim * 0.95),
                None,
            )
            if right_rim_offset is None:
                # Cup hasn't recovered yet — DETECTED stage
                key_levels = {
                    "left_rim":    left_rim,
                    "cup_bottom":  cup_bottom,
                }
                stop   = cup_bottom * 0.97
                target = left_rim + (left_rim - cup_bottom)
                return self._make_setup(
                    ticker, DETECTED, key_levels, stop, target,
                    chart_bars=cup_bottom_idx - left_rim_idx,
                    notes=f"Cup depth={(left_rim-cup_bottom)/left_rim*100:.1f}%"
                )

            right_rim_idx = cup_bottom_idx + right_rim_offset
            right_rim     = float(c[right_rim_idx])
            cup_depth     = left_rim - cup_bottom
            cup_rise      = right_rim - cup_bottom

            # Handle: 3–15 bars of tight consolidation after right rim
            handle_end_max = min(right_rim_idx + 15, n)
            best_handle_end = None
            handle_low_best = right_rim  # will be updated

            for hlen in range(3, handle_end_max - right_rim_idx + 1):
                he = right_rim_idx + hlen
                if he >= n:
                    break

                h_seg_lo = lo[right_rim_idx : he]
                h_seg_hi = h[right_rim_idx : he]
                h_low  = float(np.min(h_seg_lo))
                h_high = float(np.max(h_seg_hi))

                # Handle retrace ≤ 35% of cup rise
                if cup_rise > 0 and (right_rim - h_low) / cup_rise > 0.35:
                    break  # too deep, stop extending

                # Declining volume in handle vs cup recovery
                pre_vol  = float(np.mean(v[cup_bottom_idx : right_rim_idx])) if right_rim_idx > cup_bottom_idx else 1.0
                hdl_vol  = float(np.mean(v[right_rim_idx : he]))
                if pre_vol > 0 and hdl_vol / pre_vol < 1.0:  # vol declining
                    best_handle_end = he
                    handle_low_best = h_low

            if best_handle_end is None:
                # Right rim formed but no handle yet — WATCHING
                key_levels = {
                    "left_rim":   left_rim,
                    "cup_bottom": cup_bottom,
                    "right_rim":  right_rim,
                }
                stop   = cup_bottom * 0.97
                target = right_rim + cup_depth
                return self._make_setup(
                    ticker, WATCHING, key_levels, stop, target,
                    chart_bars=right_rim_idx - left_rim_idx,
                    notes=f"Awaiting handle"
                )

            # Handle formed
            handle_low = handle_low_best
            stop   = handle_low * 0.99
            target = right_rim + cup_depth
            chart_bars = best_handle_end - left_rim_idx

            key_levels = {
                "left_rim":   left_rim,
                "cup_bottom": cup_bottom,
                "right_rim":  right_rim,
                "handle_low": handle_low,
            }

            current_close = float(c[-1])
            current_vol   = float(v[-1])
            current_vma   = float(vma[-1]) if not np.isnan(vma[-1]) else 0.0

            # Confirmed: close above right rim with vol ≥ 1.3x
            if current_close > right_rim and current_vma > 0 and current_vol >= current_vma * 1.3:
                if spy_above_ma50 and current_close > stop:
                    vr = self._vol_ratio(current_vol, current_vma)
                    return self._make_setup(
                        ticker, CONFIRMED, key_levels, stop, target, chart_bars,
                        entry=current_close, vol_ratio=vr,
                        notes=f"Right rim={right_rim:.2f}"
                    )
                return None

            # Approaching: handle done, price within 1.5% of right rim
            if current_close >= right_rim * (1 - self.APPROACHING_PCT) and spy_above_ma50:
                return self._make_setup(
                    ticker, APPROACHING, key_levels, stop, target, chart_bars,
                    notes=f"Right rim={right_rim:.2f}"
                )

            return self._make_setup(
                ticker, WATCHING, key_levels, stop, target, chart_bars,
                notes=f"Handle forming, right rim={right_rim:.2f}"
            )

        return None
