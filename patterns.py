import pandas as pd


def find_local_extrema(series: pd.Series, order: int = 10):
    """Return indices of local highs and lows within a ±order bar window."""
    vals = series.values
    n = len(vals)
    highs, lows = [], []
    for i in range(order, n - order):
        window = vals[i - order: i + order + 1]
        if vals[i] >= max(window):
            highs.append(i)
        if vals[i] <= min(window):
            lows.append(i)

    def dedup(idxs, gap=3):
        result = []
        for idx in idxs:
            if not result or idx - result[-1] >= gap:
                result.append(idx)
        return result

    return dedup(highs), dedup(lows)


def detect_double_bottom(df: pd.DataFrame, order: int = 10,
                         similarity: float = 0.035, min_sep: int = 15,
                         lookback: int = 150, max_results: int = 4) -> list:
    """Detect all Double Bottom patterns in the most recent `lookback` bars."""
    close    = df["Close"].iloc[-lookback:]
    _, lows  = find_local_extrema(close, order)
    future_x = close.index[-1] + pd.Timedelta(weeks=8)
    results  = []

    if len(lows) < 2:
        return results

    for i in range(len(lows) - 1, 0, -1):
        for j in range(i - 1, -1, -1):
            i2, i1 = lows[i], lows[j]
            if i2 - i1 < min_sep:
                continue
            p1, p2      = float(close.iloc[i1]), float(close.iloc[i2])
            if abs(p1 - p2) / max(p1, p2) > similarity:
                continue
            between     = close.iloc[i1: i2 + 1]
            neckline    = float(between.max())
            peak_pos    = i1 + int(between.values.argmax())
            avg_low     = (p1 + p2) / 2
            if (neckline - avg_low) / avg_low < 0.04:
                continue
            current     = float(close.iloc[-1])
            depth       = neckline - avg_low
            measured_move = round(neckline + depth, 2)
            # Projected neckline breakout date — symmetric to left-side rise timing
            _left_rise = close.index[peak_pos] - close.index[i1]
            _proj      = close.index[i2] + _left_rise
            _proj      = max(_proj, close.index[-1] + pd.Timedelta(weeks=2))
            proj_neckline_x = min(_proj, future_x - pd.Timedelta(weeks=1))

            results.append({
                "pattern":          "Double Bottom",
                "description":      f"Two lows near ${avg_low:.2f} · neckline ${neckline:.2f} · target ${measured_move}",
                "neckline":         round(neckline, 2),
                "avg_low":          round(avg_low, 2),
                "measured_move":    measured_move,
                "confirmed":        current > neckline,
                "recency_bars":     len(close) - 1 - i2,
                "proj_neckline_x":  proj_neckline_x,
                # Pattern coords
                "low1_x":         close.index[i1],
                "low1_y":         round(p1, 2),
                "peak_x":         close.index[peak_pos],
                "peak_y":         round(neckline, 2),
                "low2_x":         close.index[i2],
                "low2_y":         round(p2, 2),
                # Neckline spans from low1 → today
                "neckline_x0":    close.index[i1],
                "neckline_x1":    close.index[-1],
                # Measured-move target extends into the future
                "target_x":       future_x,
                "target_y":       measured_move,
            })
            if len(results) >= max_results:
                return results
    return results


