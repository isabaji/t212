"""Rough GICS-style sector classification for the default watchlist.

Used only for portfolio-level concentration limits (t212bot/risk.py) — e.g.
so the bot doesn't happily fill five slots with names that all move together
on the same sector headline. This is a static lookup table, not a live data
feed, so it only covers the default WATCHLIST; anything else falls back to
"Other" and is treated as its own single-symbol bucket.
"""

SECTOR_MAP = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "AVGO": "Technology",
    "CRM": "Technology",
    "GOOGL": "Communication Services",
    "META": "Communication Services",
    "DIS": "Communication Services",
    "NFLX": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",
    "JPM": "Financials",
    "V": "Financials",
    "BAC": "Financials",
    "MA": "Financials",
    "UNH": "Healthcare",
    "JNJ": "Healthcare",
    "PFE": "Healthcare",
    "ABBV": "Healthcare",
    "XOM": "Energy",
    "CVX": "Energy",
    "PG": "Consumer Staples",
    "KO": "Consumer Staples",
    "WMT": "Consumer Staples",
    "BA": "Industrials",
    "CAT": "Industrials",
    "NEE": "Utilities",
}


def sector_of(symbol: str) -> str:
    known = SECTOR_MAP.get(symbol.upper())
    if known:
        return known
    return f"Other:{symbol.upper()}"
