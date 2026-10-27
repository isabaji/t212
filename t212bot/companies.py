"""Company names per ticker, for the news relevance filter (t212bot/news.py).

Yahoo's per-ticker RSS feed is "stories Yahoo associates with this ticker",
which in practice includes sector round-ups, listicles and stories about
other companies entirely -- the first live run filed Netflix, Qualcomm and
TSMC headlines under AAPL. A headline that never names the company (or its
ticker) can't be news *about* it, so it's dropped before scoring.

Static, like sectors.py: covers the default watchlist; add more via the
NEWS_COMPANY_NAMES env var ("TICK=Name|Other name;TICK2=Name") or extend
the dict. A ticker with no entry falls back to ticker-only matching, which
needs the ticker written in upper case as its own token (e.g. "(AMD)"); one-
and two-letter tickers are never matched as words (V, MA, KO, BA, HD, PG,
CAT would otherwise match ordinary English), so they rely on names alone.
"""

from __future__ import annotations

import re

COMPANY_NAMES: dict[str, list[str]] = {
    "AAPL": ["Apple", "iPhone", "iPad", "Mac"],
    "MSFT": ["Microsoft", "Azure", "Windows", "Xbox"],
    "GOOGL": ["Alphabet", "Google", "YouTube", "Waymo"],
    "AMZN": ["Amazon", "AWS"],
    "NVDA": ["Nvidia", "NVIDIA"],
    "META": ["Meta Platforms", "Meta", "Facebook", "Instagram", "WhatsApp"],
    "TSLA": ["Tesla", "Musk"],
    "JPM": ["JPMorgan", "JP Morgan", "J.P. Morgan", "Jamie Dimon"],
    "V": ["Visa"],
    "UNH": ["UnitedHealth", "United Health", "Optum"],
    "HD": ["Home Depot"],
    "XOM": ["Exxon", "ExxonMobil", "Exxon Mobil"],
    "JNJ": ["Johnson & Johnson", "Johnson and Johnson", "J&J"],
    "PG": ["Procter & Gamble", "Procter and Gamble", "P&G"],
    "DIS": ["Disney", "ESPN", "Pixar", "Marvel"],
    "AVGO": ["Broadcom"],
    "CRM": ["Salesforce"],
    "NFLX": ["Netflix"],
    "NKE": ["Nike"],
    "MCD": ["McDonald's", "McDonalds", "McDonald"],
    "BAC": ["Bank of America", "BofA"],
    "MA": ["Mastercard", "MasterCard"],
    "PFE": ["Pfizer"],
    "ABBV": ["AbbVie", "Humira", "Skyrizi", "Rinvoq"],
    "CVX": ["Chevron"],
    "KO": ["Coca-Cola", "Coca Cola", "Coke"],
    "WMT": ["Walmart", "Wal-Mart", "Sam's Club"],
    "BA": ["Boeing", "737", "787", "Dreamliner"],
    "CAT": ["Caterpillar"],
    "NEE": ["NextEra", "Florida Power"],
}

_MIN_TICKER_LEN = 3


def parse_extra(spec: str) -> dict[str, list[str]]:
    """'AMD=Advanced Micro Devices|AMD;TSM=TSMC|Taiwan Semiconductor' -> dict."""
    out: dict[str, list[str]] = {}
    for part in (spec or "").split(";"):
        if "=" not in part:
            continue
        tick, names = part.split("=", 1)
        names = [n.strip() for n in names.split("|") if n.strip()]
        if tick.strip() and names:
            out[tick.strip().upper()] = names
    return out


def names_for(extra: str | None = None) -> dict[str, list[str]]:
    merged = {k: list(v) for k, v in COMPANY_NAMES.items()}
    for tick, names in parse_extra(extra or "").items():
        merged[tick] = names + merged.get(tick, [])
    return merged


def _name_pattern(name: str) -> re.Pattern:
    # Whole-word match, case-insensitive, tolerant of possessives ("Apple's").
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?:'s)?(?![A-Za-z0-9])", re.IGNORECASE)


_pattern_cache: dict[str, re.Pattern] = {}


def is_relevant(symbol: str, title: str, names: dict[str, list[str]] | None = None) -> bool:
    """True if the headline names the company (any alias, case-insensitive,
    whole word) or its ticker (upper case, own token, >= 3 letters).
    A symbol with no aliases and a short ticker can't be judged -> True."""
    names = COMPANY_NAMES if names is None else names
    symbol = symbol.upper()
    aliases = names.get(symbol, [])
    for alias in aliases:
        pat = _pattern_cache.get(alias)
        if pat is None:
            pat = _pattern_cache[alias] = _name_pattern(alias)
        if pat.search(title):
            return True
    if len(symbol) >= _MIN_TICKER_LEN:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(symbol) + r"(?![A-Za-z0-9])", title):
            return True
        return False
    return not aliases
