# CHANGELOG

---

## 2026-05-11 — E8 Chart Analyzer: Phase 4 — Scheduler (LaunchAgent)

The Chart Analyzer now runs automatically every weekday at 4:45 PM local time via a macOS LaunchAgent, independent of `run_daily.py` and the existing signal pipeline.

### New Files

**`run_chart_analyzer.sh`** — Shell wrapper script that orchestrates the daily run:
- Auto-discovers the correct Python interpreter by testing candidates in preference order (pyenv → miniforge/miniconda/anaconda → Homebrew → system). Falls back gracefully with a clear error if none found; a `PYTHON_OVERRIDE` variable at the top of the file allows manual override.
- Weekday gate: skips Saturday and Sunday automatically (no LaunchAgent calendar logic needed for this).
- Rotates the log file at 500 lines to prevent unbounded growth.
- Runs `python -m chart_analyzer.scanner` (pattern detection + per-transition Telegram alerts).
- Runs `python -m chart_analyzer.alerts` (daily digest Telegram message).
- Timestamps all log entries; logs to `logs/chart_analyzer.log`.

**`logs/`** — Directory created for scanner and LaunchAgent log output.

**`~/Library/LaunchAgents/com.nexusedge.chart-analyzer.plist`** — macOS LaunchAgent:
- Fires `run_chart_analyzer.sh` daily at **16:45 local time** (4:45 PM).
- `RunAtLoad: false` — does not fire on login, only on schedule.
- `KeepAlive: false` — does not restart on crash.
- Separate `StandardOutPath`/`StandardErrorPath` for launchd-level output (`logs/chart_analyzer_launchd.log`).
- Already registered: `launchctl load ~/Library/LaunchAgents/com.nexusedge.chart-analyzer.plist`.

### Timezone Note
The `StartCalendarInterval` uses local time. The default (16:45) is correct for US Eastern. Adjust `Hour` in the plist for other timezones: CT → 15, MT → 14, PT → 13. After any plist edit, reload with:
```
launchctl unload ~/Library/LaunchAgents/com.nexusedge.chart-analyzer.plist
launchctl load   ~/Library/LaunchAgents/com.nexusedge.chart-analyzer.plist
```

### Manual Run
```bash
bash /Users/johnathonlaux/Projects/SwingTrader_Loxy/run_chart_analyzer.sh
```

---

## 2026-05-11 — E8 Chart Analyzer: Phase 3 — UI, Charts & Alerts

Phase 3 adds the user-facing layer: annotated Plotly thumbnail charts, a daily Telegram digest, and the fully wired "📐 Patterns" tab in the Streamlit app.

### New Files

**`chart_analyzer/charts.py`** — Builds annotated Plotly candlestick thumbnails (2-pane: price + volume, last 90 bars) for every pattern type. Each pattern overlays pattern-specific geometry:
- *InvHnS*: horizontal neckline, LS/Head/RS low markers
- *AscTriangle*: flat resistance line, dotted rising-lows trendline
- *Cup & Handle*: left/right rim lines, cup-bottom marker, handle level
- *Bull Flag*: flag high/low levels, pole region shading
- *Falling Wedge*: upper trendline (solid purple), lower trendline (dotted teal)

All charts show stop (dashed red) and target (dashed green) levels. Public API: `build_pattern_chart(ticker, df, setup)` and the convenience wrapper `build_setup_card_figure(setup)` which fetches price data via a 5-minute Streamlit cache.

**`chart_analyzer/alerts.py`** — `send_daily_digest()`: queries all CONFIRMED and APPROACHING setups and sends a single end-of-day Telegram summary message. Separate from the per-transition alerts in `scanner.py`. Run standalone via `python -m chart_analyzer.alerts`.

### Modified Files

**`app.py`** — Three changes:
1. **Session state**: added `show_chart_analyzer` flag initialised to `False`.
2. **Nav bar**: expanded from 4 to 5 columns; added "📐 Patterns" button (4th column) that toggles `show_chart_analyzer` and clears all other views. The ticker selectbox moved to the 5th column.
3. **`show_chart_analyzer_view()` function**: inserted before `show_recommended_view()`. Layout:
   - Header + back button.
   - Backtest stats banner: PF and pass/fail badge per pattern (5 mini tiles).
   - Status bar: total active, confirmed, approaching, watching, detected counts.
   - **CONFIRMED section** (two-column thumbnail grid with annotated charts + metadata cards).
   - **APPROACHING section** (same two-column grid).
   - **Watching section** (compact three-column list, no charts).
   - **Detected section** (collapsed expander, ticker + pattern name only).
   - All `show_chart_analyzer = False` resets propagated to all routing paths (sidebar ticker buttons, Home button, mobile selectbox, CTW card buttons).

