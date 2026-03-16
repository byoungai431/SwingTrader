import yfinance as yf
import pandas as pd
import streamlit as st
from config import WATCHLIST, HISTORY_DAYS


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_price_data(ticker: str) -> pd.DataFrame:
    """Fetch OHLCV price history for a single ticker. Cached for 1 hour."""
    stock = yf.Ticker(ticker)
    df = stock.history(period=HISTORY_DAYS)
    df.index = pd.to_datetime(df.index)
    return df


def fetch_all() -> dict:
    """Fetch price data for all tickers in the watchlist."""
    data = {}
    for ticker in WATCHLIST:
        try:
            df = fetch_price_data(ticker)
            if not df.empty:
                data[ticker] = df
                print(f"  {ticker}: {len(df)} rows fetched")
            else:
                print(f"  {ticker}: no data returned")
        except Exception as e:
            print(f"  {ticker}: error — {e}")
    return data


if __name__ == "__main__":
    print("Fetching price data...")
    data = fetch_all()
    print(f"\nDone. {len(data)} tickers loaded.")
    for ticker, df in data.items():
        print(f"  {ticker}: latest close = ${df['Close'].iloc[-1]:.2f}")
