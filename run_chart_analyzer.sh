#!/bin/bash
# ============================================================
# run_chart_analyzer.sh
# Daily Chart Analyzer runner for Nexus Edge.
# Called by the com.nexusedge.chart-analyzer LaunchAgent
# at 4:45 PM local time on weekdays.
#
# To override the Python interpreter, set PYTHON_OVERRIDE:
#   export PYTHON_OVERRIDE=/path/to/your/python3
# Or edit the PYTHON_OVERRIDE line directly below.
# ============================================================

PROJECT_DIR="/Users/johnathonlaux/Projects/SwingTrader_Loxy"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/chart_analyzer.log"
PYTHON_OVERRIDE="/opt/homebrew/bin/python3.11"

# ── Rotate log: keep last 500 lines ──────────────────────────────────────────
mkdir -p "$LOG_DIR"
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 500 ]; then
    tail -400 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "========================================================"
log "Chart Analyzer run starting"
log "========================================================"

# ── Weekday gate (skip Saturday=6 and Sunday=0) ───────────────────────────────
DAY=$(date +%u)   # 1=Mon … 7=Sun
if [ "$DAY" -eq 6 ] || [ "$DAY" -eq 7 ]; then
    log "Weekend — skipping run."
    exit 0
fi

# ── Discover Python ───────────────────────────────────────────────────────────
find_python() {
    # Candidates in preference order
    local candidates=(
        "$PYTHON_OVERRIDE"
        "$HOME/.pyenv/shims/python3"
        "$HOME/miniforge3/bin/python3"
        "$HOME/miniconda3/bin/python3"
        "$HOME/anaconda3/bin/python3"
        "/opt/homebrew/bin/python3"
        "/opt/homebrew/bin/python3.12"
        "/opt/homebrew/bin/python3.11"
        "/opt/homebrew/bin/python3.10"
        "/usr/local/bin/python3"
        "/usr/bin/python3"
    )
    for py in "${candidates[@]}"; do
        [ -z "$py" ] && continue
        [ -x "$py" ] || continue
        "$py" -c "import yfinance, scipy, psycopg2" 2>/dev/null && { echo "$py"; return 0; }
    done
    return 1
}

PYTHON=$(find_python)
if [ -z "$PYTHON" ]; then
    log "ERROR: Could not find a Python interpreter with yfinance, scipy, psycopg2 installed."
    log "Set PYTHON_OVERRIDE at the top of this script to the correct Python path."
    log "Example:  PYTHON_OVERRIDE=/opt/homebrew/bin/python3.11"
    exit 1
fi

log "Using Python: $PYTHON  ($("$PYTHON" --version 2>&1))"

# ── Run scanner ───────────────────────────────────────────────────────────────
log "--- Scanner start ---"
cd "$PROJECT_DIR" || { log "ERROR: Cannot cd to $PROJECT_DIR"; exit 1; }

"$PYTHON" -m chart_analyzer.scanner >> "$LOG_FILE" 2>&1
SCAN_EXIT=$?

if [ $SCAN_EXIT -eq 0 ]; then
    log "Scanner completed successfully."
else
    log "Scanner exited with code $SCAN_EXIT — check log above for details."
fi

# ── Send daily digest ─────────────────────────────────────────────────────────
log "--- Daily digest start ---"
"$PYTHON" -m chart_analyzer.alerts >> "$LOG_FILE" 2>&1
DIGEST_EXIT=$?

if [ $DIGEST_EXIT -eq 0 ]; then
    log "Daily digest sent."
else
    log "Daily digest exited with code $DIGEST_EXIT."
fi

log "Run complete."
log "========================================================"
exit 0
