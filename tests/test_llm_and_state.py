import json
from datetime import date

import anthropic

from conftest import NOW, make_prices
from t212bot import events, history, news, news_llm, pnl_history, portfolio_risk, position_ownership, report


def test_llm_scorer_off_by_default(monkeypatch):
    assert news_llm.make_scorer("lexicon") is None
    assert news_llm.make_scorer("llm") is None                      # no API key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert news_llm.configured("llm") and not news_llm.configured("lexicon")


class FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class FakeMsg:
    def __init__(self, text, stop="end_turn"):
        self.content, self.stop_reason = [FakeBlock(text)], stop


class FakeMessages:
    def __init__(self, fail_beta):
        self.calls, self.fail_beta = [], fail_beta

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail_beta and "fallbacks" in kw:
            resp = type("R", (), {"status_code": 400, "headers": {}, "request": None})()
            raise anthropic.BadRequestError("nope", response=resp, body=None)
        titles = kw["messages"][0]["content"].split("\n\n", 1)[1].splitlines()
        return FakeMsg(json.dumps({"scores": [{"i": i, "score": 0.9 if "beats" in t else -0.9}
                                              for i, t in enumerate(titles)]}))


class FakeClient:
    def __init__(self, fail_beta):
        self.messages = FakeMessages(fail_beta)
        self.beta = type("B", (), {"messages": self.messages})()


def test_llm_scorer_scores_caches_and_falls_back_from_beta(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = FakeClient(fail_beta=True)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: client)
    scorer = news_llm.make_scorer("llm", "claude-opus-5-5")
    out = scorer(["Apple beats estimates", "Apple misses estimates", "Apple beats estimates"])
    assert out[news._normalize_title("Apple beats estimates")] == 0.9
    assert out[news._normalize_title("Apple misses estimates")] == -0.9
    assert len(client.messages.calls) == 2                          # beta attempt + plain retry
    assert client.messages.calls[-1]["output_config"]["format"]["type"] == "json_schema"
    assert json.loads(news_llm.CACHE_PATH.read_text())[news._normalize_title("Apple beats estimates")] == 0.9
    scorer(["Apple beats estimates"])
    assert len(client.messages.calls) == 2                          # cache hit, no new call
    agg = news.aggregate_all({"AAPL": [news.Headline("AAPL", "Apple beats estimates", NOW)]}, NOW, 12, scorer=scorer)["AAPL"]
    assert agg.scorer == "llm" and agg.score == 0.9


def test_llm_refusal_falls_back_to_lexicon(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    class Refusing(FakeMessages):
        def create(self, **kw):
            return FakeMsg("", stop="refusal")
    client = FakeClient(False)
    client.messages = Refusing(False)
    client.beta = type("B", (), {"messages": client.messages})()
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: client)
    out = news_llm.make_scorer("llm")(["Apple beats estimates"])
    assert out[news._normalize_title("Apple beats estimates")] == news.score_headline("Apple beats estimates")


def test_portfolio_risk_nan_guard():
    pr = {"cumulative_return_pct": float("nan"), "peak_return_pct": 0.0, "paused": False}
    portfolio_risk.record_trade_pct(pr, float("nan"))
    assert pr["cumulative_return_pct"] != pr["cumulative_return_pct"]      # untouched (still NaN)
    portfolio_risk.record_trade_pct(pr, -0.2)
    assert abs(pr["cumulative_return_pct"] + 0.2) < 1e-12
    portfolio_risk.evaluate(pr, 0.15)
    assert pr["paused"] is True


def test_report_reads_everything(monkeypatch):
    today = date(2026, 10, 27)
    pnl = []
    pnl_history.record_trade(pnl, "news", "AAPL", 0.004)
    pnl_history.record_trade(pnl, "swing", "MSFT", -0.002)
    pnl_history.save(pnl)
    position_ownership.save({"AAPL_US_EQ": "news", "XOM_US_EQ": "swing"})
    st = {"scores": {}, "positions": {}, "closed": {}}
    news.record_scores(st, "2026-10-27", {"AAPL": news.NewsScore("AAPL", 0.6, 4, 5, 1.0),
                                          "MSFT": news.NewsScore("MSFT", -0.5, 3, 3, 0.0, source="peers:AAPL+NVDA")})
    news.save_state(st)
    events.save_calendar({"MSFT": {"next": "2026-10-29", "past": [], "fetched": "2026-10-27"}})
    history.append("news", "demo", True, {"value": 1, "free": 1, "positions": 1},
                   [history.decision("AAPL", "good", "Buy", "x"),
                    history.decision(None, "warning", "Skipped", "MSFT — earnings report within 2 day(s), not opening into it.")])
    text = report.build_report(7, today=date.today())
    assert "# Bot report" in text and "**news**: 1 closed, win rate 100%" in text
    assert "news: 1" in text and "swing: 1" in text
    assert "buy-grade symbol-days this period: 1" in text and "peers:AAPL+NVDA 1" in text
    assert "MSFT (2026-10-29)" in text
    assert "1 cycles, 0 errors, 1 buys" in text and "skipped: earnings report within 2 day(s)" in text
    assert "$" not in text


def test_report_excludes_non_finite_pnl_and_expired_pauses():
    today = date(2026, 10, 27)
    pnl = [{"t": "2026-10-20T10:00:00Z", "strategy": "swing", "symbol": "XOM", "pnl_pct": float("nan")},
           {"t": "2026-10-21T10:00:00Z", "strategy": "swing", "symbol": "KO", "pnl_pct": 0.01}]
    pnl_history.save(pnl)
    from t212bot import trade_stats
    trade_stats.save({"daytrade": {"CRM": {"wins": 0, "losses": 3, "total_pnl_pct": -0.01, "streak": 3,
                                           "streak_type": "loss", "paused_until": "2026-08-08"},
                                   "BA": {"wins": 0, "losses": 3, "total_pnl_pct": -0.01, "streak": 3,
                                          "streak_type": "loss", "paused_until": "2026-11-01"}}})
    text = report.build_report(30, today=today)
    assert "+nan%" not in text and "nan%" not in text
    assert "1 ledger entry with a non-finite P&L excluded" in text
    assert "**swing**: 1 closed, win rate 100%" in text
    assert "BA (daytrade) until 2026-11-01" in text and "CRM" not in text.split("Losing-streak pauses")[1].split("\n")[0]


def test_fetch_history_drops_nan_close(monkeypatch):
    from t212bot import data
    import pandas as pd

    class T:
        def __init__(self, sym):
            pass

        def history(self, **kw):
            idx = pd.date_range("2026-10-01", periods=3, freq="B", tz="America/New_York")
            return pd.DataFrame({"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3],
                                 "Close": [1.0, 2.0, float("nan")], "Volume": [1, 1, 1]}, index=idx)
    monkeypatch.setattr(data.yf, "Ticker", T)
    out = data.fetch_history(["AAPL"])
    assert len(out["AAPL"]) == 2 and float(out["AAPL"]["Close"].iloc[-1]) == 2.0
