"""End-to-end dry-run cycles with the broker, data feeds, news feed, regime
and calendar all faked. These are the tests that catch a broken cycle
before market open does."""

import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from conftest import NOW, UPTREND, BELOW_SMA, make_prices, today_et
from t212bot import bot, discovery, events, history, news, position_ownership, regime
from t212bot.config import Config
from t212bot.strategy import NewsSentiment, SMACrossover, Signal, SignalResult, Strategy


class FakeT212:
    positions: list = []

    def __init__(self, *a, **k):
        pass

    def account_cash(self):
        return {"free": 5000.0, "total": 10000.0}

    def account_info(self):
        return {"currencyCode": "USD"}

    def portfolio(self):
        return list(self.positions)

    def instruments(self):
        rows = [{"ticker": f"{s}_US_EQ", "shortName": s, "name": s.title(), "type": "STOCK", "currencyCode": "USD"}
                for s in ("AAPL", "MSFT", "NVDA", "AVGO", "GOOGL", "PLTR")]
        # Broker ticker that differs from the mechanical SYMBOL_US_EQ (as META did live).
        rows.append({"ticker": "LLY1_US_EQ", "shortName": "LLY", "name": "Eli Lilly", "type": "STOCK",
                     "currencyCode": "USD"})
        rows.append({"ticker": "TINY_US_EQ", "shortName": "TINY", "name": "Tinyco Holdings", "type": "STOCK",
                     "currencyCode": "USD"})
        return rows

    def place_market_order(self, *a):
        raise AssertionError("dry run must never place an order")


def last_record():
    return json.loads(history.HISTORY_PATH.read_text().splitlines()[-1])


def decisions_of(rec, headline):
    return {d["symbol"]: d for d in rec["decisions"] if d["headline"] == headline}


HEADS = {
    "AAPL": [news.Headline("AAPL", t, NOW - timedelta(hours=1)) for t in
             ("Apple raises guidance, beats estimates", "Apple stock jumps on record iPhone sales",
              "Analysts lift Apple price target after strong quarter")],
    "MSFT": [news.Headline("MSFT", t, NOW - timedelta(hours=1)) for t in
             ("Microsoft beats expectations", "Microsoft raises outlook", "Microsoft wins big contract")],
}


@pytest.fixture
def planted_long():
    rng = np.random.default_rng(1)
    n = 600
    df = make_prices(100 + np.cumsum(rng.normal(0.05, 1.0, n)), volume=rng.uniform(0.8e6, 1.2e6, n))
    spy = make_prices(100 + np.cumsum(rng.normal(0.03, 0.6, n)))
    idx = events._naive_index(df)
    closes, vol = df["Close"].to_numpy().copy(), df["Volume"].to_numpy().copy()
    beat_days = []
    for k in range(8):
        i = 40 + k * 60
        closes[i + 1:] *= 1.04
        vol[i + 1] = 3e6
        beat_days.append(idx[i].strftime("%Y-%m-%d"))
    df["Close"], df["High"], df["Low"], df["Volume"] = closes, closes * 1.01, closes * 0.99, vol
    return df, spy, beat_days


@pytest.fixture
def wired(monkeypatch, planted_long):
    """Everything a cycle touches, faked. Returns a mutable settings dict
    the tests tweak before running a cycle."""
    df, spy, beat_days = planted_long
    cfgd = {"regime": regime.Regime(enabled=True, entries_allowed=True, size_scale=1.0, reason="benign"),
            "prices": {}, "long": df, "spy": spy, "heads": HEADS, "earnings_next": {}}
    monkeypatch.setenv("WATCHLIST", "AAPL,MSFT,NVDA,AVGO")
    FakeT212.positions = []
    monkeypatch.setattr(bot, "Trading212Client", FakeT212)
    monkeypatch.setattr(bot, "fetch_fx_rate", lambda *a, **k: 1.0)

    def fake_history(symbols, period="1y", interval="1d"):
        if period == "10y":
            return {s: (cfgd["spy"] if s == "SPY" else cfgd["long"]) for s in symbols}
        return {s: cfgd["prices"].get(s, make_prices(UPTREND)) for s in symbols if s != "SPY"}
    monkeypatch.setattr(bot, "fetch_history", fake_history)
    monkeypatch.setattr(regime, "fetch_regime", lambda *a, **k: cfgd["regime"])
    monkeypatch.setattr(events, "fetch_earnings",
                        lambda sym: {"next": cfgd["earnings_next"].get(sym), "past": [[d, 5.0] for d in beat_days]})
    monkeypatch.setattr(events, "fetch_edgar_events", lambda sym, session=None, max_age_days=7: [])
    monkeypatch.setattr(news, "fetch_headlines",
                        lambda symbols, max_age_hours, now=None, session=None, **kw: {s: cfgd["heads"].get(s, []) for s in symbols})
    # discovery touchpoints (only used when NEWS_UNIVERSE=discover/both)
    cfgd["market"] = []
    monkeypatch.setattr(discovery, "fetch_market_headlines", lambda feeds, now=None, max_age_hours=48.0, session=None: list(cfgd["market"]))
    monkeypatch.setattr(events, "load_sec_entries", lambda session=None, max_age_days=7: [
        {"ticker": "LLY", "title": "ELI LILLY & Co", "cik": 1}, {"ticker": "PLTR", "title": "Palantir Technologies Inc.", "cik": 2},
        {"ticker": "TINY", "title": "Tinyco Holdings Inc", "cik": 3}])
    cfgd["liquid"] = {"LLY": (True, "ok"), "PLTR": (True, "ok"), "TINY": (False, "market cap below minimum")}
    monkeypatch.setattr(discovery, "check_liquidity", lambda sym, cap, vol: cfgd["liquid"].get(sym, (False, "no market-cap/volume data")))
    return cfgd


