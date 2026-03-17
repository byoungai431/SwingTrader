import requests
import streamlit as st


def _get_fmp_key() -> str:
    try:
        return st.secrets["FMP_API_KEY"]
    except Exception:
        return ""


def _fetch_fmp(ticker: str, api_key: str) -> dict:
    base = "https://financialmodelingprep.com/api/v3"
    params = {"apikey": api_key}
    try:
        r1 = requests.get(f"{base}/key-metrics-ttm/{ticker}", params=params, timeout=10)
        metrics = r1.json()[0] if r1.ok and isinstance(r1.json(), list) and r1.json() else {}
    except Exception:
        metrics = {}
    try:
        r2 = requests.get(f"{base}/profile/{ticker}", params=params, timeout=10)
        profile = r2.json()[0] if r2.ok and isinstance(r2.json(), list) and r2.json() else {}
    except Exception:
        profile = {}
    try:
        r3 = requests.get(f"{base}/cash-flow-statement/{ticker}", params={**params, "limit": 1}, timeout=10)
        cf = r3.json()[0] if r3.ok and isinstance(r3.json(), list) and r3.json() else {}
    except Exception:
        cf = {}
    return {**metrics, **profile, **cf}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fundamentals(ticker: str) -> dict:
    """Fetch key fundamental metrics via Financial Modeling Prep API."""
    api_key = _get_fmp_key()
    data = _fetch_fmp(ticker, api_key) if api_key else {}

    def safe(key, scale=1, decimals=2):
        val = data.get(key)
        if val is None or val == 0:
            return None
        try:
            return round(float(val) * scale, decimals)
        except (TypeError, ValueError):
            return None

    pe     = safe("peRatioTTM")
    fwd_pe = safe("forwardPE") or safe("priceEarningsRatioTTM")
    peg    = safe("pegRatioTTM")
    roe    = safe("roeTTM", 100)          # decimal → percent
    de     = safe("debtToEquityTTM")
    fcf_b  = safe("freeCashFlow", 1e-9)   # raw dollars → $B

    company_name        = data.get("companyName") or data.get("name") or ""
    sector              = data.get("sector") or ""
    industry            = data.get("industry") or ""
    summary_raw         = data.get("description") or ""
    sentences           = summary_raw.replace("  ", " ").split(". ")
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


if __name__ == "__main__":
    import json, sys
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    for t in ["AAPL", "MSFT", "TSLA", "COHR"]:
        print(f"\n{t}:", json.dumps(_fetch_fmp(t, key), indent=2))
