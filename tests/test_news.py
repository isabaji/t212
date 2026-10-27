import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from conftest import NOW, UPTREND, GAP_UP, DOWN_TODAY, BELOW_SMA, make_prices, rss
from t212bot import backtest, news
from t212bot.sectors import sector_of
from t212bot.strategy import NewsSentiment, Signal, SignalResult


@pytest.mark.parametrize("title, check", [
    ("Apple raises full-year guidance after record quarter, beats estimates", lambda s: s > 0.7),
    ("Tesla misses estimates, cuts outlook as demand slows", lambda s: s < -0.7),
    ("Nvidia shares not profitable for retail traders this week", lambda s: s < 0),      # negation flips
    ("Should you buy Microsoft stock before earnings?", lambda s: abs(s) < 0.3),         # listicle damped
    ("Boeing recalls 737 parts after safety probe", lambda s: s < -0.4),
    ("JPMorgan to report third-quarter results on Tuesday", lambda s: s == 0),
    ("Stock downgraded to sell at Goldman; price target cut", lambda s: s < -0.8),
])
def test_lexicon_scores(title, check):
    assert check(news.score_headline(title)), (title, news.score_headline(title))


GOOD = [("Apple raises guidance, beats estimates", NOW - timedelta(hours=1)),
        ("Apple stock jumps on record iPhone sales", NOW - timedelta(hours=3)),
        ("Analysts lift Apple price target after strong quarter", NOW - timedelta(hours=5)),
        ("Apple raises guidance, beats estimates", NOW - timedelta(hours=1)),   # duplicate
        ("Apple to hold event next week", NOW - timedelta(hours=2)),            # unscorable
        ("Apple sued over old patent", NOW - timedelta(hours=80))]              # too old
BAD = [("Microsoft downgraded to sell, cuts guidance - WSJ", NOW - timedelta(hours=2)),
       ("Microsoft misses expectations - CNBC", NOW - timedelta(hours=4)),
       ("Microsoft faces antitrust probe - FT", NOW - timedelta(hours=6))]


class FakeResp:
    def __init__(self, text, ok=True, status_code=200):
        self.text, self.ok, self.status_code = text, ok, status_code


class FakeSession:
    """Yahoo 429s for MSFT so the Google fallback is exercised."""

    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if "yahoo" in url and "s=MSFT" in url:
            return FakeResp("", ok=False, status_code=429)
        if "google" in url:
            return FakeResp(rss(BAD))
        return FakeResp(rss(GOOD))


def fetch_scores():
    sess = FakeSession()
    heads = news.fetch_headlines(["AAPL", "MSFT"], max_age_hours=48, now=NOW, session=sess)
    return heads, news.aggregate_all(heads, now=NOW, half_life_hours=12), sess


def test_rss_parsing_and_google_title_cleanup():
    parsed = news._parse_rss(rss(GOOD), "AAPL", "yahoo")
    assert len(parsed) == 6 and parsed[0].published.tzinfo is not None
    g = news._parse_rss('<rss><channel><item><title>Apple beats estimates - Reuters</title></item></channel></rss>',
                        "AAPL", "google")[0]
    assert g.title == "Apple beats estimates" and g.published is None


def test_fetch_dedupes_filters_age_and_falls_back():
    heads, scores, sess = fetch_scores()
    assert len(heads["AAPL"]) == 4                      # dedup + age filter
    assert len(heads["MSFT"]) == 3 and any("google" in u for u in sess.calls)
    a, m = scores["AAPL"], scores["MSFT"]
    assert a.score > 0.35 and a.n_scored == 3 and a.pos_frac == 1.0 and a.top_headline
    assert m.score < -0.35 and m.n_scored == 3 and m.scorer == "lexicon" and m.source == "own"


def test_fetch_failure_yields_empty_not_exception():
    class Boom:
        def get(self, url, **kw):
            raise news.requests.ConnectionError("down")
    assert news.fetch_headlines(["AAPL"], now=NOW, session=Boom()) == {"AAPL": []}


def test_archive_round_trip_has_no_dollar_figures():
    _, scores, _ = fetch_scores()
    st = news.load_state()
    news.record_scores(st, "2026-10-27", scores)
    news.save_state(st)
    st2 = news.load_state()
    hist = news.score_history(st2)
    assert abs(hist["2026-10-27"]["AAPL"].score - scores["AAPL"].score) < 1e-3
    assert hist["2026-10-27"]["MSFT"].n_scored == 3
    assert "$" not in json.dumps(st2)
    assert set(st2["scores"]["2026-10-27"]["AAPL"]) == {"score", "n", "total", "pos"}


def test_archive_prunes_old_days():
    st = {"scores": {}, "positions": {}, "closed": {}}
    for i in range(news.ARCHIVE_DAYS + 5):
        news.record_scores(st, f"2020-01-{1:02d}" if False else (pd.Timestamp("2020-01-01") + pd.Timedelta(days=i)).strftime("%Y-%m-%d"), {})
    assert len(st["scores"]) == news.ARCHIVE_DAYS