def news_strategy(cfg):
    return NewsSentiment(trend_filter=cfg.news_trend_filter_days, max_chase_pct=cfg.news_max_chase_pct)


def test_swing_cycle_runs_with_guards(wired):
    wired["earnings_next"] = {"MSFT": (today_et() + timedelta(days=1)).isoformat()}
    bot.run_cycle(Config(), SMACrossover(20, 50))
    rec = last_record()
    assert rec["bot"] == "swing" and rec["error"] is None and rec["dry_run"] is True
    assert json.loads(events.CALENDAR_PATH.read_text())["MSFT"]["next"] == wired["earnings_next"]["MSFT"]


def test_news_cycle_buys_with_history_blackout_and_read_through(wired, monkeypatch):
    monkeypatch.setenv("NEWS_PEER_DISCOUNT", "0.6")
    wired["earnings_next"] = {"MSFT": (today_et() + timedelta(days=1)).isoformat()}
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert rec["error"] is None
    buys, hist = decisions_of(rec, "Buy"), decisions_of(rec, "History")
    assert "AAPL" in buys and "History" in buys["AAPL"]["detail"]
    assert "supportive" in hist["AAPL"]["detail"] and "earnings_beat" in hist["AAPL"]["detail"]
    assert any("earnings report within" in d["detail"] and "MSFT" in d["detail"] for d in rec["decisions"])
    assert "MSFT" not in buys
    peer = {s for s in ("NVDA", "AVGO") if s in buys}
    assert peer and all("sector read-through via" in buys[s]["detail"] for s in peer)
    assert "$" not in json.dumps(json.loads(news.STATE_PATH.read_text()))
    arch = json.loads(news.STATE_PATH.read_text())["scores"][today_et().isoformat()]
    assert arch["NVDA"]["src"].startswith("peers:") and "src" not in arch["AAPL"]
    owners = json.loads(position_ownership.STATE_PATH.read_text())
    assert owners["AAPL_US_EQ"] == "news"


def test_news_cycle_untracked_positions_left_alone(wired):
    FakeT212.positions = [{"ticker": "GOOGL_US_EQ", "quantity": 3, "averagePrice": 150.0, "currentPrice": 151.0}]
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert rec["error"] is None and "GOOGL" not in decisions_of(rec, "Sell")
    assert "GOOGL_US_EQ" not in json.loads(position_ownership.STATE_PATH.read_text())


def test_news_cycle_max_hold_exit_and_same_day_reentry_block(wired):
    cfg = Config()
    FakeT212.positions = [{"ticker": "AAPL_US_EQ", "quantity": 2, "averagePrice": 112.0, "currentPrice": 112.0}]
    position_ownership.save({"AAPL_US_EQ": "news"})
    news.save_state({"scores": {}, "closed": {},
                     "positions": {"AAPL_US_EQ": {"entered": (today_et() - timedelta(days=4)).isoformat()}}})
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    sells = decisions_of(rec, "Sell")
    assert "AAPL" in sells and "max 3" in sells["AAPL"]["detail"]
    assert json.loads(news.STATE_PATH.read_text())["closed"] == {"AAPL_US_EQ": today_et().isoformat()}
    FakeT212.positions = []
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert "AAPL" not in decisions_of(rec, "Buy")
    assert any("not re-entering" in d["detail"] for d in rec["decisions"])


