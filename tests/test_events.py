from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from conftest import NOW, make_prices
from t212bot import events, news


def test_headline_classification():
    assert events.classify_headline("Apple raises full-year guidance after record quarter, beats estimates") \
        >= {"guidance_raise", "earnings_beat"}
    assert "exec_change" in events.classify_headline("Boeing CEO steps down amid safety probe")
    assert "downgrade" in events.classify_headline("Nvidia downgraded to hold at Jefferies")
    assert "m_and_a" in events.classify_headline("Microsoft agrees to buy startup for $1bn")
    hl = [news.Headline("AAPL", t, NOW) for t in ("Apple beats estimates", "Apple raises guidance", "Apple launches new iPad")]
    dom = events.dominant_events(hl)
    assert dom[0] in ("earnings_beat", "guidance_raise") and "product" not in dom
    assert events.dominant_events([news.Headline("AAPL", "Apple launches new iPad", NOW)]) == ["product"]


@pytest.fixture
def planted():
    """600-bar synthetic history with 8 'beats' (+4% drift over 5 days on heavy
    volume) and 6 'misses' (-3%), plus an independent SPY series."""
    rng = np.random.default_rng(1)
    n = 600
    df = make_prices(100 + np.cumsum(rng.normal(0.05, 1.0, n)), end="2026-10-27",
                     volume=rng.uniform(0.8e6, 1.2e6, n))
    spy = make_prices(100 + np.cumsum(rng.normal(0.03, 0.6, n)), end="2026-10-27")
    idx = events._naive_index(df)
    closes = df["Close"].to_numpy().copy()
    vol = df["Volume"].to_numpy().copy()
    beat_days, miss_days = [], []
    for k in range(8):
        i = 40 + k * 60
        closes[i + 1:] *= 1.04
        vol[i + 1] = 3e6
        beat_days.append(idx[i].strftime("%Y-%m-%d"))
    for k in range(6):
        i = 70 + k * 60
        closes[i + 1:] *= 0.97
        vol[i + 1] = 3e6
        miss_days.append(idx[i].strftime("%Y-%m-%d"))
    df["Close"], df["High"], df["Low"], df["Volume"] = closes, closes * 1.01, closes * 0.99, vol
    past = [[d, 5.0] for d in beat_days] + [[d, -4.0] for d in miss_days]
    edgar = [(d, ["earnings"]) for d in beat_days + miss_days] + [(beat_days[0], ["exec_change"])]
    return df, spy, beat_days, miss_days, past, edgar


def test_forward_returns_in_excess_of_spy(planted):
    df, spy, beat_days, *_ = planted
    rets = events.forward_returns(df, beat_days, spy)
    assert len(rets[5]) == 8 and np.median(rets[5]) > 0.02
    assert all(not np.isnan(r) for r in rets[5])
    raw = events.forward_returns(df, beat_days, None)
    assert len(raw[1]) == 8


def test_event_study_verdicts(planted):
    df, spy, beat_days, miss_days, past, edgar = planted
    study = events.event_study("AAPL", df, ["earnings_beat"], today_move=0.02, spy=spy,
                               calendar_entry={"past": past}, edgar_events=edgar)
    names = [s.name for s in study.sources]
    assert "earnings_beat" in names and "price_reaction" in names
    kind, summary = study.verdict(5, 5)
    assert kind == "supportive" and "8 prior earnings beat analogs" in summary
    assert study.best_source(5).name == "earnings_beat"            # typed source outranks price reaction
    assert study.verdict(5, 50)[0] == "insufficient"
    assert len(study.lines(5)) == len(study.sources)

    adverse = events.event_study("AAPL", df, ["earnings_miss"], today_move=-0.02, spy=spy,
                                 calendar_entry={"past": past})
    assert adverse.verdict(5, 5)[0] == "adverse"

    # undirected EDGAR category: direction-matched, single analog -> insufficient on its own
    ex = events.event_study("AAPL", df, ["exec_change"], today_move=0.02, spy=spy, edgar_events=edgar)
    best = ex.best_source(5)
    assert best is None or best.name == "price_reaction"


