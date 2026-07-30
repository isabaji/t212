"""Small, dependency-free technical indicators used by intraday strategies."""

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).fillna(100)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored volume-weighted average price -- resets each
    calendar day (assumes df's index is tz-aware exchange-local, consistent
    with the rest of the intraday pipeline; see fetch_intraday). Unlike ema,
    this incorporates traded volume directly rather than just price."""
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = typical_price * df["Volume"]
    day = df.index.date
    cum_pv = pv.groupby(day).cumsum()
    cum_vol = df["Volume"].groupby(day).cumsum()
    return cum_pv / cum_vol


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — a volatility measure in price units (e.g. dollars/share),
    used for volatility-aware position sizing rather than a flat % of account per trade."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(period).mean()