---

## 2026-05-11 — E8 Chart Analyzer: Phase 2 — Live Detection Modules & Scanner

Building on the Phase 1 foundation, Phase 2 adds the full live-scan engine: five pattern detector classes, a daily scanner orchestrator with DB state management, and Telegram alerts wired to fire at both the APPROACHING and CONFIRMED stages.

### New Files

**`chart_analyzer/patterns/__init__.py`** — Exports all 5 pattern classes and an `ALL_PATTERNS` list for the scanner to iterate.

**`chart_analyzer/patterns/base.py`** — `BasePattern` abstract base class. Defines the `scan(ticker, df, spy_above_ma50)` interface, stage constants (`DETECTED`, `WATCHING`, `APPROACHING`, `CONFIRMED`), and shared helpers (`_trim`, `_arrays`, `_local_mins`, `_local_maxs`, `_make_setup`, `_vol_ratio`).

**`chart_analyzer/patterns/inv_head_shoulders.py`** — `InvHeadShoulders`. Scans the last 150 bars for a valid triplet of local lows (head deepest, shoulders within 5%). Draws the neckline from inter-swing peaks, then classifies: WATCHING (RS formed, price below neckline), APPROACHING (within 1.5% of neckline), CONFIRMED (breakout above neckline with vol ≥ 1.2×). No SPY regime gate (reversal). Stop: 2% below RS low. Target: neckline + (neckline − head).

**`chart_analyzer/patterns/ascending_triangle.py`** — `AscendingTriangle`. Requires ≥3 local highs within 2.5% (flat resistance) and ≥3 rising lows. SPY > MA50 required for APPROACHING and CONFIRMED. Proximity: 1.5% below resistance. Vol ≥ 1.3× on breakout. Stop: 1% below last rising low. Target: resistance + (resistance − first rising low).

**`chart_analyzer/patterns/cup_handle.py`** — `CupHandle`. Anchors off the most recent local high as the left rim, finds the cup bottom (drop ≥12%), waits for recovery to within 5% of rim (right rim), then detects handle (3–15 bars, ≤35% retrace, declining vol). Returns DETECTED if cup forming, WATCHING when right rim is in but no handle yet, APPROACHING when handle is complete and price within 1.5% of right rim, CONFIRMED on breakout. SPY gate applies. Stop: 1% below handle low. Target: right rim + cup depth.

**`chart_analyzer/patterns/bull_flag.py`** — `BullFlag`. Scans from most recent bar backward for pole (≥10% gain in ≤8 bars) followed by flag (3–8 bars, range ≤5%, volume dried ≥40% vs pole). APPROACHING when flag is active and price within 1.0% of flag high; CONFIRMED when breakout bar exists with vol ≥ 1.3×. SPY gate. Stop: 1% below flag low. Target: pole length × 2 from pole low.

**`chart_analyzer/patterns/falling_wedge.py`** — `FallingWedge`. Fits linear regression to last 3–5 local highs and local lows. Both slopes must be negative; lower slope must be less negative (converging). Width must compress to ≤40% of initial. APPROACHING when price within 2.0% of upper trendline. CONFIRMED on close above trendline with vol ≥ 1.2×. No SPY gate (reversal). Stop: 1% below 10-bar swing low. Target: top of wedge.

**`chart_analyzer/scanner.py`** — Daily scan orchestrator. On each run:
1. Calls `init_db()` to ensure tables exist.
2. Loads top-100 universe via `get_universe()` (respects 7-day cache).
3. Fetches SPY regime gate (current close vs 50-day MA).
4. Downloads 6 months of OHLCV in batches of 50.
5. Runs all 5 detectors per ticker.
6. Applies stage-transition logic: setups can only advance (DETECTED → WATCHING → APPROACHING → CONFIRMED), never regress.
7. Upserts DB records via `upsert_setup()`.
8. Sends Telegram alerts at APPROACHING (📡) and CONFIRMED (🔔) stage transitions — only once per transition via `mark_alert_sent()`.
9. Invalidates DETECTED/WATCHING setups that have not progressed for > 30 days.
10. Prints a formatted scan summary.

Run:  `python -m chart_analyzer.scanner`