def test_news_cycle_stop_loss_exit(wired):
    cfg = Config()
    FakeT212.positions = [{"ticker": "AAPL_US_EQ", "quantity": 2, "averagePrice": 120.0, "currentPrice": 112.0}]
    position_ownership.save({"AAPL_US_EQ": "news"})
    news.save_state({"scores": {}, "closed": {}, "positions": {"AAPL_US_EQ": {"entered": today_et().isoformat()}}})
    bot.run_news_cycle(cfg, news_strategy(cfg))
    sells = decisions_of(last_record(), "Sell")
    assert "AAPL" in sells and "stop-loss" in sells["AAPL"]["detail"]


def test_news_cycle_hostile_regime_blocks_entries(wired):
    wired["regime"] = regime.Regime(enabled=True, entries_allowed=False, reason="SPY below its 50-day SMA")
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert not decisions_of(rec, "Buy")
    assert any("market regime hostile" in d["detail"] for d in rec["decisions"])


def test_news_cycle_event_study_gate(wired, monkeypatch, planted_long):
    df, spy, beat_days = planted_long
    adverse = df.copy()
    cl = adverse["Close"].to_numpy().copy()
    for d in beat_days:
        i = int(np.where(events._naive_index(adverse) == pd.Timestamp(d))[0][0])
        cl[i + 1:] *= 0.95
    adverse["Close"] = cl
    wired["long"] = adverse
    monkeypatch.setenv("EVENT_STUDY_GATE", "true")
    cfg = Config()
    assert cfg.event_study_gate
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert "AAPL" not in decisions_of(rec, "Buy")
    assert any("history after similar news is adverse" in d["detail"] and "AAPL" in d["detail"] for d in rec["decisions"])
    assert "adverse" in decisions_of(rec, "History")["AAPL"]["detail"]


def test_news_cycle_no_bar_today_blocks_entries(wired):
    yesterday = (today_et() - timedelta(days=1)).isoformat()
    wired["prices"] = {s: make_prices(UPTREND, end=yesterday) for s in ("AAPL", "MSFT", "NVDA", "AVGO")}
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert not decisions_of(rec, "Buy") and any("no daily bar for today" in d["detail"] for d in rec["decisions"])