def test_peer_read_through():
    scores = {
        "NVDA": news.NewsScore("NVDA", 0.8, 5, 6, 1.0, "Nvidia raises guidance"),
        "AVGO": news.NewsScore("AVGO", 0.6, 4, 5, 0.75, "Broadcom beats"),
        "MSFT": news.NewsScore("MSFT", -0.5, 3, 3, 0.0, "x"),      # Technology too: a negative third peer
        "AAPL": news.NewsScore("AAPL", 0.0, 1, 2, 0.0, None),      # too few own headlines -> borrows
        "JPM": news.NewsScore("JPM", 0.0, 0, 0, 0.0, None),        # Financials: no peers with news
    }
    ps = news.peer_scores(scores, sector_of, 3, 0.5)
    assert ps["AAPL"].source == "peers:AVGO+MSFT+NVDA"
    assert abs(ps["AAPL"].score - 0.5 * (0.8 * 5 + 0.6 * 4 - 0.5 * 3) / 12) < 1e-9
    assert ps["AAPL"].n_scored == 12 and ps["AAPL"].top_headline.startswith("[NVDA]")
    assert ps["MSFT"] is scores["MSFT"] and ps["JPM"] is scores["JPM"]
    assert news.peer_scores(scores, sector_of, 3, 0.0) is scores
    st = {"scores": {}, "positions": {}, "closed": {}}
    news.record_scores(st, "2026-10-27", ps)
    assert st["scores"]["2026-10-27"]["AAPL"]["src"] == "peers:AVGO+MSFT+NVDA"
    assert "src" not in st["scores"]["2026-10-27"]["NVDA"]
    assert news.score_history(st)["2026-10-27"]["AAPL"].source == "peers:AVGO+MSFT+NVDA"


def test_strategy_gates():
    _, scores, _ = fetch_scores()
    strat = NewsSentiment()
    strat.set_news(scores, history={})
    sig = strat.generate_signals({"AAPL": make_prices(UPTREND), "MSFT": make_prices(UPTREND)})
    assert sig["AAPL"].signal is Signal.BUY and 0 < sig["AAPL"].strength <= 1
    assert sig["AAPL"].reason.startswith("news:3:")
    assert sig["MSFT"].signal is Signal.SELL
    assert strat.generate_signals({"AAPL": make_prices(GAP_UP)})["AAPL"].reason == "chased"
    assert strat.generate_signals({"AAPL": make_prices(DOWN_TODAY)})["AAPL"].reason == "unconfirmed"
    assert strat.generate_signals({"AAPL": make_prices(BELOW_SMA)})["AAPL"].reason == "downtrend"
    assert strat.generate_signals({"AAPL": make_prices(UPTREND[-5:])})["AAPL"].signal is Signal.HOLD


def test_strategy_fresh_news_and_min_headlines():
    _, scores, _ = fetch_scores()
    strat = NewsSentiment()
    day = make_prices(UPTREND).index[-1].strftime("%Y-%m-%d")
    yesterday = (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (pd.Timestamp(day) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    strat.set_news(scores, history={yesterday: {"AAPL": news.NewsScore("AAPL", 0.6, 4, 5, 1.0)}})
    assert strat.generate_signals({"AAPL": make_prices(UPTREND)})["AAPL"].reason == "stale_news"
    strat.set_news(scores, history={week_ago: {"AAPL": news.NewsScore("AAPL", 0.6, 4, 5, 1.0)}})
    assert strat.generate_signals({"AAPL": make_prices(UPTREND)})["AAPL"].signal is Signal.BUY
    strat.set_news({"AAPL": news.NewsScore("AAPL", 0.9, 2, 2, 1.0)})
    assert strat.generate_signals({"AAPL": make_prices(UPTREND)})["AAPL"].signal is Signal.HOLD


def test_strategy_backtest_mode_looks_up_by_bar_date_and_symbol():
    _, scores, _ = fetch_scores()
    df = make_prices(UPTREND)
    day = df.index[-1].strftime("%Y-%m-%d")
    bt = NewsSentiment(score_history={day: {"AAPL": scores["AAPL"]}}, backtest_symbol="AAPL")
    assert bt.generate_signals({"X": df})["X"].signal is Signal.BUY          # placeholder key, as the engine uses
    other_day = make_prices(UPTREND, end=(pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    assert bt.generate_signals({"X": other_day})["X"].signal is Signal.HOLD


def test_build_trades_max_hold_exit():
    df = make_prices([100] * 10, tz=None)
    sigs = pd.Series([SignalResult(Signal.HOLD)] * 10, index=df.index)
    sigs.iloc[2] = SignalResult(Signal.BUY, 1.0, "x")
    trades = backtest._build_trades(df, sigs, 0, 0, max_hold_bars=3)
    assert len(trades) == 1 and trades[0]["exit_reason"] == "max_hold" and trades[0]["exit_date"] == df.index[5]
    assert backtest._build_trades(df, sigs, 0, 0)[0]["exit_reason"] == "end_of_data"


def test_news_backtest_replays_archive(monkeypatch, capsys):
    days = pd.bdate_range(end="2026-10-27", periods=30)
    closes = list(100 + np.arange(80) * 0.5 + np.random.default_rng(0).normal(0, 0.2, 80))
    pdf = make_prices(closes, end="2026-10-27")
    archive = {d.strftime("%Y-%m-%d"): {"AAPL": {"score": 0.6 if i % 7 == 3 else 0.05, "n": 4, "total": 6,
                                                  "pos": 1.0 if i % 7 == 3 else 0.5}}
               for i, d in enumerate(days)}
    news.save_state({"scores": archive, "positions": {}, "closed": {}})
    monkeypatch.setattr(backtest, "fetch_history", lambda symbols, period="2y", interval="1d": {"AAPL": pdf})
    backtest.print_news_backtest(["AAPL", "MSFT"], n_windows=1, stop_loss_pct=0.03, take_profit_pct=0.06, max_hold_days=3)
    out = capsys.readouterr().out
    assert "=== AAPL ===" in out and "max_hold" in out and "closed trades" in out
    assert "fewer than 20 days" not in out


def test_news_backtest_empty_archive_explains_itself(capsys):
    backtest.print_news_backtest(["AAPL"])
    assert "No archived news scores yet" in capsys.readouterr().out