def detect_inv_head_shoulders(df: pd.DataFrame, order: int = 10,
                               shoulder_sim: float = 0.04, lookback: int = 200,
                               max_results: int = 4) -> list:
    """Detect all Inverse Head-and-Shoulders patterns."""
    close    = df["Close"].iloc[-lookback:]
    _, lows  = find_local_extrema(close, order)
    future_x = close.index[-1] + pd.Timedelta(weeks=8)
    results  = []

    if len(lows) < 3:
        return results

    for i in range(len(lows) - 1, 1, -1):
        rs_i, hd_i, ls_i = lows[i], lows[i - 1], lows[i - 2]
        if hd_i - ls_i < 10 or rs_i - hd_i < 10:
            continue
        ls_p = float(close.iloc[ls_i])
        hd_p = float(close.iloc[hd_i])
        rs_p = float(close.iloc[rs_i])
        if hd_p >= ls_p or hd_p >= rs_p:
            continue
        if abs(ls_p - rs_p) / max(ls_p, rs_p) > shoulder_sim:
            continue

        left_seg   = close.iloc[ls_i: hd_i + 1]
        right_seg  = close.iloc[hd_i: rs_i + 1]
        left_peak  = float(left_seg.max())
        right_peak = float(right_seg.max())
        lp_pos     = ls_i + int(left_seg.values.argmax())
        rp_pos     = hd_i + int(right_seg.values.argmax())
        neckline   = (left_peak + right_peak) / 2
        current    = float(close.iloc[-1])
        avg_sh     = (ls_p + rs_p) / 2
        measured_move = round(neckline + (neckline - hd_p), 2)

        # Projected neckline breakout date — symmetric to left-shoulder-to-peak timing
        _left_rise = close.index[lp_pos] - close.index[ls_i]
        _proj      = close.index[rs_i] + _left_rise
        _proj      = max(_proj, close.index[-1] + pd.Timedelta(weeks=2))
        proj_neckline_x = min(_proj, future_x - pd.Timedelta(weeks=1))

        results.append({
            "pattern":          "Inverse Head & Shoulders",
            "description":      f"Head ${hd_p:.2f} · shoulders ~${avg_sh:.2f} · neckline ${neckline:.2f} · target ${measured_move}",
            "neckline":         round(neckline, 2),
            "head_price":       round(hd_p, 2),
            "measured_move":    measured_move,
            "confirmed":        current > neckline,
            "recency_bars":     len(close) - 1 - rs_i,
            "proj_neckline_x":  proj_neckline_x,
            # Pattern coords
            "ls_x":           close.index[ls_i],
            "ls_y":           round(ls_p, 2),
            "lp_x":           close.index[lp_pos],
            "lp_y":           round(left_peak, 2),
            "hd_x":           close.index[hd_i],
            "hd_y":           round(hd_p, 2),
            "rp_x":           close.index[rp_pos],
            "rp_y":           round(right_peak, 2),
            "rs_x":           close.index[rs_i],
            "rs_y":           round(rs_p, 2),
            # Neckline spans from left shoulder → today
            "neckline_x0":    close.index[ls_i],
            "neckline_x1":    close.index[-1],
            # Measured-move target
            "target_x":       future_x,
            "target_y":       measured_move,
        })
        if len(results) >= max_results:
            return results
    return results


def detect_cup_and_handle(df: pd.DataFrame, order: int = 10,
                           rim_sim: float = 0.05, handle_pct: float = 0.35,
                           min_cup_bars: int = 30, lookback: int = 200,
                           max_results: int = 4) -> list:
    """Detect all Cup-and-Handle patterns."""
    close    = df["Close"].iloc[-lookback:]
    highs, _ = find_local_extrema(close, order)
    future_x = close.index[-1] + pd.Timedelta(weeks=8)
    results  = []

    if len(highs) < 2:
        return results

    for i in range(len(highs) - 1, 0, -1):
        for j in range(i - 1, -1, -1):
            lr_i, rr_i = highs[j], highs[i]
            if rr_i - lr_i < min_cup_bars:
                continue
            lr_p = float(close.iloc[lr_i])
            rr_p = float(close.iloc[rr_i])
            if abs(lr_p - rr_p) / max(lr_p, rr_p) > rim_sim:
                continue
            between       = close.iloc[lr_i: rr_i + 1]
            cup_pos       = lr_i + int(between.values.argmin())
            cup_p         = float(between.min())
            cup_depth     = (lr_p - cup_p) / lr_p
            if cup_depth < 0.08:
                continue
            handle_region = close.iloc[rr_i:]
            if len(handle_region) < 3:
                continue
            handle_low    = float(handle_region.min())
            handle_retrace = (rr_p - handle_low) / (rr_p - cup_p)
            if handle_retrace > handle_pct:
                continue
            current       = float(close.iloc[-1])
            avg_rim       = (lr_p + rr_p) / 2
            measured_move = round(rr_p + (rr_p - cup_p), 2)

            # Projected breakout date — handle typically ~1/6 of cup duration
            _cup_dur = (close.index[rr_i] - close.index[lr_i]) / 6
            _proj    = close.index[rr_i] + _cup_dur
            _proj    = max(_proj, close.index[-1] + pd.Timedelta(weeks=2))
            proj_neckline_x = min(_proj, future_x - pd.Timedelta(weeks=1))

            results.append({
                "pattern":          "Cup & Handle",
                "description":      f"Cup bottom ${cup_p:.2f} · rim ~${avg_rim:.2f} · target ${measured_move}",
                "neckline":         round(rr_p, 2),
                "cup_bottom":       round(cup_p, 2),
                "measured_move":    measured_move,
                "confirmed":        current > rr_p,
                "recency_bars":     len(close) - 1 - rr_i,
                "proj_neckline_x":  proj_neckline_x,
                # Pattern coords
                "lr_x":           close.index[lr_i],
                "lr_y":           round(lr_p, 2),
                "cup_x":          close.index[cup_pos],
                "cup_y":          round(cup_p, 2),
                "rr_x":           close.index[rr_i],
                "rr_y":           round(rr_p, 2),
                # Breakout line spans left rim → today
                "neckline_x0":    close.index[lr_i],
                "neckline_x1":    close.index[-1],
                # Measured-move target
                "target_x":       future_x,
                "target_y":       measured_move,
            })
            if len(results) >= max_results:
                return results
    return results


