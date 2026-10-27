"""Market-regime filter shared by all three bots.

Every strategy here is long-only, and none of the backtests so far
separate "the strategy worked" from "the market went up". This is the
simplest honest guard against that: when the broad market (SPY) is below
its own REGIME_SMA_DAYS-day SMA, no bot opens a new position; when
implied volatility (^VIX) is elevated, positions are sized down, and
above a higher level entries stop altogether. Exits are never affected --
a regime turning hostile is a reason to stop adding risk, not to dump
everything at once.

Fails open: if SPY/VIX can't be fetched the cycle logs a warning and runs
as if the regime were benign, because a yfinance outage shouldn't
silently switch every bot off (the swing bot's own price fetch would be
failing loudly in that case anyway). The backtests take the same mask via
--regime-filter (t212bot/backtest.py) so the effect can be measured
rather than assumed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

SPY_SYMBOL = "SPY"
VIX_SYMBOL = "^VIX"


@dataclass
class Regime:
    enabled: bool
    spy_close: float | None = None
    spy_sma: float | None = None
    vix: float | None = None
    entries_allowed: bool = True
    size_scale: float = 1.0
    reason: str = "regime filter disabled"

    def describe(self) -> str:
        if not self.enabled:
            return self.reason
        parts = []
        if self.spy_close is not None and self.spy_sma is not None:
            parts.append(f"SPY {self.spy_close:.0f} vs SMA {self.spy_sma:.0f} "
                         f"({'above' if self.spy_close > self.spy_sma else 'BELOW'})")
        if self.vix is not None:
            parts.append(f"VIX {self.vix:.1f}")
        parts.append("entries allowed" if self.entries_allowed else "NO NEW ENTRIES")
        if self.entries_allowed and self.size_scale < 1:
            parts.append(f"sizes x{self.size_scale:.2f}")
        return ", ".join(parts) + f" -- {self.reason}"


def spy_mask(spy: pd.DataFrame, sma_days: int = 50) -> pd.Series:
    """Boolean series (tz-naive, normalised dates) -- True where SPY's close
    is above its SMA. Uses the same day's close, exactly what the live
    filter sees at the end of that day; a backtest entering at that close
    sees the same information."""
    close = spy["Close"].astype(float)
    mask = close > close.rolling(sma_days).mean()
    idx = pd.DatetimeIndex(spy.index)
    mask.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
    return mask[~mask.index.duplicated(keep="last")].sort_index()


def evaluate(spy: pd.DataFrame | None, vix_level: float | None, sma_days: int = 50,
             vix_scale_above: float = 25.0, vix_block_above: float = 35.0) -> Regime:
    reg = Regime(enabled=True)
    if spy is not None and len(spy) > sma_days:
        close = spy["Close"].astype(float)
        reg.spy_close = float(close.iloc[-1])
        reg.spy_sma = float(close.rolling(sma_days).mean().iloc[-1])
    reg.vix = vix_level
    reasons = []
    if reg.spy_close is not None and reg.spy_sma is not None and reg.spy_close <= reg.spy_sma:
        reg.entries_allowed = False
        reasons.append(f"SPY below its {sma_days}-day SMA")
    if vix_level is not None:
        if vix_block_above and vix_level > vix_block_above:
            reg.entries_allowed = False
            reasons.append(f"VIX above {vix_block_above:g}")
        elif vix_scale_above and vix_level > vix_scale_above:
            reg.size_scale = 0.5
            reasons.append(f"VIX above {vix_scale_above:g}, sizing halved")
    reg.reason = "; ".join(reasons) if reasons else "benign"
    return reg


def fetch_regime(enabled: bool = True, sma_days: int = 50, vix_scale_above: float = 25.0,
                 vix_block_above: float = 35.0) -> Regime:
    if not enabled:
        return Regime(enabled=False)
    spy = vix = None
    try:
        spy = yf.Ticker(SPY_SYMBOL).history(period="1y", interval="1d", auto_adjust=True)
        if spy.empty:
            spy = None
    except Exception as exc:  # noqa: BLE001
        log.warning("Regime: SPY fetch failed (%s) -- treating the regime as benign", exc)
    try:
        vdf = yf.Ticker(VIX_SYMBOL).history(period="1mo", interval="1d")
        if not vdf.empty:
            vix = float(vdf["Close"].iloc[-1])
    except Exception as exc:  # noqa: BLE001
        log.warning("Regime: VIX fetch failed (%s) -- ignoring volatility", exc)
    if spy is None and vix is None:
        reg = Regime(enabled=True, reason="data unavailable, treated as benign")
        return reg
    return evaluate(spy, vix, sma_days, vix_scale_above, vix_block_above)
