"""Strategy interface and a simple SMA-crossover example.

To add your own strategy, subclass Strategy and implement generate_signals().
"""

from abc import ABC, abstractmethod
from enum import Enum

import pandas as pd

from .indicators import ema, rsi


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Strategy(ABC):
    @abstractmethod
    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, Signal]:
        """Map each symbol to a Signal, given its OHLCV history."""


class SMACrossover(Strategy):
    """Buy when the fast SMA crosses above the slow SMA; sell on the reverse cross.

    Deliberately simple — a starting point, not an edge.
    """

    def __init__(self, fast: int = 20, slow: int = 50):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow

    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, Signal]:
        signals: dict[str, Signal] = {}
        for sym, df in prices.items():
            close = df["Close"]
            if len(close) < self.slow + 1:
                signals[sym] = Signal.HOLD
                continue
            fast = close.rolling(self.fast).mean()
            slow = close.rolling(self.slow).mean()
            above_now = fast.iloc[-1] > slow.iloc[-1]
            above_prev = fast.iloc[-2] > slow.iloc[-2]
            if above_now and not above_prev:
                signals[sym] = Signal.BUY
            elif not above_now and above_prev:
                signals[sym] = Signal.SELL
            else:
                signals[sym] = Signal.HOLD
        return signals


class OpeningRangeConfluence(Strategy):
    """Day-trading strategy: opening-range breakout confirmed by EMA trend + RSI momentum.

    Long-only, intended for intraday bars (e.g. 5-minute). Entry requires all
    three signals to agree — confirmation to get in, but a hair trigger to get
    out: any one of a range breakdown, a trend flip, or momentum fading exits.
    Forcing positions flat before the close is enforced by the bot's
    day-trading cycle (run_day_trade_cycle), not by this class.
    """

    def __init__(self, or_minutes: int = 30, bar_minutes: int = 5,
                 ema_fast: int = 9, ema_slow: int = 21, rsi_period: int = 14,
                 rsi_buy_range: tuple[float, float] = (50, 70),
                 rsi_exit_floor: float = 40, rsi_exit_ceiling: float = 78):
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be shorter than ema_slow")
        self.or_bars = max(1, or_minutes // bar_minutes)
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_buy_min, self.rsi_buy_max = rsi_buy_range
        self.rsi_exit_floor = rsi_exit_floor
        self.rsi_exit_ceiling = rsi_exit_ceiling

    def generate_signals(self, prices: dict[str, pd.DataFrame]) -> dict[str, Signal]:
        return {sym: self._signal_for(df) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame) -> Signal:
        if df.empty or len(df) < self.ema_slow + 1:
            return Signal.HOLD

        close = df["Close"]
        fast = ema(close, self.ema_fast)
        slow = ema(close, self.ema_slow)
        r = rsi(close, self.rsi_period)

        today = df.index[-1].date()
        today_bars = df[df.index.date == today]
        if len(today_bars) <= self.or_bars:
            return Signal.HOLD  # opening range not yet established for today

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
            return Signal.BUY
        if breakdown or trend_flip_down or momentum_fade:
            return Signal.SELL
        return Signal.HOLD
