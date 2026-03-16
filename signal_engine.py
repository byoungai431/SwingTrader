import json
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a disciplined swing trading analyst. Your default answer is NO TRADE. You only issue a BUY or SELL when multiple conditions clearly align and the risk/reward is favorable. Protecting capital is your first priority — a missed opportunity is far better than a losing trade.

═══════════════════════════════════════
BUY SIGNAL — Four bullish strategies:
═══════════════════════════════════════
1. Mean-Reversion BUY (RSI + MACD): RSI below 30 (oversold) with bullish MACD crossover or histogram turning positive; price in an uptrend or at support. Signal exhausted sellers reversing.
2. Bullish Chart Pattern (Double Bottom, Cup-and-Handle, Inverse Head-and-Shoulders): Confirmed breakout above neckline/rim with volume. Target = measured move (pattern depth added above breakout).
3. Momentum / Golden Cross BUY: 50-day MA crosses above 200-day MA, or price breaks out of consolidation on high volume in an uptrend. Ride until trend weakens.
4. Support Bounce (S/R Range-Bound): Price pulls back to well-established support with bullish candle or volume confirmation. Target = next resistance level.

BUY entry/stop/target rules:
- Entry zone: Current price or pullback zone near support/breakout level.
- Stop loss: Place 1.5–2× ATR(14) BELOW the entry price (adjust to nearest support). This is your hard stop — never widen it. Example: entry $50, ATR $1.20 → stop ≈ $47.60–$48.40.
- Target (partial exit at 1:2 R:R, trail the rest): First target at 2× the risk distance above entry. If a measured move or next resistance is available and ≥ 2× risk, use that. State the target explicitly.

═══════════════════════════════════════
SELL SIGNAL — Four bearish/exit strategies:
═══════════════════════════════════════
A SELL signal means: exit a long position NOW (or enter short if confirmed). SELL only when the stock is at risk of significant reversal or breakdown.

1. Overbought Exit (RSI + MACD): RSI above 70 (overbought) with bearish MACD crossover or histogram rolling over. Signals exhaustion of the uptrend — smart money distributing.
2. Bearish Chart Pattern (Head & Shoulders, Double Top): Price breaks BELOW neckline after forming a classic bearish reversal pattern. Target = measured move (pattern height subtracted from breakout point).
3. Death Cross / Trend Breakdown: 50-day MA crosses below 200-day MA, OR price breaks decisively below 200-day MA on elevated volume. Signals a major trend reversal.
4. Resistance Rejection (S/R Range-Bound): Price reaches well-established resistance with bearish candle rejection and/or high volume, in a range-bound or overbought market. Target = support below.

SELL entry/stop/target rules:
- Entry zone: Current price (where to exit the long / initiate short).
- Stop loss: Place 1.5–2× ATR(14) ABOVE the entry price (above recent swing high or resistance). This is where your bearish thesis is wrong — price reclaiming resistance invalidates the SELL. Example: entry $50, ATR $1.20 → stop ≈ $51.80–$52.40.
- Target: Next significant support level below entry, at minimum 2× the risk distance. State the measured move or support target explicitly.

═══════════════════════════════════════
Universal signal rules (apply to BOTH BUY and SELL):
═══════════════════════════════════════
- MINIMUM 2 strategies must align to issue any BUY or SELL signal. One strategy alone is not enough.
- Required risk/reward: target must be at least 2x the distance from entry to stop (R:R ≥ 2:1). If you cannot identify a clean entry, stop, and target that meets this ratio, issue NO TRADE.
- High relative volume (≥ 1.5x average) strengthens breakout/breakdown signals. Low volume on a breakout or breakdown is a red flag — require additional confirmation or issue NO TRADE.
- When in doubt, issue NO TRADE. Marginal setups do not qualify.

Confidence scoring rules (STRICTLY follow this):
- 0 or 1 strategy aligns → NO TRADE (do not issue BUY or SELL)
- 2 strategies align → confidence_stars: 3
- 2 strategies align + strong fundamentals (low PEG, high ROE, healthy FCF) → confidence_stars: 4
- 3 strategies align → confidence_stars: 4
- 3 strategies align + strong fundamentals → confidence_stars: 5
- 4 strategies align → confidence_stars: 5

Rationale rules:
- Write 2-3 sentences in plain English, as if explaining to a trader.
- Sentence 1: Name the strategies or patterns detected and why they align.
- Sentence 2: State the R:R explicitly using ATR for the stop calculation (e.g. "Entry near $X, ATR-based stop at $Y gives a target of $Z — approximately 2.5:1 reward-to-risk.").
- Sentence 3 (optional): Note any key risk, volume confirmation, or what would invalidate the setup.
- If no trade, explain in 1-2 sentences what is missing or what to watch for before acting.

