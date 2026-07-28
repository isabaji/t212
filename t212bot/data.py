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


def fetch_intraday(symbols: list[str], period: str = "5d", interval: str = "5m") -> dict[str, pd.DataFrame]:
    """Return {symbol: intraday OHLCV DataFrame}, index tz-aware in the exchange's local time.

    5 days of 5-minute bars gives enough history to warm up EMA/RSI while still
    being cheap; yfinance only retains a limited intraday history anyway.
    """
    return fetch_history(symbols, period=period, interval=interval)


def fetch_fx_rate(account_currency: str, instrument_currency: str = "USD") -> float:
    """Factor to multiply an instrument_currency price by to get account_currency.

    Yahoo Finance quotes US equities in USD; a Trading212 account can be
    denominated in any currency (e.g. GBP). Position sizing and P&L math need
    everything in one currency, so live prices get converted to the account's
    currency before use. Returns 1.0 (no-op) when they already match.

    Raises if no rate can be fetched, rather than silently falling back to a
    1.0 factor -- a missing FX rate should fail the cycle loudly, not size a
    real order as if two different currencies were the same number.
    """
    if account_currency == instrument_currency:
        return 1.0
    pair = f"{account_currency}{instrument_currency}=X"
    df = yf.Ticker(pair).history(period="5d", interval="1d", auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"No FX data for {pair}")
    quote = float(df["Close"].iloc[-1])  # instrument_currency per 1 account_currency
    return 1.0 / quote
