"""Strategy interface and a simple SMA-crossover example.

To add your own strategy, subclass Strategy and implement generate_signals().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from .indicators import ema, rsi


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    """A signal plus, for BUY, how strong the setup is (0..1).

    strength feeds RiskManager.size_buy: a strong signal is sized toward the
    MAX_POSITION_PCT ceiling, a weak one gets a smaller slice of it. Only
    meaningful for BUY — SELL/HOLD carry the default and are ignored by sizing.
    """

    signal: Signal
    strength: float = 1.0


class Strategy(ABC):
    @abstractmethod
    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, SignalResult]:
        """Map each symbol to a SignalResult, given its OHLCV history."""


class SMACrossover(Strategy):
    """Buy when the fast SMA crosses above the slow SMA; sell on the reverse cross.

    Deliberately simple — a starting point, not an edge.

    trend_filter (optional): require price above its own trend_filter-day SMA
    before taking a BUY — a regime filter that skips crossover signals in a
    longer-term downtrend, where crossovers are more likely to be noise/chop
    rather than a real trend starting. Off by default (None) to keep existing
    behavior unchanged; SELL is never filtered, since exiting promptly is
    still correct in any regime.

    strength_norm_pct: a BUY's strength is how far price has extended above
    its own slow SMA, as a fraction of that SMA, scaled so strength_norm_pct
    of extension maps to full strength (1.0). A crossover right at the SMA
    (barely triggered) sizes small; one already running scores near the cap.
    """

    def __init__(self, fast: int = 20, slow: int = 50, trend_filter: int | None = None,
                 strength_norm_pct: float = 0.05):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow
        self.trend_filter = trend_filter
        self.strength_norm_pct = strength_norm_pct

    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, SignalResult]:
        signals: dict[str, SignalResult] = {}
        for sym, df in prices.items():
            close = df["Close"]
            min_bars = max(self.slow, self.trend_filter or 0)
            if len(close) < min_bars + 1:
                signals[sym] = SignalResult(Signal.HOLD)
                continue
            fast = close.rolling(self.fast).mean()
            slow = close.rolling(self.slow).mean()
            above_now = fast.iloc[-1] > slow.iloc[-1]
            above_prev = fast.iloc[-2] > slow.iloc[-2]
            if above_now and not above_prev:
                if self.trend_filter:
                    trend = close.rolling(self.trend_filter).mean()
                    if close.iloc[-1] <= trend.iloc[-1]:
                        signals[sym] = SignalResult(Signal.HOLD)
                        continue
                extension_pct = (close.iloc[-1] - slow.iloc[-1]) / slow.iloc[-1]
                strength = max(0.0, min(1.0, extension_pct / self.strength_norm_pct))
                signals[sym] = SignalResult(Signal.BUY, strength)
            elif not above_now and above_prev:
                signals[sym] = SignalResult(Signal.SELL)
            else:
                signals[sym] = SignalResult(Signal.HOLD)
        return signals


class OpeningRangeConfluence(Strategy):
    """Day-trading strategy: opening-range breakout confirmed by EMA trend + RSI momentum.

    Long-only, intended for intraday bars (e.g. 5-minute). Entry requires all
    three signals to agree — confirmation to get in, but a hair trigger to get
    out: any one of a range breakdown, a trend flip, or momentum fading exits.
    Forcing positions flat before the close is enforced by the bot's
    day-trading cycle (run_day_trade_cycle), not by this class.

    A BUY's strength averages three 0..1 sub-scores, each normalized against
    its own *_strength_norm_pct/param so a signal that just barely cleared
    the entry bar scores low and one that cleared it comfortably scores near
    1.0: how far price broke past the opening range, how close RSI sits to
    the top of the bullish band, and how wide the EMA spread is.
    """

    def __init__(self, or_minutes: int = 30, bar_minutes: int = 5,
                 ema_fast: int = 9, ema_slow: int = 21, rsi_period: int = 14,
                 rsi_buy_range: tuple[float, float] = (50, 70),
                 rsi_exit_floor: float = 40, rsi_exit_ceiling: float = 78,
                 breakout_strength_norm_pct: float = 0.005,
                 ema_strength_norm_pct: float = 0.01):
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be shorter than ema_slow")
        self.or_bars = max(1, or_minutes // bar_minutes)
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_buy_min, self.rsi_buy_max = rsi_buy_range
        self.rsi_exit_floor = rsi_exit_floor
        self.rsi_exit_ceiling = rsi_exit_ceiling
        self.breakout_strength_norm_pct = breakout_strength_norm_pct
        self.ema_strength_norm_pct = ema_strength_norm_pct

    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, SignalResult]:
        return {sym: self._signal_for(df) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame) -> SignalResult:
        if df.empty or len(df) < self.ema_slow + 1:
            return SignalResult(Signal.HOLD)

        close = df["Close"]
        fast = ema(close, self.ema_fast)
        slow = ema(close, self.ema_slow)
        r = rsi(close, self.rsi_period)

        today = df.index[-1].date()
        today_bars = df[df.index.date == today]
        if len(today_bars) <= self.or_bars:
            return SignalResult(Signal.HOLD)  # opening range not yet established for today

        opening_range = today_bars.iloc[: self.or_bars]
        or_high = opening_range["High"].max()
        or_low = opening_range["Low"].min()

        last_close = close.iloc[-1]
        last_fast, last_slow, last_rsi = fast.iloc[-1], slow.iloc[-1], r.iloc[-1]

        breakout_up = last_close > or_high
        uptrend = last_fast > last_slow
        bullish_momentum = self.rsi_buy_min <= last_rsi <= self.rsi_buy_max

        breakdown = last_close < or_low
        trend_flip_down = last_fast < last_slow
        momentum_fade = last_rsi < self.rsi_exit_floor or last_rsi > self.rsi_exit_ceiling

        if breakout_up and uptrend and bullish_momentum:
            breakout_score = max(0.0, min(1.0,
                ((last_close - or_high) / or_high) / self.breakout_strength_norm_pct))
            momentum_score = max(0.0, min(1.0,
                (last_rsi - self.rsi_buy_min) / (self.rsi_buy_max - self.rsi_buy_min)))
            trend_score = max(0.0, min(1.0,
                ((last_fast - last_slow) / last_slow) / self.ema_strength_norm_pct))
            strength = (breakout_score + momentum_score + trend_score) / 3
            return SignalResult(Signal.BUY, strength)
        if breakdown or trend_flip_down or momentum_fade:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)
