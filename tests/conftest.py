"""Shared fixtures. Every test runs against temporary state files and a
fixed set of environment variables, never the repo's state/ directory or
the network -- any module that fetches something is monkeypatched per
test, and a test that forgets will fail loudly (no credentials, sandboxed
hosts) rather than quietly phone home."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from t212bot import daily_target, discovery, events, history, news, news_llm, pnl_history, portfolio_risk, position_ownership, trade_stats

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
ET = ZoneInfo("America/New_York")


def today_et():
    """The live cycles compare bar dates to *today* in New York, so
    fixtures that must look 'current' are dated with the real clock."""
    return datetime.now(timezone.utc).astimezone(ET).date()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for mod in (daily_target, portfolio_risk, trade_stats, pnl_history, position_ownership, news):
        monkeypatch.setattr(mod, "STATE_PATH", tmp_path / f"{mod.__name__.split('.')[-1]}.json")
    monkeypatch.setattr(events, "CALENDAR_PATH", tmp_path / "earnings_calendar.json")
    monkeypatch.setattr(events, "EDGAR_CACHE_PATH", tmp_path / "edgar_cache.json")
    monkeypatch.setattr(events, "_cik_cache", None)
    monkeypatch.setattr(events, "SEC_TICKERS_CACHE_PATH", tmp_path / "sec_tickers.json")
    monkeypatch.setattr(discovery, "UNIVERSE_PATH", tmp_path / "universe.json")
    monkeypatch.setattr(news_llm, "CACHE_PATH", tmp_path / "llm_cache.json")
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "history.jsonl")
    monkeypatch.setattr(news, "_POLITE_DELAY_SECONDS", 0)
    for var in ("ANTHROPIC_API_KEY", "EVENT_STUDY_GATE", "NEWS_PEER_DISCOUNT", "NEWS_SCORER",
                "REGIME_FILTER_ENABLED", "EARNINGS_BLACKOUT_DAYS", "WATCHLIST", "NEWS_UNIVERSE", "NEWS_FEEDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("T212_API_KEY", "test-key")
    monkeypatch.setenv("T212_API_SECRET", "test-secret")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("T212_ENV", "demo")
    # Cycle tests default to the fixed watchlist; discovery has its own tests.
    monkeypatch.setenv("NEWS_UNIVERSE", "watchlist")
    yield tmp_path


def make_prices(closes, end=None, tz="America/New_York", volume=None) -> pd.DataFrame:
    end = end or today_et().isoformat()
    idx = pd.bdate_range(end=end, periods=len(closes), tz=tz)
    c = pd.Series(closes, index=idx, dtype=float)
    vol = pd.Series(volume if volume is not None else 1e6, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c, "Volume": vol})


def rss(items) -> str:
    body = "".join(
        f"<item><title>{t}</title><link>http://x/{i}</link>"
        f"<pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for i, (t, d) in enumerate(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'


UPTREND = list(np.linspace(100, 110, 60)) + [112.0]          # +1.8% today, above SMA20
GAP_UP = list(np.linspace(100, 110, 30)) + [120.0]            # +9% today -> chased
DOWN_TODAY = list(np.linspace(100, 110, 30)) + [109.0]        # -0.9% today -> unconfirmed
BELOW_SMA = list(np.linspace(120, 100, 30)) + [101.0]         # +1% today, below SMA20
