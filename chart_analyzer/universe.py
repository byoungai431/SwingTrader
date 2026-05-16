"""
Full S&P 500 universe (~503 tickers).
Cached in pattern_universe table; refreshes automatically if older than 7 days.
"""

from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

from chart_analyzer.history import write_universe, get_universe_tickers, get_universe_refresh_date


_CACHE_DAYS = 7


def _fetch_sp500_tickers() -> list:
    """Scrape current S&P 500 constituents from Wikipedia."""
    import urllib.request
    from io import StringIO
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36"
    })
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8")
    df = pd.read_html(StringIO(html), header=0)[0]
    tickers = df["Symbol"].tolist()
    tickers = [t.replace(".", "-") for t in tickers]
    return tickers


def compute_universe(write_db: bool = True) -> list:
    """
    Fetch all S&P 500 tickers, download 30d OHLCV to compute dollar volume
    ranking, and return the full list sorted by dollar volume descending.
    """
    print("Universe: fetching S&P 500 ticker list...")
    tickers = _fetch_sp500_tickers()
    print(f"Universe: downloading 30d data for {len(tickers)} tickers...")

    raw = yf.download(
        tickers,
        period="30d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    close  = raw["Close"]  if "Close"  in raw.columns else raw.xs("Close",  axis=1, level=0)
    volume = raw["Volume"] if "Volume" in raw.columns else raw.xs("Volume", axis=1, level=0)

    dollar_vol = (close * volume).tail(20).mean()
    dollar_vol = dollar_vol.dropna().sort_values(ascending=False)

    top100      = dollar_vol.head(100)
    ticker_list = list(top100.index)
    dvol_list   = list(top100.values)

    print(f"Universe: {len(ticker_list)} tickers. #1 = {ticker_list[0]}, last = {ticker_list[-1]}")

    if write_db:
        write_universe(ticker_list, dvol_list)
        print("Universe: written to pattern_universe table.")

    return ticker_list


def get_universe() -> list:
    """
    Return cached full S&P 500 universe if refreshed within the last 7 days.
    Otherwise recompute from scratch.
    """
    refresh_date = get_universe_refresh_date()
    if refresh_date:
        try:
            last = datetime.strptime(refresh_date[:19], "%Y-%m-%d %H:%M:%S")
            if datetime.utcnow() - last < timedelta(days=_CACHE_DAYS):
                tickers = get_universe_tickers()
                if tickers:
                    return tickers
        except Exception:
            pass

    return compute_universe(write_db=True)


if __name__ == "__main__":
    universe = compute_universe(write_db=False)
    print(f"\nTotal: {len(universe)}")
    print("Top 10:")
    for i, t in enumerate(universe[:10], 1):
        print(f"  {i:2d}. {t}")
