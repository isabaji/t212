from datetime import date, timedelta

from conftest import NOW, rss
from t212bot import companies, discovery, news

SEC = [
    {"ticker": "AAPL", "title": "Apple Inc.", "cik": 320193},
    {"ticker": "NVDA", "title": "NVIDIA CORP", "cik": 1045810},
    {"ticker": "JPM", "title": "JPMORGAN CHASE & CO", "cik": 19617},
    {"ticker": "TGT", "title": "TARGET CORP", "cik": 27419},
    {"ticker": "GM", "title": "General Motors Co", "cik": 1467858},
    {"ticker": "LLY", "title": "ELI LILLY & Co", "cik": 59478},
    {"ticker": "AMD", "title": "ADVANCED MICRO DEVICES, INC.", "cik": 2488},
    {"ticker": "GOOG", "title": "Alphabet Inc.", "cik": 1652044},
    {"ticker": "GOOGL", "title": "Alphabet Inc.", "cik": 1652044},
    {"ticker": "UBER", "title": "Uber Technologies, Inc", "cik": 1543151},
    {"ticker": "GPS", "title": "Gap Inc.", "cik": 39911},
    {"ticker": "IBM", "title": "INTERNATIONAL BUSINESS MACHINES CORP", "cik": 51143},
    {"ticker": "PLTR", "title": "Palantir Technologies Inc.", "cik": 1321655},
]


def build():
    return discovery.TickerResolver.build(companies.names_for(), SEC)


def test_alias_rules():
    r = build()
    assert r.index["apple"] == "AAPL" and "iphone" in r.index              # curated aliases kept
    assert r.index["nvidia"] == "NVDA"
    assert r.index["jpmorgan chase"] == "JPM" and r.index["jpmorgan"] == "JPM"
    assert "target" not in r.index                                          # generic single word
    assert "general" not in r.index and r.index["general motors"] == "GM"
    assert r.index["eli lilly"] == "LLY"
    assert r.index["advanced micro devices"] == "AMD" and "advanced" not in r.index
    assert r.index["alphabet"] == "GOOGL" and "GOOG" not in r.names        # curated claim wins; GOOG gets nothing
    assert r.index["uber"] == "UBER" and r.index["uber technologies"] == "UBER"
    assert "gap" not in r.index
    assert r.index["international business machines"] == "IBM"
    assert r.index["palantir"] == "PLTR"


def test_resolve_names_and_explicit_tickers():
    r = build()
    assert r.resolve("Nvidia and AMD rally as chip demand surges") == {"NVDA": "name", "AMD": "name"}  # bare ticker
    assert r.resolve("IBM names new CEO as AI push continues") == {"IBM": "name"}               # CEO/AI stop-listed
    assert r.resolve("AMD STOCK SOARS AS NVDA SLIPS") == {}                                      # all-caps: no bare tickers
    assert r.resolve("JPMorgan Chase profit rises") == {"JPM": "name"}                           # SEC spelling added
    assert r.resolve("Eli Lilly's weight-loss drug wins approval") == {"LLY": "name"}
    assert r.resolve("Target cuts outlook; Gap shares fall") == {}          # generic names never match
    assert r.resolve("Acme Widgets (NASDAQ: PLTR) announces results") == {"PLTR": "explicit"}
    assert r.resolve("$UBER rips higher after earnings") == {"UBER": "explicit"}
    assert r.resolve("(NYSE: ZZZZ) unknown company") == {}                  # unknown ticker ignored
    assert r.resolve("The general consensus is bullish") == {}


def test_discover_ranks_and_filters():
    r = build()
    heads = [news.Headline("", t, NOW - timedelta(hours=1)) for t in (
        "Nvidia beats estimates", "Nvidia raises guidance", "Nvidia shares jump",
        "Apple unveils new iPad", "JPMorgan Chase names new CFO",
        "Palantir Technologies (NASDAQ: PLTR) wins Army contract")]
    out = discovery.discover(heads, r, min_mentions=2, max_candidates=10)
    assert list(out)[0] == "NVDA" and len(out["NVDA"]) == 3
    assert "PLTR" in out                                                     # explicit: qualifies alone
    assert "AAPL" not in out and "JPM" not in out                           # single name mention
    assert all(h.symbol == "NVDA" for h in out["NVDA"])
    assert len(discovery.discover(heads, r, min_mentions=1, max_candidates=2)) == 2


def test_feed_list_syntax():
    default = discovery.feed_list()
    names = [n for n, _ in default]
    assert "cnbc-top" in names and any(n.startswith("gnews:") for n in names)
    only = discovery.feed_list("mine=https://a/rss;other=https://b/rss")
    assert only == [("mine", "https://a/rss"), ("other", "https://b/rss")]
    ext = dict(discovery.feed_list("+mine=https://a/rss;-cnbc-top"))
    assert "mine" in ext and "cnbc-top" not in ext and "marketwatch-top" in ext


class FakeResp:
    def __init__(self, text, ok=True):
        self.text, self.ok, self.status_code = text, ok, 200 if ok else 503