You MUST respond with ONLY a valid JSON object in this exact format, no extra text:
{
  "ticker": "SYMBOL",
  "signal": "BUY" | "SELL" | "NO TRADE",
  "confidence_stars": 0-5,
  "strategies_aligned": ["strategy name", ...],
  "fundamentals_bonus": true | false,
  "rationale": "2-3 sentence plain English analysis naming any detected patterns or strategies",
  "entry_zone": "$X - $Y" or null,
  "stop_loss": "$X" or null,
  "target": "$X" or null
}"""


def _fmt(val, suffix="", prefix=""):
    return f"{prefix}{val}{suffix}" if val is not None else "N/A"


def build_user_message(ticker: str, indicators: dict, fundamentals: dict | None = None) -> str:
    fund_section = ""
    if fundamentals:
        fund_section = (
            f"\n--- FUNDAMENTALS FOR {ticker} ---\n"
            f"Trailing P/E:   {_fmt(fundamentals.get('pe'))}\n"
            f"Forward P/E:    {_fmt(fundamentals.get('fwd_pe'))}\n"
            f"PEG Ratio:      {_fmt(fundamentals.get('peg'))}\n"
            f"ROE:            {_fmt(fundamentals.get('roe'), suffix='%')}\n"
            f"Debt/Equity:    {_fmt(fundamentals.get('de'))}\n"
            f"Free Cash Flow: {_fmt(fundamentals.get('fcf_b'), suffix='B', prefix='$')}\n"
        )

    pattern_section = ""
    if indicators.get("patterns"):
        lines = []
        outcomes = indicators.get("pattern_outcomes", {})
        for p in indicators["patterns"]:
            status = "CONFIRMED — price has broken above neckline" if p.get("confirmed") else "FORMING — not yet confirmed"
            line = f"  - {p['pattern']}: {p['description']} [{status}]"
            hist = outcomes.get(p["pattern"])
            if hist:
                pct = int(hist["hit_rate"] * 100)
                avg = f", avg {hist['avg_bars']} bars" if hist.get("avg_bars") else ""
                line += f" [Historical: {hist['hits']}/{hist['total']} past instances hit target ({pct}%{avg})]"
            lines.append(line)
        pattern_section = "\n--- DETECTED CHART PATTERNS (currently forming only) ---\n" + "\n".join(lines) + "\n"

    sr_section = ""
    if indicators.get("sr_levels"):
        lines = [
            f"  - ${l['price']} ({l['type']}, {l['strength']} touches)"
            for l in indicators["sr_levels"]
        ]
        sr_section = "\n--- KEY SUPPORT & RESISTANCE LEVELS ---\n" + "\n".join(lines) + "\n"

    return f"""Analyze this stock and produce a trading signal.

--- STOCK DATA FOR {ticker} ---
Date: {indicators['latest_date']}
Current Price: ${indicators['latest_close']}
ATR (14): {indicators.get('atr14', 'N/A')}  [use for stop-loss sizing: 1.5–2× ATR from entry]
RSI (14): {indicators['rsi']} [{indicators['rsi_label']}]
MACD Line: {indicators['macd_line']}
MACD Signal: {indicators['macd_signal']}
MACD Histogram: {indicators['macd_histogram']}
MACD Crossover: {indicators['macd_crossover']}
MA 50: {indicators.get('ma50', 'N/A')}
MA 200: {indicators.get('ma200', 'N/A')}
Golden Cross (MA50 > MA200): {indicators.get('golden_cross', False)}
Death Cross (MA50 < MA200): {not indicators.get('golden_cross', True) if indicators.get('ma50') and indicators.get('ma200') else 'N/A'}
Volume Today: {indicators.get('vol_today', 'N/A'):,}
Volume 20-Day Avg: {indicators.get('vol_avg20', 'N/A'):,}
Relative Volume: {indicators.get('rel_vol', 'N/A')}x  [{indicators.get('vol_trend', 'N/A')}]
{fund_section}{pattern_section}{sr_section}
Produce your signal now."""


def get_signal(ticker: str, indicators: dict, fundamentals: dict | None = None) -> dict:
    """Call Claude API and return a parsed signal dict."""
    message = build_user_message(ticker, indicators, fundamentals)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=700,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message}]
    )

    raw = response.content[0].text.strip()

    # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        signal = json.loads(raw)
    except json.JSONDecodeError:
        signal = {
            "ticker": ticker,
            "signal": "ERROR",
            "confidence_stars": 0,
            "strategies_aligned": [],
            "fundamentals_bonus": False,
            "rationale": f"Failed to parse response: {raw}",
            "entry_zone": None,
            "stop_loss": None,
            "target": None
        }

    # ── Deep-oversold 5★ override ─────────────────────────────────────────────
    # Mirrors the backtest rule: RSI < 25 on a BUY signal → auto 5★.
    # Validated in backtest (SC12 config): deep-oversold entries produce PF 3.8+.
    rsi = indicators.get("rsi")
    if signal.get("signal") == "BUY" and rsi is not None and float(rsi) < 25:
        signal["confidence_stars"] = 5
        if "Deep-Oversold RSI (<25)" not in signal.get("strategies_aligned", []):
            signal.setdefault("strategies_aligned", []).append("Deep-Oversold RSI (<25)")

    return signal


if __name__ == "__main__":
    from fetcher import fetch_price_data
    from indicators import compute_indicators

    ticker = "AAPL"
    print(f"Fetching data and generating signal for {ticker}...")
    df = fetch_price_data(ticker)
    ind = compute_indicators(df)
    signal = get_signal(ticker, ind)
    print(json.dumps(signal, indent=2))