### Stage Transition Rules
| From → To | Action |
|---|---|
| None → DETECTED/WATCHING/APPROACHING/CONFIRMED | Insert new setup |
| DETECTED → WATCHING/APPROACHING/CONFIRMED | Update; set timestamps; send alert if APPROACHING or CONFIRMED |
| WATCHING → APPROACHING/CONFIRMED | Update; send APPROACHING alert |
| APPROACHING → CONFIRMED | Update; send CONFIRMED alert |
| Any → backward | Ignored (no regression) |
| DETECTED/WATCHING + age > 30d, no detection | Invalidated |

---

## 2026-05-11 — E8 Chart Analyzer: Phase 1 Foundation

A completely isolated chart pattern detection module (`chart_analyzer/`) has been added to Nexus Edge. It runs independently of the existing 7-engine signal pipeline — no changes to `signal_engine.py`, `run_daily.py`, `history.py`, `indicators.py`, or the `signals` table.

### Architecture Overview

**Purpose:** Detect 5 high-conviction chart patterns as they develop, surface them at two stages (APPROACHING and CONFIRMED), and eventually present them in a dedicated "Chart Analyzer" tab in the Streamlit UI.

**Stage model:** `DETECTED → WATCHING → APPROACHING → CONFIRMED → TRIGGERED/INVALIDATED`
- APPROACHING alerts fire when a pattern is geometrically close to completion (pattern-specific thresholds)
- CONFIRMED alerts fire on a clean breakout bar with volume confirmation
- This two-stage design ensures explosive breakouts are not missed while confirmed entries retain high conviction

**5 Patterns implemented:**
1. **Inverse Head & Shoulders** — reversal; no SPY regime gate
2. **Ascending Triangle** — continuation; requires SPY > MA50
3. **Cup & Handle** — continuation; requires SPY > MA50
4. **Bull Flag** — continuation; requires SPY > MA50
5. **Falling Wedge** — reversal; no SPY regime gate

**Backtest gate:** Profit Factor ≥ 1.25 required per pattern. Any pattern that fails validation is excluded from live scanning.

**Universe:** Top-100 S&P 500 tickers by 20-day average dollar volume (price × volume). Cached in `pattern_universe` table; auto-refreshes every 7 days.

### New Files

**`chart_analyzer/__init__.py`** — Module initializer (empty).

**`chart_analyzer/history.py`** — Isolated PostgreSQL layer for the chart analyzer. Creates and manages three new tables:
- `pattern_setups` — active and historical pattern records with stage, key levels, entry/stop/target, and alert flags
- `pattern_backtest_stats` — PF, win rate, avg bars, and pass/fail per pattern type
- `pattern_universe` — top-100 ticker cache with dollar volumes and refresh timestamp

Key functions: `init_db()`, `upsert_setup()`, `get_active_setups()`, `write_universe()`, `get_universe_tickers()`, `write_backtest_stats()`, `get_backtest_stats()`, `mark_alert_sent()`, `invalidate_setup()`

**`chart_analyzer/universe.py`** — Computes the top-100 universe by scraping the S&P 500 list from Wikipedia, downloading 30 days of OHLCV via yfinance, and ranking by 20-day average dollar volume. `get_universe()` returns the cached DB list if refreshed within 7 days; otherwise recomputes.

**`chart_analyzer/backtest.py`** — Walk-forward backtest (2020-01-01 → 2026-01-01) on the top-100 universe. Downloads data in batches of 50, runs all 5 pattern detectors per ticker, simulates trades with priority: stop-hit (vs daily low) → target-hit (vs daily high) → max-hold exit (40 bars). Applies 40-bar cooldown between signals per ticker and 0.1% round-trip commission. Writes results to `pattern_backtest_stats` and prints a formatted results table. Run via `python -m chart_analyzer.backtest`.

### Phase 2+ (Upcoming)
- `chart_analyzer/patterns/` — live detection modules (one per pattern) producing APPROACHING/CONFIRMED setups
- `chart_analyzer/scanner.py` — daily scanner reading the live universe and running all pattern detectors
- `chart_analyzer/alerts.py` — Telegram alerts distinct from existing signal alerts (📡 APPROACHING, 🔔 CONFIRMED)
- `chart_analyzer/charts.py` — annotated Plotly thumbnails for the UI
- Chart Analyzer tab in `app.py` — two-column thumbnail grid, CONFIRMED above APPROACHING
- Independent cron/LaunchAgent for headless scanner execution

---

## 2026-05-11 — Signal Accuracy & Performance Fixes

### Fix 1 — Engine 5: Missing `yfinance` import (crash)
**File:** `signal_engine.py` — `compute_hot_sectors()`  
**Problem:** `yf.download(...)` was called at line 441 but `yfinance` was never imported anywhere in the file. Every daily scan that reached Engine 5 sector rotation would crash with `NameError: name 'yf' is not defined`, silently skipping all E5 signals.  
**Fix:** Added `import yfinance as yf` inside `compute_hot_sectors()`, alongside the existing inline numpy/pandas imports.

