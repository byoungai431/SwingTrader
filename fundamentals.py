import requests
import streamlit as st


def _get_finnhub_key() -> str:
    try:
        return st.secrets["FINNHUB_API_KEY"]
    except Exception:
        return ""


def _fetch_finnhub(ticker: str, api_key: str) -> dict:
    params = {"symbol": ticker, "token": api_key}
    try:
        r1 = requests.get("https://finnhub.io/api/v1/stock/metric",
                          params={**params, "metric": "all"}, timeout=10)
        metrics = r1.json().get("metric", {}) if r1.ok else {}
    except Exception:
        metrics = {}
    try:
        r2 = requests.get("https://finnhub.io/api/v1/stock/profile2",
                          params=params, timeout=10)
        profile = r2.json() if r2.ok else {}
    except Exception:
        profile = {}
    try:
        r3 = requests.get("https://finnhub.io/api/v1/stock/financials-reported",
                          params={**params, "freq": "annual"}, timeout=10)
        reports = r3.json().get("data", []) if r3.ok else []
        cf = reports[0].get("report", {}).get("cf", []) if reports else []
        fcf = None
        op_cf = next((x["value"] for x in cf if x.get("concept") == "NetCashProvidedByUsedInOperatingActivities"), None)
        capex = next((x["value"] for x in cf if x.get("concept") == "PaymentsToAcquirePropertyPlantAndEquipment"), None)
        if op_cf is not None and capex is not None:
            fcf = op_cf - capex
    except Exception:
        fcf = None

    return {"metrics": metrics, "profile": profile, "fcf": fcf}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fundamentals(ticker: str) -> dict:
    """Fetch key fundamental metrics via Finnhub API."""
    api_key = _get_finnhub_key()
    data = _fetch_finnhub(ticker, api_key) if api_key else {"metrics": {}, "profile": {}, "fcf": None}

    m = data["metrics"]
    p = data["profile"]

    def safe(val, scale=1, decimals=2):
        if val is None:
            return None
        try:
            return round(float(val) * scale, decimals)
        except (TypeError, ValueError):
            return None

    pe     = safe(m.get("peTTM"))
    fwd_pe = safe(m.get("forwardPE")) or safe(m.get("peNormalizedAnnual"))
    peg    = safe(m.get("pegRatio"))
    roe    = safe(m.get("roeTTM"))        # already in percent
    de     = safe(m.get("totalDebt/totalEquityAnnual"))
    fcf_b  = safe(data["fcf"], 1e-9) if data["fcf"] else safe(m.get("freeCashFlowTTM"), 1e-9)

    company_name = p.get("name") or ""
    sector       = p.get("finnhubIndustry") or ""
    industry     = p.get("finnhubIndustry") or ""
    summary_raw  = p.get("description") or ""  # not always available
    sentences    = summary_raw.replace("  ", " ").split(". ")
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
        print(f"\n{t}:", json.dumps(_fetch_finnhub(t, key), indent=2))
