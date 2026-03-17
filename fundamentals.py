import time
import yfinance as yf
import streamlit as st


def _fetch_info(ticker: str) -> dict:
    """Fetch yfinance info with up to 2 retries if data comes back sparse."""
    for attempt in range(2):
        try:
            info = yf.Ticker(ticker).info or {}
            if len(info) >= 10:
                return info
        except Exception:
            pass
        if attempt == 0:
            time.sleep(1)
    return {}


def _build_result(info: dict) -> dict:
    def safe(key, scale=1, decimals=2):
        val = info.get(key)
        if val is None or val == "Infinity":
            return None
        try:
            return round(float(val) * scale, decimals)
        except (TypeError, ValueError):
            return None

    pe     = safe("trailingPE")
    fwd_pe = safe("forwardPE")
    peg    = safe("pegRatio")
    roe    = safe("returnOnEquity", 100)
    de     = safe("debtToEquity")
    fcf_b  = safe("freeCashflow", 1e-9)

    company_name = info.get("longName") or info.get("shortName") or ""
    sector       = info.get("sector") or ""
    industry     = info.get("industry") or ""
    summary_raw  = info.get("longBusinessSummary") or ""
    sentences = summary_raw.replace("  ", " ").split(". ")
    company_description = ". ".join(sentences[:2]).strip()
    if company_description and not company_description.endswith("."):
        company_description += "."

    strong_count = 0
    if peg is not None and peg < 1:      strong_count += 1
    if roe is not None and roe > 15:     strong_count += 1
    if fcf_b is not None and fcf_b > 0:  strong_count += 1

    return {
        "pe":                  pe,
        "fwd_pe":              fwd_pe,
        "peg":                 peg,
        "roe":                 roe,
        "de":                  de,
        "fcf_b":               fcf_b,
        "fundamentals_strong": strong_count >= 2,
        "company_name":        company_name,
        "sector":              sector,
        "industry":            industry,
        "company_description": company_description,
    }


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fundamentals(ticker: str) -> dict:
    """Fetch key fundamental metrics for a ticker using yfinance."""
    info = _fetch_info(ticker)
    return _build_result(info)


if __name__ == "__main__":
    import json
    for t in ["AAPL", "MSFT", "TSLA", "COHR"]:
        print(f"\n{t}:", json.dumps(fetch_fundamentals(t), indent=2))
