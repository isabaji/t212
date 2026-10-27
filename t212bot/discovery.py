"""Market-wide news discovery: scan many general finance feeds, work out
which companies the headlines are about, and hand the news cycle a
*dynamic* universe -- whatever is in the news today -- instead of a fixed
watchlist.

Two phases in run_news_cycle (t212bot/bot.py):
1. Broad scan (this module): DEFAULT_FEEDS (CNBC, MarketWatch, Yahoo
   Finance, Seeking Alpha, Investing.com, Nasdaq, press-release wires,
   Google News business) plus targeted Google News searches for the event
   phrases the strategy cares about ("raises guidance", "misses
   estimates", "FDA approval", ...). Every headline is resolved to the
   tickers it names -- explicit "(NYSE: XYZ)" / "$XYZ" mentions first
   (press releases always carry them), then company names from the SEC's
   public ticker list (top NEWS_SEC_TOP_N by the file's market-cap order)
   and the curated aliases in companies.py. Tickers named in at least
   NEWS_MIN_MENTIONS distinct headlines (one is enough when the ticker
   was explicit) become candidates, ranked by mention count, capped at
   NEWS_MAX_CANDIDATES.
2. Depth (existing code): each candidate's own Yahoo feed is pulled and
   merged with the headlines that surfaced it, then scored, archived and
   traded exactly as before. Positions already held are always included.

Guard rails before a discovered name can be *bought*: it must be
tradable on Trading212 (client.instruments()), and pass a liquidity check
(market cap and average dollar volume via yfinance, cached weekly in
state/universe.json as a yes/no -- no figures). Watchlist symbols are
exempt from the liquidity check so a yfinance hiccup can't block them.

Name matching is deliberately conservative: single-word names must be
>= 4 letters and not generic English ("Target", "Gap", "Block", "First",
"General" are all real companies and all poison); first-token shortcuts
("JPMorgan" for "JPMorgan Chase") need >= 6 letters; when two companies
share an alias the larger one (earlier in the SEC file) keeps it. Missing
a mention costs a trade; a false match costs a wrong trade.

Feed URLs are plain RSS and can rot; each is fetched independently, a
failing one logs a warning and the rest carry on, and NEWS_FEEDS can
replace or extend the list without a code change.
"""

from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

from .news import Headline, _HEADERS, _REQUEST_TIMEOUT, _normalize_title, _parse_rss

log = logging.getLogger(__name__)

UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "state" / "universe.json"

DEFAULT_FEEDS: dict[str, str] = {
    "cnbc-top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "cnbc-earnings": "https://www.cnbc.com/id/15839135/device/rss/rss.html",
    "cnbc-finance": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "marketwatch-top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "marketwatch-realtime": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "yahoo-finance": "https://finance.yahoo.com/news/rssindex",
    "seekingalpha-news": "https://seekingalpha.com/market_currents.xml",
    "investing-stocks": "https://www.investing.com/rss/news_25.rss",
    "nasdaq-markets": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "nasdaq-earnings": "https://www.nasdaq.com/feed/rssoutbound?category=Earnings",
    "prnewswire-financial": "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    "globenewswire-public": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "gnews-business": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
}
# Targeted Google News searches -- exactly the event types NewsSentiment
# and the event study are built around. `when:1d` keeps them fresh.
GNEWS_EVENT_QUERIES = [
    "raises guidance", "cuts guidance", "raises outlook", "lowers outlook", "beats estimates",
    "misses estimates", "FDA approval", "downgraded to sell", "upgraded to buy", "agrees to acquire",
    "CEO steps down", "record quarter", "profit warning", "wins contract",
]
GNEWS_SEARCH_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
_FEED_DELAY_SECONDS = 0.2