def detect_head_shoulders(df: pd.DataFrame, order: int = 10,
                          shoulder_sim: float = 0.04, lookback: int = 200,
                          max_results: int = 4) -> list:
    """Detect Head-and-Shoulders (bearish reversal) patterns."""
    close    = df["Close"].iloc[-lookback:]
    highs, _ = find_local_extrema(close, order)
    future_x = close.index[-1] + pd.Timedelta(weeks=8)
    results  = []

    if len(highs) < 3:
        return results

    for i in range(len(highs) - 1, 1, -1):
        rs_i, hd_i, ls_i = highs[i], highs[i - 1], highs[i - 2]
        if hd_i - ls_i < 10 or rs_i - hd_i < 10:
            continue
        ls_p = float(close.iloc[ls_i])
        hd_p = float(close.iloc[hd_i])
        rs_p = float(close.iloc[rs_i])
        if hd_p <= ls_p or hd_p <= rs_p:
            continue
        if abs(ls_p - rs_p) / max(ls_p, rs_p) > shoulder_sim:
            continue

        left_seg  = close.iloc[ls_i: hd_i + 1]
        right_seg = close.iloc[hd_i: rs_i + 1]
        lt_p      = float(left_seg.min())
        rt_p      = float(right_seg.min())
        lt_pos    = ls_i + int(left_seg.values.argmin())
        rt_pos    = hd_i + int(right_seg.values.argmin())
        neckline  = round((lt_p + rt_p) / 2, 2)

        current       = float(close.iloc[-1])
        avg_sh        = (ls_p + rs_p) / 2
        measured_move = round(neckline - (hd_p - neckline), 2)

        _left_fall      = close.index[lt_pos] - close.index[ls_i]
        _proj           = close.index[rs_i] + _left_fall
        _proj           = max(_proj, close.index[-1] + pd.Timedelta(weeks=2))
        proj_neckline_x = min(_proj, future_x - pd.Timedelta(weeks=1))

        results.append({
            "pattern":          "Head & Shoulders",
            "description":      f"Head ${hd_p:.2f} · shoulders ~${avg_sh:.2f} · neckline ${neckline} · target ${measured_move}",
            "neckline":         neckline,
            "head_price":       round(hd_p, 2),
            "measured_move":    measured_move,
            "confirmed":        current < neckline,
            "recency_bars":     len(close) - 1 - rs_i,
            "proj_neckline_x":  proj_neckline_x,
            "bearish":          True,
            "ls_x":           close.index[ls_i],
            "ls_y":           round(ls_p, 2),
            "lt_x":           close.index[lt_pos],
            "lt_y":           round(lt_p, 2),
            "hd_x":           close.index[hd_i],
            "hd_y":           round(hd_p, 2),
            "rt_x":           close.index[rt_pos],
            "rt_y":           round(rt_p, 2),
            "rs_x":           close.index[rs_i],
            "rs_y":           round(rs_p, 2),
            "neckline_x0":    close.index[ls_i],
            "neckline_x1":    close.index[-1],
            "target_x":       future_x,
            "target_y":       measured_move,
        })
        if len(results) >= max_results:
            return results
    return results


