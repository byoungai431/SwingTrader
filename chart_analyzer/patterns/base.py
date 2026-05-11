"""
BasePattern — abstract base class for all Chart Analyzer pattern detectors.

Each subclass implements scan(ticker, df, spy_above_ma50) → dict | None.

Stage values:
  DETECTED    — pattern structure present but not near trigger
  WATCHING    — structure confirmed, monitoring
  APPROACHING — price within pattern-specific proximity threshold of breakout
  CONFIRMED   — clean breakout with volume in last CONFIRM_LOOKBACK bars
"""

from __future__ import annotations


import numpy as np
import pandas as pd
from abc import ABC, abstractmethod
from scipy.signal import argrelextrema


# Stage constants
DETECTED    = "DETECTED"
WATCHING    = "WATCHING"
APPROACHING = "APPROACHING"
CONFIRMED   = "CONFIRMED"

CONFIRM_LOOKBACK = 3  # bars back to recognise a recent breakout as CONFIRMED


class BasePattern(ABC):
    PATTERN_TYPE: str = ""
    WINDOW: int = 120                  # bars of history to analyse
    APPROACHING_PCT: float = 0.015    # default proximity threshold (1.5%)

    @abstractmethod
    def scan(self, ticker: str, df: pd.DataFrame, spy_above_ma50: bool = True) -> dict | None:
        """
        Analyse recent price history and return a setup dict describing the
        current state of any developing pattern, or None if no valid setup found.
        """
        ...

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _trim(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.tail(self.WINDOW).copy().reset_index(drop=True)

    def _arrays(self, df: pd.DataFrame):
        """Return (close, high, low, volume, vol_ma20) as float64 numpy arrays."""
        c   = df["Close"].values.astype(np.float64)
        h   = df["High"].values.astype(np.float64)
        lo  = df["Low"].values.astype(np.float64)
        v   = df["Volume"].values.astype(np.float64)
        vma = pd.Series(v).rolling(20, min_periods=1).mean().values
        return c, h, lo, v, vma

    def _local_mins(self, lo: np.ndarray, order: int = 8) -> np.ndarray:
        return argrelextrema(lo, np.less, order=order)[0]

    def _local_maxs(self, hi: np.ndarray, order: int = 5) -> np.ndarray:
        return argrelextrema(hi, np.greater, order=order)[0]

    def _make_setup(
        self,
        ticker: str,
        stage: str,
        key_levels: dict,
        stop: float,
        target: float,
        chart_bars: int,
        entry: float | None = None,
        vol_ratio: float | None = None,
        notes: str = "",
    ) -> dict:
        return {
            "ticker":       ticker,
            "pattern_type": self.PATTERN_TYPE,
            "stage":        stage,
            "key_levels":   {k: round(float(v), 4) for k, v in key_levels.items()},
            "entry_price":  round(float(entry), 4) if entry is not None else None,
            "stop_price":   round(float(stop),  4) if stop  is not None else None,
            "target_price": round(float(target),4) if target is not None else None,
            "vol_ratio":    round(float(vol_ratio), 3) if vol_ratio is not None else None,
            "chart_bars":   int(chart_bars),
            "notes":        str(notes),
        }

    def _vol_ratio(self, vol: float, vma: float) -> float | None:
        return round(vol / vma, 3) if vma and not np.isnan(vma) and vma > 0 else None
