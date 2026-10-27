import numpy as np
import pandas as pd

from conftest import make_prices
from t212bot import backtest, events, regime
from t212bot.strategy import Signal, SignalResult


def test_evaluate():
    up = make_prices(np.linspace(400, 500, 120))
    down = make_prices(np.linspace(500, 400, 120))
    r = regime.evaluate(up, 18.0)
    assert r.entries_allowed and r.size_scale == 1.0 and r.reason == "benign"
    assert "above" in r.describe()
    r = regime.evaluate(up, 28.0)
    assert r.entries_allowed and r.size_scale == 0.5 and "halved" in r.reason
    assert not regime.evaluate(up, 40.0).entries_allowed
    r = regime.evaluate(down, 18.0)
    assert not r.entries_allowed and "SMA" in r.reason and "NO NEW ENTRIES" in r.describe()
    assert regime.evaluate(None, None).entries_allowed
    off = regime.Regime(enabled=False)
    assert not off.enabled and "disabled" in off.describe()


def test_fetch_regime_fails_open(monkeypatch):
    class T:
        def __init__(self, sym):
            pass

        def history(self, **kw):
            raise RuntimeError("yfinance down")
    monkeypatch.setattr(regime.yf, "Ticker", T)
    r = regime.fetch_regime(True)
    assert r.enabled and r.entries_allowed and "benign" in r.reason
    assert not regime.fetch_regime(False).enabled


def test_spy_mask_and_backtest_lookup():
    up = make_prices(np.linspace(400, 500, 120))
    mask = regime.spy_mask(up, 50)
    assert mask.index.tz is None and bool(mask.iloc[-1]) and not bool(mask.iloc[10])   # NaN SMA -> False
    df = make_prices([100] * 10, tz=None)
    sigs = pd.Series([SignalResult(Signal.HOLD)] * 10, index=df.index)
    sigs.iloc[2] = SignalResult(Signal.BUY, 1.0, "x")
    naive = events._naive_index(df)
    assert backtest._build_trades(df, sigs, 0, 0, regime_mask=pd.Series(False, index=naive)) == []
    assert len(backtest._build_trades(df, sigs, 0, 0, regime_mask=pd.Series(True, index=naive))) == 1
    m = pd.Series([True, False], index=pd.DatetimeIndex(["2026-10-23", "2026-10-26"]))
    assert backtest._regime_allows(m, pd.Timestamp("2026-10-27 14:35", tz="America/New_York")) is False
    assert backtest._regime_allows(m, pd.Timestamp("2026-10-24")) is True       # missing date -> prior value
    assert backtest._regime_allows(m, pd.Timestamp("2026-10-01")) is True       # before the mask -> allowed
    assert backtest._regime_allows(None, pd.Timestamp("2026-10-01")) is True