def test_news_cycle_logs_error_and_reraises(wired, monkeypatch):
    monkeypatch.setattr(bot, "fetch_history", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yfinance down")))
    cfg = Config()
    with pytest.raises(RuntimeError):
        bot.run_news_cycle(cfg, news_strategy(cfg))
    assert last_record()["error"] == "yfinance down"


class AlwaysBuy(Strategy):
    def generate_signals(self, prices, confirm_prices=None, daily_prices=None):
        return {s: SignalResult(Signal.BUY, 0.8) for s in prices}


@pytest.fixture
def intraday(monkeypatch, wired):
    now_et = pd.Timestamp.now(tz="America/New_York").floor("5min")

    def fake_intraday(symbols, key, secret, days=5, timeframe="5Min", feed="iex"):
        idx = pd.date_range(end=now_et, periods=200, freq="5min")
        out = {}
        for s in symbols:
            c = pd.Series(np.linspace(100, 101, 200), index=idx)
            out[s] = pd.DataFrame({"Open": c, "High": c * 1.001, "Low": c * 0.999, "Close": c, "Volume": 1e5})
        return out
    monkeypatch.setattr(bot, "fetch_intraday", fake_intraday)
    monkeypatch.setattr(bot, "_minutes_until_close", lambda ts: 120.0)
    monkeypatch.setattr(bot, "_too_early_for_entries", lambda now, delay: False)
    monkeypatch.setattr(bot, "STALE_BAR_MINUTES", 10 ** 6)
    monkeypatch.setenv("ALPACA_API_KEY", "a")
    monkeypatch.setenv("ALPACA_API_SECRET", "b")
    return wired


def test_daytrade_cycle_guards(intraday):
    intraday["earnings_next"] = {"MSFT": (today_et() + timedelta(days=1)).isoformat()}
    intraday["regime"] = regime.Regime(enabled=True, entries_allowed=True, size_scale=0.5, reason="VIX above 25, sizing halved")
    bot.run_day_trade_cycle(Config(), AlwaysBuy())
    rec = last_record()
    assert rec["bot"] == "daytrade" and rec["error"] is None
    buys = decisions_of(rec, "Buy")
    assert "AAPL" in buys and "MSFT" not in buys
    assert any("earnings report within" in d["detail"] and "MSFT" in d["detail"] for d in rec["decisions"])
    intraday["regime"] = regime.Regime(enabled=True, entries_allowed=False, reason="SPY below its 50-day SMA")
    position_ownership.save({})
    bot.run_day_trade_cycle(Config(), AlwaysBuy())
    rec = last_record()
    assert not decisions_of(rec, "Buy") and any("regime hostile" in d["detail"] for d in rec["decisions"])


def test_news_cycle_discover_mode(wired, monkeypatch):
    """Universe comes from the market scan: LLY (named in 3 headlines, liquid,
    tradable) is bought; TINY is discovered but fails the liquidity gate;
    ZZZZ-style untradable names never reach the universe; the watchlist is
    not scanned at all."""
    monkeypatch.setenv("NEWS_UNIVERSE", "discover")
    wired["market"] = [news.Headline("", t, NOW - timedelta(hours=1)) for t in (
        "Eli Lilly raises full-year guidance after record quarter", "Eli Lilly beats estimates, shares jump",
        "Eli Lilly posts strong quarter, raises outlook", "Tinyco Holdings beats estimates", "Tinyco Holdings raises guidance",
        "Tinyco Holdings wins contract", "Apple unveils new iPad")]
    wired["heads"] = {}                       # per-ticker feeds return nothing: discovery headlines alone must do
    cfg = Config()
    assert cfg.news_universe == "discover"
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert rec["error"] is None, rec["error"]
    buys = decisions_of(rec, "Buy")
    assert "LLY" in buys and "TINY" not in buys and "AAPL" not in buys, buys
    arch = json.loads(news.STATE_PATH.read_text())["scores"][today_et().isoformat()]
    assert "LLY" in arch and "MSFT" not in arch                             # watchlist not scanned in discover mode
    assert "TINY" not in arch                                                # failed liquidity before the depth pass
    uni = json.loads(discovery.UNIVERSE_PATH.read_text())
    assert uni["LLY"]["liquid"] is True and uni["TINY"]["liquid"] is False
    assert uni["LLY"]["name"] == "Eli Lilly" and uni["TINY"]["name"] == "Tinyco Holdings"   # broker display names
    from t212bot import data
    assert data.YAHOO_TO_T212 == {"LLY": "LLY1_US_EQ"}                        # broker ticker mapped, not rejected
    assert "LLY1_US_EQ" in json.loads(position_ownership.STATE_PATH.read_text())  # and the order used it


def test_news_cycle_discover_drops_untradable(wired, monkeypatch):
    monkeypatch.setenv("NEWS_UNIVERSE", "both")
    monkeypatch.setattr(FakeT212, "instruments", lambda self: [
        {"ticker": f"{s}_US_EQ", "shortName": s, "name": s, "type": "STOCK", "currencyCode": "USD"}
        for s in ("AAPL", "MSFT", "NVDA", "AVGO")])
    wired["market"] = [news.Headline("", t, NOW - timedelta(hours=1)) for t in (
        "Eli Lilly raises guidance", "Eli Lilly beats estimates")]
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert rec["error"] is None
    arch = json.loads(news.STATE_PATH.read_text())["scores"][today_et().isoformat()]
    assert "LLY" not in arch and "AAPL" in arch                             # untradable dropped; watchlist kept ("both")


def test_news_opening_delay_helper():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    def at(h, m):
        return datetime(2026, 10, 27, h, m, tzinfo=et).astimezone(timezone.utc)
    assert bot._news_too_early(at(9, 31), 30)
    assert bot._news_too_early(at(9, 59), 30)
    assert not bot._news_too_early(at(10, 0), 30)
    assert not bot._news_too_early(at(9, 0), 30)       # pre-market: the no-bar gate handles it
    assert not bot._news_too_early(at(9, 31), 0)


def test_news_cycle_opening_delay_blocks_entries(wired, monkeypatch):
    monkeypatch.setenv("NEWS_ENTRY_DELAY_MINUTES", "30")
    monkeypatch.setattr(bot, "_news_too_early", lambda now, delay: delay > 0)
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert not decisions_of(rec, "Buy")
    assert any("minutes of the open" in d["detail"] and "AAPL" in d["detail"] for d in rec["decisions"]), rec["decisions"]


def test_news_cycle_entry_cap(wired, monkeypatch):
    monkeypatch.setenv("NEWS_PEER_DISCOUNT", "0.6")             # AAPL + NVDA/AVGO would all buy
    monkeypatch.setenv("NEWS_MAX_ENTRIES_PER_CYCLE", "1")
    cfg = Config()
    bot.run_news_cycle(cfg, news_strategy(cfg))
    rec = last_record()
    assert len(decisions_of(rec, "Buy")) == 1
    assert any("this cycle" in d["detail"] and "NEWS_MAX_ENTRIES_PER_CYCLE" in d["detail"] for d in rec["decisions"])