# Company-name suffixes stripped (repeatedly) from the end of SEC titles.
_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "cos", "ltd", "limited", "plc",
    "llc", "lp", "l p", "nv", "n v", "sa", "s a", "ag", "se", "holdings", "holding", "group", "trust", "fund",
    "class", "a", "b", "c", "new", "de", "the", "and", "common", "stock", "shares", "ordinary", "adr", "ads",
}
# Words that are real company names but far too common as English to match on their own.
GENERIC_WORDS = {
    "target", "gap", "block", "snap", "match", "unity", "core", "first", "general", "american", "united",
    "national", "international", "global", "energy", "capital", "financial", "digital", "health", "bank",
    "western", "southern", "northern", "eastern", "pacific", "atlantic", "standard", "universal", "advanced",
    "applied", "consolidated", "public", "service", "services", "systems", "technologies", "technology",
    "resources", "industries", "brands", "foods", "motors", "medical", "realty", "properties", "partners",
    "power", "water", "gold", "silver", "oil", "gas", "steel", "copper", "air", "auto", "home", "life", "world",
    "best", "big", "main", "street", "central", "citizens", "people", "progressive", "discover", "visa",
    "mobile", "live", "nation", "republic", "liberty", "freedom", "pioneer", "frontier", "summit", "peak",
    "apex", "prime", "premier", "royal", "crown", "eagle", "star", "sun", "moon", "ocean", "river", "lake",
    "valley", "mountain", "forest", "green", "blue", "red", "black", "white", "orange", "coca", "box", "yelp",
    "fox", "wayfair", "hub", "anywhere", "beyond", "carter", "chase", "church", "dollar", "equity", "extra",
    "federal", "fifth", "fortune", "franklin", "generation", "growth", "income", "insight", "interactive",
    "jack", "kinder", "lincoln", "lumen", "madison", "monster", "paramount", "patterson", "regal", "science",
    "select", "signature", "sterling", "stride", "teradata", "total", "triumph", "victory", "virtu", "vista",
    # first-token shortcuts that are places, people, or everyday words
    "delta", "alaska", "texas", "boston", "southwest", "norwegian", "taiwan", "illinois", "norfolk", "las",
    "trump", "zoom", "workday", "snowflake", "carnival", "marathon", "waste", "automatic", "simon", "arthur",
    "take", "builders", "carrier", "super", "trade", "ross", "campbell", "tractor", "quest", "charles", "analog",
    "intuitive", "edwards", "cardinal", "ally", "travelers", "marsh", "apollo", "ares", "nasdaq", "emerson",
    "parker", "union", "booking", "constellation", "philip", "duke", "affirm", "lucid", "microchip", "charter",
    "electronic", "penn", "spirit", "baker", "devon", "williams", "mosaic", "ball", "vulcan", "martin", "tyson",
    "crown", "old", "morgan", "phillips", "dominion", "hilton", "keurig", "kraft", "hess", "warner", "sirius",
    "ford", "nike", "shell", "bp", "rio", "tinto", "ultra", "mini", "micro", "mega", "max", "plus", "one", "two",
    "three", "five", "seven", "ten", "hundred", "thousand", "million", "billion", "trillion", "north", "south",
    "east", "west", "city", "state", "county", "country", "nation", "world", "earth", "planet", "space", "sky",
    "cloud", "wind", "solar", "nuclear", "electric", "motor", "mobile", "wireless", "network", "networks",
    "software", "hardware", "computer", "computers", "machines", "devices", "semiconductor", "semiconductors",
    "pharmaceuticals", "pharma", "therapeutics", "biosciences", "sciences", "labs", "laboratories", "clinic",
    "hospital", "insurance", "assurance", "mutual", "savings", "credit", "lending", "payments", "exchange",
    "markets", "market", "stores", "store", "shop", "restaurants", "restaurant", "hotels", "hotel", "resorts",
    "airlines", "airline", "air", "lines", "cruise", "rail", "railway", "freight", "logistics", "shipping",
    "express", "delivery", "retail", "wholesale", "outlet", "outfitters", "apparel", "beauty", "fitness",
}
_EXPLICIT_TICKER_RE = re.compile(
    r"\((?:NYSE|NASDAQ|Nasdaq|NYSE American|NYSE Arca|NYSEAmerican|AMEX|CBOE|OTC(?:QB|QX|)?)\s*:\s*([A-Z]{1,5})(?:\.[A-Z])?\)"
    r"|(?<![A-Za-z0-9])\$([A-Z]{1,5})(?![A-Za-z0-9])"
)
# Bare upper-case tokens that are also tickers ("Nvidia and AMD rally"):
# accepted only for known tickers of 3+ letters, never on an all-caps
# headline, and never for these everyday acronyms that collide with real
# tickers (ALL = Allstate, AI = C3.ai, ON = ON Semi, ...).
_BARE_TICKER_RE = re.compile(r"(?<![A-Za-z0-9$])([A-Z]{3,5})(?![A-Za-z0-9])")
_BARE_TICKER_STOPLIST = {
    "ALL", "ARE", "FOR", "NEW", "NOW", "ONE", "OUT", "SEE", "TOP", "BIG", "LOW", "HOW", "WHY", "WHO", "CAN",
    "CEO", "CFO", "COO", "CTO", "GDP", "ETF", "IPO", "SEC", "FDA", "USA", "FED", "DOJ", "FTC", "FBI", "CIA",
    "NYSE", "DOW", "LIVE", "BUY", "SELL", "HOLD", "UPS", "DIP", "RUN", "HIT", "CUT", "RSS", "CNBC", "WSJ",
    "AI", "API", "EPS", "EBIT", "YOY", "QOQ", "EV", "EVS", "OPEC", "NATO", "IMF", "ECB", "BOE", "BOJ", "PCE",
    "CPI", "PPI", "PMI", "ISM", "ADP", "BLS", "GAAP", "LLC", "PLC", "INC", "CORP", "LTD", "AND", "THE",
    "WITH", "FROM", "INTO", "OVER", "MORE", "LESS", "THAN", "THIS", "THAT", "WILL", "HAS", "HAD", "ITS",
    "NOT", "BUT", "YET", "AMID", "AFTER", "WHAT", "WHEN", "JUST", "STILL", "GOOD", "BAD", "BEST", "WORST",
    "FREE", "REAL", "TRUE", "FAST", "SLOW", "SAFE", "CASH", "DEBT", "LOAN", "RATE", "BOND", "GOLD", "OIL",
    "TECH", "DATA", "CHIP", "CARS", "JOBS", "WAGE", "HOME", "LAND", "FOOD", "DRUG", "CARE", "LIFE", "PLAY",
    "ELON", "MUSK", "CHINA", "INDIA", "JAPAN", "EURO", "USD", "GBP", "JPY", "CNY", "BTC", "ETH", "UK", "EU",
}


