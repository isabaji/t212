"""Market data via Yahoo Finance, and Yahoo <-> Trading212 ticker mapping.

Trading212's API has no real-time quote endpoint, so signals are computed from
an external feed. Yahoo data is delayed — fine for daily/hourly strategies, not
for high-frequency ones.
"""

import logging

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# Trading212 tickers look like "AAPL_US_EQ". US equities map mechanically;
# add explicit entries here for LSE/EU listings (e.g. "VOD.L": "VODl_EQ").
YAHOO_TO_T212: dict[str, str] = {}


def to_t212_ticker(yahoo_symbol: str) -> str:
    return YAHOO_TO_T212.get(yahoo_symbol, f"{yahoo_symbol}_US_EQ")


def to_yahoo_symbol(t212_ticker: str) -> str:
    for y, t in YAHOO_TO_T212.items():
        if t == t212_ticker:
            return y
    return t212_ticker.split("_")[0]


def fetch_history(symbols: list[str], period: str = "1y", interval: str = "1d") -> dict[str, pd.DataFrame]:
    """Return {symbol: OHLCV DataFrame} for each symbol that has data."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            log.warning("No data for %s, skipping", sym)
            continue
        out[sym] = df
    return out