class FakeSession:
    def get(self, url, **kw):
        if "dead" in url:
            return FakeResp("", ok=False)
        if "html" in url:
            return FakeResp("<html>not a feed</html>")
        if "news.google.com" in url:
            return FakeResp(rss([("Nvidia beats estimates - Reuters", NOW - timedelta(hours=1))]))  # dup once stripped
        if "b/rss" in url:
            return FakeResp(rss([("Nvidia beats estimates", NOW - timedelta(hours=2)),     # duplicate across feeds
                                 ("Old story", NOW - timedelta(hours=90))]))
        return FakeResp(rss([("Nvidia beats estimates", NOW - timedelta(hours=1)),
                             ("Apple unveils new iPad - CNBC", NOW - timedelta(hours=1))]))


def test_fetch_market_headlines_dedupes_and_tolerates_dead_feeds(monkeypatch):
    monkeypatch.setattr(discovery, "_FEED_DELAY_SECONDS", 0)
    feeds = [("a", "https://a/rss"), ("b", "https://b/rss"), ("dead", "https://dead/rss"),
             ("page", "https://html/rss"), ("g", "https://news.google.com/rss/search?q=x")]
    out = discovery.fetch_market_headlines(feeds, now=NOW, session=FakeSession())
    titles = sorted(h.title for h in out)
    assert titles == ["Apple unveils new iPad - CNBC", "Nvidia beats estimates"]      # 'g' is a dup of 'a'
    assert {h.source for h in out} == {"a"}


def test_liquidity_and_universe(monkeypatch):
    class Info(dict):
        pass
    data = {"BIG": Info(marketCap=5e10, threeMonthAverageVolume=5e6, lastPrice=100.0),
            "TINY": Info(marketCap=3e8, threeMonthAverageVolume=1e5, lastPrice=5.0),
            "THIN": Info(marketCap=5e9, threeMonthAverageVolume=1e5, lastPrice=20.0),
            "NODATA": Info()}

    class T:
        def __init__(self, sym):
            self.fast_info = data.get(sym, Info())
    monkeypatch.setattr(discovery.yf, "Ticker", T)
    assert discovery.check_liquidity("BIG", 2e9, 2e7) == (True, "ok")
    assert discovery.check_liquidity("TINY", 2e9, 2e7)[1] == "market cap below minimum"
    assert discovery.check_liquidity("THIN", 2e9, 2e7)[1] == "average dollar volume below minimum"
    assert discovery.check_liquidity("NODATA", 2e9, 2e7)[1] == "no market-cap/volume data"
    today = date(2026, 10, 27)
    st = {}
    discovery.refresh_universe(st, {"BIG": "Big Co", "TINY": "Tiny Co", "AAPL": "Apple"}, today, 2e9, 2e7,
                               exempt={"AAPL"})
    assert discovery.is_liquid(st, "BIG") and not discovery.is_liquid(st, "TINY") and discovery.is_liquid(st, "AAPL")
    assert st["AAPL"]["reason"] == "watchlist" and st["BIG"]["first_seen"] == today.isoformat()
    # cached: a now-failing lookup within max_age_days isn't re-run
    monkeypatch.setattr(discovery, "check_liquidity", lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    discovery.refresh_universe(st, {"BIG": "Big Co"}, today + timedelta(days=2), 2e9, 2e7)
    assert discovery.is_liquid(st, "BIG")
    discovery.refresh_universe(st, {"BIG": "Big Co"}, today + timedelta(days=9), 2e9, 2e7)
    assert not discovery.is_liquid(st, "BIG") and st["BIG"]["reason"].startswith("lookup failed")
    discovery.save_universe(st)
    assert discovery.load_universe() == st


INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "name": "Apple", "type": "STOCK", "currencyCode": "USD"},
    {"ticker": "META1_US_EQ", "shortName": "META", "name": "Meta Platforms", "type": "STOCK", "currencyCode": "USD"},
    {"ticker": "ZETA_US_EQ", "shortName": "ZETA", "name": "Zeta Global Holdings", "type": "STOCK", "currencyCode": "USD"},
    {"ticker": "VODl_EQ", "shortName": "VOD", "name": "Vodafone", "type": "STOCK", "currencyCode": "GBX"},
    {"ticker": "SPY_US_EQ", "shortName": "SPY", "name": "SPDR S&P 500", "type": "ETF", "currencyCode": "USD"},
    {"nope": 1},
]


def test_instrument_entries_and_tradable_map():
    entries = discovery.instrument_entries(INSTRUMENTS)
    assert [e["ticker"] for e in entries] == ["AAPL", "META", "ZETA"]          # US stocks only, no ETFs/LSE
    assert discovery.tradable_map(INSTRUMENTS) == {"AAPL": "AAPL_US_EQ", "META": "META1_US_EQ", "ZETA": "ZETA_US_EQ"}
    assert discovery.tradable_map(None) is None
    r = discovery.TickerResolver.build(companies.names_for(), None, extra_entries=entries)
    assert r.index["zeta global"] == "ZETA" and r.index["zeta"] == "ZETA"     # broker list is a name source
    assert r.resolve("Zeta Global jumps on AI marketing deal") == {"ZETA": "name"}


def test_fetch_instruments_fails_open():
    class C:
        def instruments(self):
            return INSTRUMENTS
    assert discovery.fetch_instruments(C()) == INSTRUMENTS

    class Broken:
        def instruments(self):
            raise RuntimeError("429")
    assert discovery.fetch_instruments(Broken()) is None
