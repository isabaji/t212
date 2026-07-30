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

    reason: set on a HOLD that used to be a BUY before an anti-chase guard
    suppressed it (see max_chase_pct on each strategy below) -- lets bot.py
    log "skipped, too extended" separately from a genuine no-signal HOLD.
    """

    signal: Signal
    strength: float = 1.0
    reason: str | None = None


class Strategy(ABC):
    @abstractmethod
    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        """Map each symbol to a SignalResult, given its OHLCV history.

        confirm_prices (optional): a second, typically finer-grained bar
        series per symbol that a strategy MAY use as extra confirmation
        before acting on a signal computed from `prices` -- e.g. requiring a
        breakout to hold across several 1-minute closes rather than firing
        on a single coarser bar's close alone. Ignored by strategies that
        don't use it (e.g. SMACrossover).
        """


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

    max_chase_pct: anti-chase guard. If price has already extended past the
    slow SMA by more than this fraction, the BUY is suppressed entirely
    (HOLD, reason="chased") rather than just capped at full strength -- a
    move this far along is more likely to be bought at a local top than to
    be an early, plannable entry. None disables the guard.
    """

    def __init__(self, fast: int = 20, slow: int = 50, trend_filter: int | None = None,
                 strength_norm_pct: float = 0.05, max_chase_pct: float | None = 0.15):
        if fast >= slow:
            raise ValueError("fast window must be shorter than slow window")
        self.fast = fast
        self.slow = slow
        self.trend_filter = trend_filter
        self.strength_norm_pct = strength_norm_pct
        self.max_chase_pct = max_chase_pct

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
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
                if self.max_chase_pct is not None and extension_pct > self.max_chase_pct:
                    signals[sym] = SignalResult(Signal.HOLD, reason="chased")
                    continue
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

    max_chase_pct: anti-chase guard. If price has already run more than this
    fraction past the opening-range high by the time the signal is evaluated
    (e.g. because a check was delayed), the BUY is suppressed entirely (HOLD,
    reason="chased") instead of just capped at full strength -- avoids buying
    into a breakout that's already mostly spent. None disables the guard.

    confirm_bars: extra safety gate on entry only (exits stay hair-trigger,
    unaffected). When generate_signals() is given confirm_prices -- typically
    finer-grained bars than `prices`, e.g. 1-minute vs. this strategy's usual
    5-minute -- a BUY additionally requires the last confirm_bars closes of
    that finer series to all still be above the opening-range high, i.e. the
    breakout has genuinely held rather than spiked above the range on one
    coarse bar and already faded by the time it's acted on. Suppressed BUYs
    get reason="unconfirmed". 0 (or missing/short confirm_prices) disables
    this -- it's an optional extra check, not a requirement.

    min_ema_spread_pct: hard entry gate (backtest-only experiment, not yet
    used by the live bot). Unlike ema_strength_norm_pct -- which only scales
    a BUY's strength score -- this rejects a setup outright (HOLD,
    reason="weak_trend") if (fast EMA - slow EMA) / slow EMA falls short of
    this fraction, i.e. the EMAs have barely separated. None disables the
    gate (default), matching prior behavior where any uptrend, however
    marginal, was an eligible entry.
    """

    def __init__(self, or_minutes: int = 30, bar_minutes: int = 5,
                 ema_fast: int = 9, ema_slow: int = 21, rsi_period: int = 14,
                 rsi_buy_range: tuple[float, float] = (50, 70),
                 rsi_exit_floor: float = 40, rsi_exit_ceiling: float = 78,
                 breakout_strength_norm_pct: float = 0.005,
                 ema_strength_norm_pct: float = 0.01,
                 max_chase_pct: float | None = 0.02,
                 confirm_bars: int = 3,
                 min_ema_spread_pct: float | None = None):
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
        self.max_chase_pct = max_chase_pct
        self.confirm_bars = confirm_bars
        self.min_ema_spread_pct = min_ema_spread_pct

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        confirm_prices = confirm_prices or {}
        return {sym: self._signal_for(df, confirm_prices.get(sym)) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame, confirm_df: pd.DataFrame | None = None) -> SignalResult:
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
            ema_spread_pct = (last_fast - last_slow) / last_slow
            if self.min_ema_spread_pct is not None and ema_spread_pct < self.min_ema_spread_pct:
                return SignalResult(Signal.HOLD, reason="weak_trend")
            breakout_pct = (last_close - or_high) / or_high
            if self.max_chase_pct is not None and breakout_pct > self.max_chase_pct:
                return SignalResult(Signal.HOLD, reason="chased")
            if self.confirm_bars and confirm_df is not None and len(confirm_df) >= self.confirm_bars:
                recent_closes = confirm_df["Close"].iloc[-self.confirm_bars:]
                if not (recent_closes > or_high).all():
                    return SignalResult(Signal.HOLD, reason="unconfirmed")
            breakout_score = max(0.0, min(1.0, breakout_pct / self.breakout_strength_norm_pct))
            momentum_score = max(0.0, min(1.0,
                (last_rsi - self.rsi_buy_min) / (self.rsi_buy_max - self.rsi_buy_min)))
            trend_score = max(0.0, min(1.0, ema_spread_pct / self.ema_strength_norm_pct))
            strength = (breakout_score + momentum_score + trend_score) / 3
            return SignalResult(Signal.BUY, strength)
        if breakdown or trend_flip_down or momentum_fade:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)