---

### Fix 2 — Engine 7: Star label was inverted
**File:** `signal_engine.py` — `get_pattern_signal()`  
**Problem:** `_star_label = "5★" if _stars == 6 else "4★"` — when `_stars` was 6 (Tier 1, High Conviction), the rationale string printed "5★"; when `_stars` was 5 it printed "4★". The label was one tier low in both cases, causing misleading output in Telegram alerts and the UI.  
**Fix:** Changed to `_star_label = "5★ MAX" if _stars >= 6 else "5★"`, which correctly labels conf=6 as "5★ MAX" and conf=5 as "5★".

---

### Fix 3 — IBS exit threshold mismatched backtest
**File:** `run_daily.py` — `IBS_MIN_EXIT`  
**Problem:** Live system used `IBS_MIN_EXIT = 0.80` but the validated backtest used `SW_IBS_MIN_EXIT = 0.90` (in `backtest_master.py`). This caused 5★ positions to exit 10 IBS points earlier than the backtested strategy, producing live results that diverge from the expected performance curve.  
**Fix:** Changed `IBS_MIN_EXIT` from `0.80` → `0.90` to match the backtest configuration.

---

### Fix 4 — Engine 1: MA50 reclaim had a 1% false-positive buffer
**File:** `signal_engine.py` — `get_signal()`  
**Problem:** `cond_ma50 = (close > ma50) and (prev_close <= prev_ma50 * 1.01)`. The `* 1.01` tolerance meant a stock that was already 0.9% above its MA50 yesterday could still count as "reclaiming from below" today, generating a false MA50 bounce signal for stocks in steady uptrends well above their MA50.  
**Fix:** Removed the buffer: `cond_ma50 = (close > ma50) and (prev_close <= prev_ma50)`. A strict cross is required — price must have been at or below the MA50 the prior day.

---

### Fix 5 — Engine 1: E1 SELL role clarified
**File:** `signal_engine.py` — `get_signal()`  
**Problem:** E1 SELL signals require `sells_count >= 2 AND buys_count == 0 AND two_red AND bear_regime`, and are additionally blocked in `run_daily.py` unless there is an open BUY position. This means E1 SELL almost never fires, and all real exits are handled by the auto-exit system (stop, trail, IBS, max-hold). This created confusion about which system was responsible for exits.  
**Fix:** Added an explanatory comment above the SELL conditions block making the role of E1 SELL explicit: it is a secondary overlay; primary exits are owned by `_check_stop_target_hits()` in `run_daily.py`. No logic was removed since the SELL path is wired into the app UI.

---

### Fix 6 — Earnings proximity entry filter added
**File:** `run_daily.py` — signal processing loop  
**Problem:** BUY signals were generated for stocks 1–2 days before earnings, exposing positions to binary gap risk (earnings surprise gaps through stops). No earnings proximity check existed in the entry filter chain.  
**Fix:** Added an earnings proximity guard after the IBS filter. If `days_to_earnings <= 2` for the base ticker, the BUY signal is suppressed for E1 (core swing) and E3 (leveraged) entries. The `days_to_earnings` field is already computed by `compute_indicators()` via yfinance calendar data.

---

### Fix 7 — Engine 6: `is_t2` purpose clarified
**File:** `signal_engine.py` — `get_chop_signal()`  
**Problem:** `is_t2` was computed but appeared unused for the confidence score (`conf = 5 if is_t1 else 3` — T2 and T3 both get conf=3). This looked like a dead variable and created confusion about whether T2 should get a different score.  
**Fix:** Added an explicit comment (`# T2 and T3 intentionally share conf=3 — is_t2 is kept for the rationale label only`) to make the intent clear. `is_t2` is legitimately used in `tier_label` for the rationale string (T2 3★ vs T3 3★), just not for confidence scoring, which is by design.

---

### Fix 8 — Removed unnecessary 0.5s sleep per ticker
**File:** `run_daily.py` — main scan loop  
**Problem:** A `time.sleep(0.5)` call existed at the end of each ticker's processing loop with a comment about "API rate limits." However, all signal generation is now entirely rule-based — there are no API calls inside the scan loop. At 500+ S&P 500 tickers, this sleep added approximately 4–5 minutes of dead time to every daily scan run.  
**Fix:** Removed `time.sleep(0.5)` and the now-unused `import time` statement entirely.