def test_archive_signal_dates():
    hist = {"2026-10-01": {"AAPL": news.NewsScore("AAPL", 0.5, 4, 5, 1.0)},
            "2026-10-02": {"AAPL": news.NewsScore("AAPL", 0.5, 2, 5, 1.0)},    # too few headlines
            "2026-10-03": {"AAPL": news.NewsScore("AAPL", -0.6, 4, 5, 0.0)}}
    assert events.archive_signal_dates(hist, "AAPL", 0.35, 3) == ["2026-10-01"]
    assert events.archive_signal_dates(hist, "AAPL", 0.35, 3, positive=False) == ["2026-10-03"]


def test_earnings_calendar(monkeypatch):
    today = date(2026, 10, 27)
    nxt = (today + timedelta(days=1)).isoformat()
    monkeypatch.setattr(events, "fetch_earnings",
                        lambda sym: {"next": nxt if sym == "AAPL" else None, "past": [["2026-07-30", 3.2]]})
    cal = {}
    events.refresh_calendar(cal, ["AAPL", "MSFT"], today)
    assert cal["AAPL"]["next"] == nxt and cal["MSFT"]["next"] is None and cal["AAPL"]["fetched"] == today.isoformat()
    assert events.in_earnings_blackout(cal, "AAPL", today, 2)
    assert not events.in_earnings_blackout(cal, "MSFT", today, 2)
    assert not events.in_earnings_blackout(cal, "AAPL", today, 0)
    assert events.days_to_earnings(cal, "AAPL", today) == 1
    cal["AAPL"]["next"] = today.isoformat()
    assert events.in_earnings_blackout(cal, "AAPL", today, 2)
    assert not events.in_earnings_blackout(cal, "AAPL", today, 2, allow_report_day=True)
    assert events.upcoming_reports(cal, today, 7) == [(today.isoformat(), "AAPL")]
    # a passed date forces a refresh even when fetched today; a failed fetch keeps the old entry
    cal["AAPL"] = {"next": (today - timedelta(days=1)).isoformat(), "past": [], "fetched": today.isoformat()}
    events.refresh_calendar(cal, ["AAPL"], today)
    assert cal["AAPL"]["next"] == nxt
    monkeypatch.setattr(events, "fetch_earnings", lambda sym: (_ for _ in ()).throw(RuntimeError("yf down")))
    cal["AAPL"]["fetched"] = (today - timedelta(days=3)).isoformat()
    events.refresh_calendar(cal, ["AAPL"], today)
    assert cal["AAPL"]["next"] == nxt
    events.save_calendar(cal)
    assert events.load_calendar() == cal


def test_fetch_earnings_parses_yfinance_frame(monkeypatch):
    now = pd.Timestamp.now(tz="UTC").normalize()
    frame = pd.DataFrame(
        {"EPS Estimate": [1.0, 1.0, 1.0], "Reported EPS": [float("nan"), 1.2, 0.9], "Surprise(%)": [float("nan"), 5.0, -3.0]},
        index=pd.DatetimeIndex([now + pd.Timedelta(days=10), now - pd.Timedelta(days=80), now - pd.Timedelta(days=170)],
                               name="Earnings Date").tz_convert("America/New_York"))

    class T:
        earnings_dates = frame
    monkeypatch.setattr(events.yf, "Ticker", lambda sym: T())
    out = events.fetch_earnings("AAPL")
    assert out["next"] == (now + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    assert [s for _, s in out["past"]] == [5.0, -3.0]


class FakeResp:
    def __init__(self, payload, ok=True):
        self._p, self.ok, self.status_code = payload, ok, 200 if ok else 503

    def json(self):
        return self._p


class FakeSession:
    def get(self, url, **kw):
        assert "User-Agent" in kw["headers"]
        if "company_tickers" in url:
            return FakeResp({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
        assert url.endswith("CIK0000320193.json"), url
        return FakeResp({"filings": {"recent": {"form": ["8-K", "10-Q", "8-K"],
                                                "filingDate": ["2026-05-01", "2026-04-30", "2026-02-01"],
                                                "items": ["2.02,9.01", "", "5.02"]}}})


def test_edgar_events_and_cache(monkeypatch):
    monkeypatch.setattr(events.time, "sleep", lambda s: None)
    ev = events.fetch_edgar_events("AAPL", FakeSession())
    assert ev == [("2026-05-01", ["earnings"]), ("2026-02-01", ["exec_change"])]
    assert events.fetch_edgar_events("AAPL", None) == ev        # cached: no session needed
    assert events.lookup_cik("ZZZZ", FakeSession()) is None