def _mostly_upper(title: str) -> bool:
    letters = [c for c in title if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.6


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------
def feed_list(spec: str | None = None) -> list[tuple[str, str]]:
    """Feeds to scan. spec (NEWS_FEEDS): empty -> defaults + event searches;
    'name=url;name=url' -> exactly those; prefix '+' to extend the defaults
    instead ('+mine=https://...'). '-name' removes a default."""
    feeds = dict(DEFAULT_FEEDS)
    for q in GNEWS_EVENT_QUERIES:
        feeds[f"gnews:{q}"] = GNEWS_SEARCH_URL.format(q=requests.utils.quote(f'"{q}" when:1d'))
    spec = (spec or "").strip()
    if not spec:
        return list(feeds.items())
    extend = spec.startswith("+")
    custom: dict[str, str] = {}
    removals: set[str] = set()
    for part in spec.lstrip("+").split(";"):
        part = part.strip()
        if not part:
            continue
        if part.startswith("-"):
            removals.add(part[1:].strip())
        elif "=" in part:
            name, url = part.split("=", 1)
            custom[name.strip()] = url.strip()
    if extend:
        feeds.update(custom)
        return [(n, u) for n, u in feeds.items() if n not in removals]
    return list(custom.items())


def fetch_market_headlines(feeds: list[tuple[str, str]], now: datetime | None = None,
                           max_age_hours: float = 48.0,
                           session: requests.Session | None = None) -> list[Headline]:
    """All in-window headlines across the feeds, deduplicated by title.
    Headline.source is the feed name; Headline.symbol is "" (unresolved)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)
    session = session or requests.Session()
    seen: set[str] = set()
    out: list[Headline] = []
    per_feed: dict[str, int] = {}
    for i, (name, url) in enumerate(feeds):
        if i:
            time.sleep(_FEED_DELAY_SECONDS)
        try:
            resp = session.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
            if not resp.ok:
                raise RuntimeError(f"HTTP {resp.status_code}")
            items = _parse_rss(resp.text, "", "google" if "news.google.com" in url else name)
        except (requests.RequestException, RuntimeError, ET.ParseError) as exc:
            log.warning("Feed %s failed: %s", name, exc)
            continue
        kept = 0
        for h in items:
            if h.published is not None and h.published < cutoff:
                continue
            key = _normalize_title(h.title)
            if not key or key in seen:
                continue
            seen.add(key)
            h.source = name
            out.append(h)
            kept += 1
        per_feed[name] = kept
    log.info("Discovery: %d unique headlines from %d/%d feeds (%s)", len(out), len(per_feed), len(feeds),
             ", ".join(f"{n} {c}" for n, c in sorted(per_feed.items(), key=lambda x: -x[1])[:8]))
    return out


# ---------------------------------------------------------------------------
# Headline -> tickers
# ---------------------------------------------------------------------------
def _strip_suffixes(title: str) -> str:
    words = _normalize_title(title).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


class TickerResolver:
    """Phrase index: normalised alias -> ticker. Longest match wins; a
    phrase already claimed by one ticker is never reassigned."""

    def __init__(self, aliases: dict[str, list[str]]):
        self.names: dict[str, list[str]] = {t: list(a) for t, a in aliases.items()}
        self.index: dict[str, str] = {}
        self.max_words = 1
        for ticker, names in aliases.items():
            for name in names:
                key = _normalize_title(name)
                if not key or key in self.index:
                    continue
                self.index[key] = ticker
                self.max_words = max(self.max_words, len(key.split()))
        self.known = set(aliases)

    @classmethod
    def build(cls, company_names: dict[str, list[str]], sec_entries: list[dict] | None = None,
              max_sec_entries: int = 1500) -> "TickerResolver":
        """company_names (companies.py + NEWS_COMPANY_NAMES) take priority;
        then SEC titles in file order (largest companies first) for the
        broad universe, with the conservative alias rules in the module
        docstring."""
        aliases: dict[str, list[str]] = {t: list(n) for t, n in company_names.items()}
        claimed: set[str] = {_normalize_title(n) for ns in aliases.values() for n in ns}
        for entry in (sec_entries or [])[:max_sec_entries]:
            ticker = str(entry.get("ticker", "")).upper()
            title = str(entry.get("title", ""))
            if not ticker or not title:
                continue
            full = _strip_suffixes(title)
            words = full.split()
            cands: list[str] = []
            if words:
                letters = sum(len(w) for w in words)
                if len(words) >= 2 and letters >= 6:
                    cands.append(full)
                elif len(words) == 1 and len(words[0]) >= 4 and words[0] not in GENERIC_WORDS:
                    cands.append(full)
                if len(words) >= 2 and len(words[0]) >= 4 and words[0] not in GENERIC_WORDS:
                    cands.append(words[0])
            cands = [c for c in cands if c not in claimed]
            if cands:
                # A curated ticker keeps its names and gains the SEC spelling
                # (e.g. "JPMorgan Chase"); an uncurated one is added fresh.
                aliases.setdefault(ticker, []).extend(cands)
                claimed.update(cands)
        return cls(aliases)

    def resolve(self, title: str) -> dict[str, str]:
        """{ticker: "explicit" | "name"} for every company the headline names."""
        found: dict[str, str] = {}
        for m in _EXPLICIT_TICKER_RE.finditer(title):
            tick = (m.group(1) or m.group(2) or "").upper()
            if tick and tick in self.known:
                found[tick] = "explicit"
        if not _mostly_upper(title):
            for m in _BARE_TICKER_RE.finditer(title):
                tick = m.group(1)
                if tick in self.known and tick not in _BARE_TICKER_STOPLIST:
                    found.setdefault(tick, "name")
        tokens = _normalize_title(title).split()
        n = len(tokens)
        i = 0
        while i < n:
            matched = False
            for size in range(min(self.max_words, n - i), 0, -1):
                phrase = " ".join(tokens[i:i + size])
                tick = self.index.get(phrase)
                if tick:
                    found.setdefault(tick, "name")
                    i += size
                    matched = True
                    break
            if not matched:
                i += 1
        return found


def discover(headlines: list[Headline], resolver: TickerResolver, min_mentions: int = 2,
             max_candidates: int = 40) -> dict[str, list[Headline]]:
    """{ticker: [headlines naming it]} for the strongest candidates, ranked
    by mentions (an explicit ticker mention counts double and qualifies on
    its own). Each returned Headline is a copy tagged with the ticker."""
    by_ticker: dict[str, list[Headline]] = {}
    weight: dict[str, float] = {}
    for h in headlines:
        for tick, how in resolver.resolve(h.title).items():
            by_ticker.setdefault(tick, []).append(Headline(tick, h.title, h.published, h.link, h.source))
            weight[tick] = weight.get(tick, 0.0) + (2.0 if how == "explicit" else 1.0)
    ranked = sorted(by_ticker, key=lambda t: (-weight[t], t))
    out: dict[str, list[Headline]] = {}
    for tick in ranked:
        if weight[tick] >= min_mentions or weight[tick] >= 2.0:
            out[tick] = by_ticker[tick]
        if len(out) >= max_candidates:
            break
    log.info("Discovery: %d candidates from %d tickers mentioned: %s", len(out), len(by_ticker),
             ", ".join(f"{t}({len(out[t])})" for t in list(out)[:15]) + (" ..." if len(out) > 15 else ""))
    return out


# ---------------------------------------------------------------------------
# Universe guard rails (state/universe.json)
# ---------------------------------------------------------------------------
def load_universe() -> dict:
    if UNIVERSE_PATH.exists():
        try:
            data = json.loads(UNIVERSE_PATH.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_universe(state: dict) -> None:
    UNIVERSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def check_liquidity(symbol: str, min_market_cap: float, min_dollar_volume: float) -> tuple[bool, str]:
    """(liquid?, reason) from yfinance fast_info. Unknown data -> not liquid:
    for a name the bot has never traded, no information is a reason to
    wait, not to buy."""
    info = yf.Ticker(symbol).fast_info
    cap = info.get("marketCap") if hasattr(info, "get") else getattr(info, "market_cap", None)
    vol = info.get("threeMonthAverageVolume") if hasattr(info, "get") else getattr(info, "three_month_average_volume", None)
    price = info.get("lastPrice") if hasattr(info, "get") else getattr(info, "last_price", None)
    try:
        cap = float(cap) if cap is not None else None
        dollar_vol = float(vol) * float(price) if vol is not None and price is not None else None
    except (TypeError, ValueError):
        cap, dollar_vol = None, None
    if cap is None or dollar_vol is None:
        return False, "no market-cap/volume data"
    if cap < min_market_cap:
        return False, "market cap below minimum"
    if dollar_vol < min_dollar_volume:
        return False, "average dollar volume below minimum"
    return True, "ok"


def refresh_universe(state: dict, candidates: dict[str, str], today: date, min_market_cap: float,
                     min_dollar_volume: float, max_age_days: int = 7, exempt: set[str] | None = None) -> None:
    """Record first/last seen for every candidate and (re)check liquidity
    for those not checked within max_age_days. exempt symbols (the
    watchlist) are recorded as liquid without a lookup."""
    exempt = exempt or set()
    today_s = today.isoformat()
    for sym, name in candidates.items():
        entry = state.setdefault(sym, {"name": name, "first_seen": today_s})
        entry["name"] = entry.get("name") or name
        entry["last_seen"] = today_s
        if sym in exempt:
            entry.update({"liquid": True, "checked": today_s, "reason": "watchlist"})
            continue
        checked = entry.get("checked")
        if checked and (today - date.fromisoformat(checked)).days < max_age_days:
            continue
        try:
            liquid, reason = check_liquidity(sym, min_market_cap, min_dollar_volume)
        except Exception as exc:  # noqa: BLE001 - any yfinance failure
            liquid, reason = False, f"lookup failed: {type(exc).__name__}"
        entry.update({"liquid": liquid, "checked": today_s, "reason": reason})
        if not liquid:
            log.info("Universe: %s not eligible (%s)", sym, reason)


def is_liquid(state: dict, symbol: str) -> bool:
    return bool((state.get(symbol) or {}).get("liquid"))


def tradable_tickers(client) -> set[str] | None:
    """Trading212 tickers the account can trade, or None if the lookup
    failed (callers then skip the check; an untradable order is rejected
    by the broker anyway and handled as a skipped symbol)."""
    try:
        return {i["ticker"] for i in client.instruments() if isinstance(i, dict) and i.get("ticker")}
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch Trading212 instrument list (%s); skipping tradability check", exc)
        return None