def detect_double_top(df: pd.DataFrame, order: int = 10,
                      similarity: float = 0.035, min_sep: int = 15,
                      lookback: int = 150, max_results: int = 4) -> list:
    """Detect Double Top (bearish reversal) patterns."""
    close    = df["Close"].iloc[-lookback:]
    highs, _ = find_local_extrema(close, order)
    future_x = close.index[-1] + pd.Timedelta(weeks=8)
    results  = []

    if len(highs) < 2:
        return results

    for i in range(len(highs) - 1, 0, -1):
        for j in range(i - 1, -1, -1):
            i2, i1 = highs[i], highs[j]
            if i2 - i1 < min_sep:
                continue
            p1, p2 = float(close.iloc[i1]), float(close.iloc[i2])
            if abs(p1 - p2) / max(p1, p2) > similarity:
                continue
            between    = close.iloc[i1: i2 + 1]
            neckline   = float(between.min())
            trough_pos = i1 + int(between.values.argmin())
            avg_high   = (p1 + p2) / 2
            if (avg_high - neckline) / avg_high < 0.04:
                continue
            current       = float(close.iloc[-1])
            depth         = avg_high - neckline
            measured_move = round(neckline - depth, 2)

            _left_fall      = close.index[trough_pos] - close.index[i1]
            _proj           = close.index[i2] + _left_fall
            _proj           = max(_proj, close.index[-1] + pd.Timedelta(weeks=2))
            proj_neckline_x = min(_proj, future_x - pd.Timedelta(weeks=1))

            results.append({
                "pattern":          "Double Top",
                "description":      f"Two highs near ${avg_high:.2f} · neckline ${neckline:.2f} · target ${measured_move}",
                "neckline":         round(neckline, 2),
                "avg_high":         round(avg_high, 2),
                "measured_move":    measured_move,
                "confirmed":        current < neckline,
                "recency_bars":     len(close) - 1 - i2,
                "proj_neckline_x":  proj_neckline_x,
                "bearish":          True,
                "high1_x":        close.index[i1],
                "high1_y":        round(p1, 2),
                "trough_x":       close.index[trough_pos],
                "trough_y":       round(neckline, 2),
                "high2_x":        close.index[i2],
                "high2_y":        round(p2, 2),
                "neckline_x0":    close.index[i1],
                "neckline_x1":    close.index[-1],
                "target_x":       future_x,
                "target_y":       measured_move,
            })
            if len(results) >= max_results:
                return results
    return results


def get_historical_outcomes(df: pd.DataFrame, pattern_type: str,
                            order: int = 10, max_bars: int = 40,
                            active_threshold: int = 40) -> dict:
    """
    Run pattern detection over the full history, find past (completed) instances,
    and return outcome statistics: how often did price reach the measured move?
    """
    _BEARISH = {"Head & Shoulders", "Double Top"}
    full_n = len(df)
    if pattern_type == "Double Bottom":
        results = detect_double_bottom(df, order=order, lookback=full_n, max_results=30)
    elif pattern_type == "Inverse Head & Shoulders":
        results = detect_inv_head_shoulders(df, order=order, lookback=full_n, max_results=30)
    elif pattern_type == "Cup & Handle":
        results = detect_cup_and_handle(df, order=order, lookback=full_n, max_results=30)
    elif pattern_type == "Head & Shoulders":
        results = detect_head_shoulders(df, order=order, lookback=full_n, max_results=30)
    elif pattern_type == "Double Top":
        results = detect_double_top(df, order=order, lookback=full_n, max_results=30)
    else:
        return {}

    # Only analyse patterns that are clearly in the past (not currently forming)
    historical = [r for r in results if r.get("recency_bars", 0) > active_threshold]
    if not historical:
        return {}

    close_vals = df["Close"].values
    hits, bars_to_hit = 0, []
    is_bearish = pattern_type in _BEARISH

    for r in historical:
        recency  = r["recency_bars"]
        from_bar = full_n - 1 - recency
        target   = r["measured_move"]
        end      = min(from_bar + max_bars + 1, full_n)
        future   = close_vals[from_bar + 1: end]
        hit_mask = future <= target if is_bearish else future >= target
        if hit_mask.any():
            hits += 1
            bars_to_hit.append(int(hit_mask.argmax()) + 1)

    total = len(historical)
    return {
        "total":    total,
        "hits":     hits,
        "hit_rate": round(hits / total, 2) if total else 0,
        "avg_bars": round(sum(bars_to_hit) / len(bars_to_hit), 1) if bars_to_hit else None,
    }


def detect_support_resistance(df: pd.DataFrame, order: int = 10,
                               cluster_pct: float = 0.025, n_levels: int = 6):
    """Find key S/R levels by clustering local highs and lows."""
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    highs, lows = find_local_extrema(close, order)

    prices = [float(high.iloc[i]) for i in highs] + [float(low.iloc[i]) for i in lows]
    if not prices:
        return []

    prices.sort()
    clusters = [[prices[0]]]
    for price in prices[1:]:
        if (price - clusters[-1][0]) / clusters[-1][0] <= cluster_pct:
            clusters[-1].append(price)
        else:
            clusters.append([price])

    current = float(close.iloc[-1])
    result  = []
    for cluster in clusters:
        avg      = sum(cluster) / len(cluster)
        strength = len(cluster)
        result.append({
            "price":    round(avg, 2),
            "strength": strength,
            "type":     "resistance" if avg > current else "support",
        })

    result.sort(key=lambda x: -x["strength"])
    return result[:n_levels]
