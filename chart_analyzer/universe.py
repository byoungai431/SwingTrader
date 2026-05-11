"""
Top-100 S&P 500 universe by 20-day average dollar volume.
Cached in pattern_universe table; refreshes automatically if older than 7 days.
"""

from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf

from chart_analyzer.history import write_universe, get_universe_tickers, get_universe_refresh_date


_CACHE_DAYS = 7


def _fetch_sp500_tickers() -> list:
    """Scrape current S&P 500 constituents from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    tables = pd.read_html(url, header=0)
    df = tables[0]
    tickers = df["Symbol"].tolist()
    # yfinance uses dash for class-B shares (BRK-B, BF-B)
    tickers = [t.replace(".", "-") for t in tickers]
    return tickers


def compute_top100(write_db: bool = True) -> list:
    """
    Download 30-day OHLCV for all S&P 500 tickers, compute 20-day average
    dollar volume (close × volume), return top-100 sorted descending.
    Optionally writes results to pattern_universe table.
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

    close = raw["Close"] if "Close" in raw.columns else raw.xs("Close", axis=1, level=0)
    volume = raw["Volume"] if "Volume" in raw.columns else raw.xs("Volume", axis=1, level=0)

    dollar_vol = (close * volume).tail(20).mean()
    dollar_vol = dollar_vol.dropna().sort_values(ascending=False)

    top100 = dollar_vol.head(100)
    ticker_list = list(top100.index)
    dvol_list = list(top100.values)

    print(f"Universe: top-100 computed. #1 = {ticker_list[0]}, #100 = {ticker_list[-1]}")

    if write_db:
        write_universe(ticker_list, dvol_list)
        print("Universe: written to pattern_universe table.")

    return ticker_list


def get_universe() -> list:
    """
    Return the cached top-100 universe if refreshed within the last 7 days.
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

    return compute_top100(write_db=True)


if __name__ == "__main__":
    universe = compute_top100(write_db=False)
    print("\nTop 10:")
    for i, t in enumerate(universe[:10], 1):
        print(f"  {i:2d}. {t}")
