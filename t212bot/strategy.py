"""Strategy interface and a simple SMA-crossover example.

To add your own strategy, subclass Strategy and implement generate_signals().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from .indicators import bollinger_bands, ema, rsi, vwap


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
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        """Map each symbol to a SignalResult, given its OHLCV history.

        confirm_prices (optional): a second, typically finer-grained bar
        series per symbol that a strategy MAY use as extra confirmation
        before acting on a signal computed from `prices` -- e.g. requiring a
        breakout to hold across several 1-minute closes rather than firing
        on a single coarser bar's close alone. Ignored by strategies that
        don't use it (e.g. SMACrossover).

        daily_prices (optional): a per-symbol series of *daily* bars (not
        the intraday bars `prices` is built from), for strategies that want
        a longer-horizon trend read than intraday indicators can see -- e.g.
        OpeningRangeConfluence's trend_filter_days. The caller does not need
        to pre-trim this to "no lookahead"; a strategy using it is
        responsible for excluding any day not strictly before the current
        intraday bar's date itself (see OpeningRangeConfluence._signal_for).
        Ignored by strategies that don't use it.
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
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
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


class LongTermTrendConfluence(Strategy):
    """Swing strategy: buy when price is above both its long-horizon SMA and
    EMA (200-day by default) -- both agree on a long-term uptrend; sell when
    price drops below either one.

    Event-driven like SMACrossover: BUY fires only on the bar where "above
    both" first becomes true, not on every bar it stays true (a resting HOLD
    the rest of the time keeps behavior and logs consistent with the other
    strategies here; bot.py's own position check already makes re-signaling
    BUY while holding harmless, so this is about clean logs, not correctness).
    SELL fires on the bar where "above both" first becomes false.

    Combining an SMA and an EMA over the *same* period requires two
    differently-weighted reads of the same window to agree, rather than
    trusting one long moving average alone -- EMA weights recent closes more
    heavily, so it can cross a little earlier than the SMA on a genuine
    shift, but also whipsaw a little more on noise the SMA would smooth
    through. Requiring both sides of that trade-off to agree is the point,
    the same way OpeningRangeConfluence requires breakout + trend + momentum
    to agree rather than trading on any one alone.

    strength_norm_pct: a BUY's strength is how far price sits above its own
    long-horizon SMA (the steadier of the two lines) as a fraction of that
    SMA, scaled so strength_norm_pct of extension maps to full strength
    (1.0) -- same approach as SMACrossover.

    max_chase_pct: anti-chase guard, same idea as SMACrossover -- if price
    has already extended past the SMA by more than this fraction, the BUY is
    suppressed entirely (HOLD, reason="chased") rather than sized down,
    since a move this extended above a 200-day average reads more like a
    blow-off than a fresh, plannable long-term entry. None disables it.
    """

    def __init__(self, sma_period: int = 200, ema_period: int = 200,
                 strength_norm_pct: float = 0.10, max_chase_pct: float | None = 0.30):
        self.sma_period = sma_period
        self.ema_period = ema_period
        self.strength_norm_pct = strength_norm_pct
        self.max_chase_pct = max_chase_pct

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        signals: dict[str, SignalResult] = {}
        min_bars = max(self.sma_period, self.ema_period)
        for sym, df in prices.items():
            close = df["Close"]
            if len(close) < min_bars + 1:
                signals[sym] = SignalResult(Signal.HOLD)
                continue
            sma_line = close.rolling(self.sma_period).mean()
            ema_line = ema(close, self.ema_period)

            above_both_now = close.iloc[-1] > sma_line.iloc[-1] and close.iloc[-1] > ema_line.iloc[-1]
            above_both_prev = close.iloc[-2] > sma_line.iloc[-2] and close.iloc[-2] > ema_line.iloc[-2]

            if above_both_now and not above_both_prev:
                extension_pct = (close.iloc[-1] - sma_line.iloc[-1]) / sma_line.iloc[-1]
                if self.max_chase_pct is not None and extension_pct > self.max_chase_pct:
                    signals[sym] = SignalResult(Signal.HOLD, reason="chased")
                    continue
                strength = max(0.0, min(1.0, extension_pct / self.strength_norm_pct))
                signals[sym] = SignalResult(Signal.BUY, strength)
            elif not above_both_now and above_both_prev:
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

    min_ema_spread_pct: hard entry gate. Unlike ema_strength_norm_pct --
    which only scales a BUY's strength score -- this rejects a setup outright
    (HOLD, reason="weak_trend") if (fast EMA - slow EMA) / slow EMA falls
    short of this fraction, i.e. the EMAs have barely separated. None
    disables the gate, matching prior behavior where any uptrend, however
    marginal, was an eligible entry.

    min_strength: hard entry gate (backtest-only experiment, not yet used by
    the live bot) on the *combined* setup quality -- the same strength score
    (average of the breakout/momentum/trend sub-scores) that otherwise only
    feeds position sizing. Rejects outright (HOLD, reason="weak_signal") if
    strength falls short, rather than just sizing a marginal setup smaller.
    None disables the gate.

    min_volume_ratio / volume_avg_period: hard entry gate (backtest-only
    experiment) requiring the breakout bar's Volume to be at least
    min_volume_ratio times the trailing volume_avg_period-bar average volume
    (computed excluding the breakout bar itself, so it's a real "is this
    move backed by above-average participation" check, not self-referential).
    Rejects outright (HOLD, reason="low_volume") if not met. None disables
    the gate; if there isn't yet volume_avg_period bars of history the gate
    is skipped (fails open), same as confirm_bars with a short confirm_df.

    require_retest / retest_tolerance_pct: replaces the entry trigger itself
    (backtest-only experiment). Instead of buying the *first* bar to close
    above the opening-range high (which is what every setting above still
    does -- all of them filter around that same trigger), this waits for a
    pullback-and-hold pattern: since the opening range, price must have (1)
    broken above or_high, (2) pulled back to within retest_tolerance_pct
    below or_high without ever needing to fall further (a true retest of the
    former resistance as new support, not a failed breakout), and (3) the
    current bar closes back above or_high again. The pullback and the
    reclaim can be the same bar (a single-bar dip-and-recover) or different
    bars. All the other gates above (confirm_bars, min_ema_spread_pct, etc.)
    still apply on top once this trigger fires. False when require_retest is
    False (default) -- unrelated to and does not change existing behavior.

    trend_filter_days: hard entry gate (backtest-only experiment) on a
    longer horizon than anything else here -- everything above reads only
    the intraday bars in `prices` (hours, not weeks). This instead requires
    the prior *daily* close (from generate_signals' daily_prices, not
    `prices`) to be above its own trailing trend_filter_days-day SMA,
    i.e. the symbol is in a genuine multi-week uptrend, not just showing an
    intraday breakout inside a longer downtrend or chop. Rejects outright
    (HOLD, reason="weak_daily_trend") if not met. Only ever looks at daily
    bars strictly before the current intraday bar's own calendar date, so
    it can't see a same-day close that hasn't happened yet even if a caller
    passes an unfiltered daily series (see _signal_for). None disables the
    gate; if daily_prices doesn't have this symbol, or there aren't yet
    trend_filter_days+1 prior days of it, the gate is skipped (fails open),
    same philosophy as confirm_bars/min_volume_ratio with too-short history.
    """

    def __init__(self, or_minutes: int = 30, bar_minutes: int = 5,
                 ema_fast: int = 9, ema_slow: int = 21, rsi_period: int = 14,
                 rsi_buy_range: tuple[float, float] = (50, 70),
                 rsi_exit_floor: float = 40, rsi_exit_ceiling: float = 78,
                 breakout_strength_norm_pct: float = 0.005,
                 ema_strength_norm_pct: float = 0.01,
                 max_chase_pct: float | None = 0.02,
                 confirm_bars: int = 3,
                 min_ema_spread_pct: float | None = None,
                 min_strength: float | None = None,
                 min_volume_ratio: float | None = None,
                 volume_avg_period: int = 20,
                 require_retest: bool = False,
                 retest_tolerance_pct: float = 0.003,
                 trend_filter_days: int | None = None):
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
        self.require_retest = require_retest
        self.retest_tolerance_pct = retest_tolerance_pct
        self.min_ema_spread_pct = min_ema_spread_pct
        self.min_strength = min_strength
        self.min_volume_ratio = min_volume_ratio
        self.volume_avg_period = volume_avg_period
        self.trend_filter_days = trend_filter_days

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        confirm_prices = confirm_prices or {}
        daily_prices = daily_prices or {}
        return {sym: self._signal_for(df, confirm_prices.get(sym), daily_prices.get(sym))
                for sym, df in prices.items()}

    def _has_retest_setup(self, today_bars: pd.DataFrame, or_high: float) -> bool:
        """True if, since the opening range, price broke above or_high,
        pulled back to within retest_tolerance_pct below or_high (retesting
        it as support rather than failing straight through it), and the
        current (last) bar has reclaimed or_high again. The pullback and the
        reclaim may be the same bar."""
        post_range = today_bars.iloc[self.or_bars:]
        if len(post_range) < 2:
            return False
        prior = post_range.iloc[:-1]
        current = post_range.iloc[-1]
        broke = prior[prior["High"] > or_high]
        if broke.empty:
            return False
        since_break = post_range.loc[broke.index[0]:]
        retest_low_bound = or_high * (1 - self.retest_tolerance_pct)
        retested = ((since_break["Low"] <= or_high) & (since_break["Low"] >= retest_low_bound)).any()
        return bool(retested) and current["Close"] > or_high

    def _signal_for(self, df: pd.DataFrame, confirm_df: pd.DataFrame | None = None,
                     daily_df: pd.DataFrame | None = None) -> SignalResult:
        if df.empty or len(df) < max(self.ema_slow, self.volume_avg_period) + 1:
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

        if self.require_retest:
            breakout_up = self._has_retest_setup(today_bars, or_high)
        else:
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
            if self.trend_filter_days is not None and daily_df is not None:
                today = df.index[-1].date()
                prior_daily = daily_df[daily_df.index.date < today]
                if len(prior_daily) >= self.trend_filter_days:
                    daily_sma = prior_daily["Close"].rolling(self.trend_filter_days).mean().iloc[-1]
                    last_daily_close = prior_daily["Close"].iloc[-1]
                    if pd.notna(daily_sma) and last_daily_close < daily_sma:
                        return SignalResult(Signal.HOLD, reason="weak_daily_trend")
            if self.min_volume_ratio is not None and "Volume" in df.columns:
                avg_volume = df["Volume"].rolling(self.volume_avg_period).mean().shift(1).iloc[-1]
                if pd.notna(avg_volume) and avg_volume > 0:
                    last_volume = df["Volume"].iloc[-1]
                    if last_volume < avg_volume * self.min_volume_ratio:
                        return SignalResult(Signal.HOLD, reason="low_volume")
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
            if self.min_strength is not None and strength < self.min_strength:
                return SignalResult(Signal.HOLD, reason="weak_signal")
            return SignalResult(Signal.BUY, strength)
        if breakdown or trend_flip_down or momentum_fade:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)


class MeanReversionPullback(Strategy):
    """Day-trading strategy: buy pullbacks to the fast EMA within an
    established uptrend, instead of chasing breakouts (see
    OpeningRangeConfluence). A genuinely different bet, not a variant of the
    breakout strategy's entry: that every backtest this session confirmed
    OpeningRangeConfluence's breakouts have a ~30% win rate ceiling regardless
    of how the initial-breakout trigger is filtered or replaced with a retest
    suggests these liquid large-caps may mean-revert intraday more reliably
    than they trend. Long-only, intended for intraday bars (e.g. 5-minute).

    A first-pass version of this (checked into an earlier commit, no
    min_pullback_depth_pct/min_ema_spread_pct/prior-strength check below)
    overtraded badly -- 2.8x OpeningRangeConfluence's trade frequency on the
    same 60-day backtest, because a fast EMA(9) tracks 5-minute price closely
    enough that ordinary noise constantly "touches" it. The three additions
    below exist specifically to fix that, not for their own sake.

    Entry requires all of:
      - uptrend: fast EMA above slow EMA (the near-term trend is up)
      - min_ema_spread_pct: that uptrend must be genuinely established, not
        barely-there -- same hard gate that helped OpeningRangeConfluence's
        win rate (see min_ema_spread_pct there). None disables it.
      - pullback: the current bar's Low actually reached at least
        min_pullback_depth_pct below the fast EMA -- a real retracement, not
        just brushing past a line that's already hugging price
      - bounce: the current bar's Close has reclaimed the fast EMA, i.e. the
        pullback found support and reversed within the same bar it dipped in
      - momentum band: RSI(14) between rsi_floor and rsi_ceiling -- cooled
        off enough to be a real pullback (rules out buying a shallow wiggle
        or an already-overbought bounce with no room left) but not so
        depressed it looks like a real breakdown wearing a pullback's face
      - prior_strength_rsi_min: RSI must have reached at least this level at
        some point in the trailing prior_strength_lookback_bars -- confirms
        there was an actual rally to pull back from, rather than the fast
        EMA just chopping sideways through flat, noisy price. None disables
        this (stateless proxy for "don't keep re-buying the same range" --
        this class has no persistent memory between live bot cycles, since
        main.py builds a fresh instance each cron run, so a real cooldown
        isn't possible; this is the data-only substitute).

    max_chase_pct: if price has already run more than this fraction above
    the fast EMA by the time the signal is evaluated, the BUY is suppressed
    (HOLD, reason="chased") -- same anti-chase idea as OpeningRangeConfluence,
    applied to how far the bounce has already gone rather than a breakout.

    A BUY's strength averages three 0..1 sub-scores: how wide the EMA
    spread is (trend conviction), how deep the pullback was relative to the
    fast EMA (deeper means more room for the bounce to run), and how far RSI
    sits from rsi_ceiling (lower RSI within the band scores higher -- bought
    closer to the dip's low, not after it's already recovered most of the
    way).

    Exit is a hair trigger, same philosophy as OpeningRangeConfluence: any
    one of the trend flipping down, price closing below the lowest low of
    the trailing pullback_lookback_bars (the level this bounce is supposed
    to be defending), or RSI hitting either exit_floor or exit_ceiling
    closes the position. EOD flatten is enforced by the bot's day-trading
    cycle (run_day_trade_cycle), not this class.
    """

    def __init__(self, ema_fast: int = 9, ema_slow: int = 21, rsi_period: int = 14,
                 min_pullback_depth_pct: float = 0.0015,
                 rsi_floor: float = 35, rsi_ceiling: float = 60,
                 rsi_exit_floor: float = 25, rsi_exit_ceiling: float = 75,
                 max_chase_pct: float | None = 0.01,
                 pullback_lookback_bars: int = 12,
                 pullback_strength_norm_pct: float = 0.005,
                 ema_strength_norm_pct: float = 0.01,
                 min_ema_spread_pct: float | None = 0.001,
                 prior_strength_lookback_bars: int = 12,
                 prior_strength_rsi_min: float | None = 55):
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be shorter than ema_slow")
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.min_pullback_depth_pct = min_pullback_depth_pct
        self.rsi_floor = rsi_floor
        self.rsi_ceiling = rsi_ceiling
        self.rsi_exit_floor = rsi_exit_floor
        self.rsi_exit_ceiling = rsi_exit_ceiling
        self.max_chase_pct = max_chase_pct
        self.pullback_lookback_bars = pullback_lookback_bars
        self.pullback_strength_norm_pct = pullback_strength_norm_pct
        self.ema_strength_norm_pct = ema_strength_norm_pct
        self.min_ema_spread_pct = min_ema_spread_pct
        self.prior_strength_lookback_bars = prior_strength_lookback_bars
        self.prior_strength_rsi_min = prior_strength_rsi_min

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        return {sym: self._signal_for(df) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame) -> SignalResult:
        min_bars = max(self.ema_slow, self.pullback_lookback_bars,
                        self.prior_strength_lookback_bars) + 1
        if df.empty or len(df) < min_bars:
            return SignalResult(Signal.HOLD)

        close = df["Close"]
        fast = ema(close, self.ema_fast)
        slow = ema(close, self.ema_slow)
        r = rsi(close, self.rsi_period)

        last_close = close.iloc[-1]
        last_low = df["Low"].iloc[-1]
        last_fast, last_slow, last_rsi = fast.iloc[-1], slow.iloc[-1], r.iloc[-1]

        uptrend = last_fast > last_slow
        ema_spread_pct = (last_fast - last_slow) / last_slow
        pullback_depth_pct = (last_fast - last_low) / last_fast
        dipped_to_fast_ema = pullback_depth_pct >= self.min_pullback_depth_pct
        reclaimed = last_close > last_fast
        momentum_ok = self.rsi_floor <= last_rsi <= self.rsi_ceiling
        if self.prior_strength_rsi_min is not None:
            prior_rsi = r.iloc[-self.prior_strength_lookback_bars - 1:-1]
            had_prior_strength = bool((prior_rsi >= self.prior_strength_rsi_min).any())
        else:
            had_prior_strength = True

        recent_low = df["Low"].iloc[-self.pullback_lookback_bars - 1:-1].min()
        trend_flip_down = last_fast < last_slow
        breakdown = last_close < recent_low
        momentum_extreme = last_rsi < self.rsi_exit_floor or last_rsi > self.rsi_exit_ceiling

        if uptrend and dipped_to_fast_ema and reclaimed and momentum_ok and had_prior_strength:
            if self.min_ema_spread_pct is not None and ema_spread_pct < self.min_ema_spread_pct:
                return SignalResult(Signal.HOLD, reason="weak_trend")
            chase_pct = (last_close - last_fast) / last_fast
            if self.max_chase_pct is not None and chase_pct > self.max_chase_pct:
                return SignalResult(Signal.HOLD, reason="chased")
            trend_score = max(0.0, min(1.0, ema_spread_pct / self.ema_strength_norm_pct))
            pullback_score = max(0.0, min(1.0, pullback_depth_pct / self.pullback_strength_norm_pct))
            momentum_score = max(0.0, min(1.0,
                (self.rsi_ceiling - last_rsi) / (self.rsi_ceiling - self.rsi_floor)))
            strength = (trend_score + pullback_score + momentum_score) / 3
            return SignalResult(Signal.BUY, strength)
        if trend_flip_down or breakdown or momentum_extreme:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)


class VWAPReclaim(Strategy):
    """Day-trading strategy: buy when price reclaims the session's
    volume-weighted average price (VWAP) from below, with RSI confirming
    momentum. A third, structurally distinct signal (see EnsembleVote) --
    unlike OpeningRangeConfluence (a fixed opening-range level) or
    MeanReversionPullback (EMA-based pullback depth), VWAP is anchored to
    cumulative traded volume for the session, resets every day, and reacts
    to volume directly rather than only price. Long-only, intraday bars.

    Entry requires both:
      - reclaim: the previous bar closed at or below VWAP and the current
        bar closed above it -- price just crossed back above the session's
        volume-weighted "fair value", not merely drifting above a level it
        was already clear of
      - momentum band: RSI(14) between rsi_floor and rsi_ceiling -- real
        confirming momentum, not overbought or still negative

    Exit is a hair trigger: price closing back below VWAP, or RSI hitting
    rsi_exit_floor/rsi_exit_ceiling, closes the position. EOD flatten is
    enforced by the bot's day-trading cycle, not this class.

    A BUY's strength averages two 0..1 sub-scores: how far price has
    reclaimed above VWAP (normalized by vwap_distance_norm_pct) and how far
    RSI sits above rsi_floor within the entry band.
    """

    def __init__(self, rsi_period: int = 14, rsi_floor: float = 45, rsi_ceiling: float = 70,
                 rsi_exit_floor: float = 30, rsi_exit_ceiling: float = 80,
                 vwap_distance_norm_pct: float = 0.003):
        self.rsi_period = rsi_period
        self.rsi_floor = rsi_floor
        self.rsi_ceiling = rsi_ceiling
        self.rsi_exit_floor = rsi_exit_floor
        self.rsi_exit_ceiling = rsi_exit_ceiling
        self.vwap_distance_norm_pct = vwap_distance_norm_pct

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        return {sym: self._signal_for(df) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame) -> SignalResult:
        if df.empty or len(df) < self.rsi_period + 2 or "Volume" not in df.columns:
            return SignalResult(Signal.HOLD)

        v = vwap(df)
        r = rsi(df["Close"], self.rsi_period)
        last_close, prev_close = df["Close"].iloc[-1], df["Close"].iloc[-2]
        last_vwap, prev_vwap = v.iloc[-1], v.iloc[-2]
        last_rsi = r.iloc[-1]
        if pd.isna(prev_vwap) or pd.isna(last_vwap):
            return SignalResult(Signal.HOLD)

        reclaimed = prev_close <= prev_vwap and last_close > last_vwap
        momentum_ok = self.rsi_floor <= last_rsi <= self.rsi_ceiling
        below_vwap = last_close < last_vwap
        momentum_extreme = last_rsi < self.rsi_exit_floor or last_rsi > self.rsi_exit_ceiling

        if reclaimed and momentum_ok:
            distance_pct = (last_close - last_vwap) / last_vwap
            distance_score = max(0.0, min(1.0, distance_pct / self.vwap_distance_norm_pct))
            momentum_score = max(0.0, min(1.0,
                (last_rsi - self.rsi_floor) / (self.rsi_ceiling - self.rsi_floor)))
            strength = (distance_score + momentum_score) / 2
            return SignalResult(Signal.BUY, strength)
        if below_vwap or momentum_extreme:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)


class BollingerSqueezeBreakout(Strategy):
    """Day-trading strategy: buy a breakout above the upper Bollinger Band
    following a volatility squeeze -- a fourth, structurally distinct signal
    (see EnsembleVote). Unlike OpeningRangeConfluence (a fixed opening-range
    level), MeanReversionPullback (EMA-based pullback depth), or VWAPReclaim
    (a volume-weighted price level), this reacts to *volatility* directly:
    it looks for a period of unusually narrow bands (consolidation) followed
    by an expansion through the upper band, rather than any fixed price
    level or moving average. Long-only, intraday bars.

    Entry requires all of:
      - squeeze: band width ((upper - lower) / middle) was at or below its
        own trailing squeeze_percentile (within squeeze_lookback_bars of its
        own history) at some point in the squeeze_confirm_bars bars
        immediately *preceding* the current bar -- confirms a genuine recent
        contraction. Deliberately excludes the current (breakout) bar itself:
        bands mechanically widen as soon as a real breakout candle enters
        them, so requiring the squeeze to still hold on the breakout bar
        would exclude the very breakout it's meant to catch.
      - breakout: the current bar's Close is above the upper band
      - momentum band: RSI(14) between rsi_floor and rsi_ceiling -- real
        confirming momentum, not already overbought

    Exit is a hair trigger, same philosophy as the other strategies here: the
    current bar's Close falling back below the middle band (SMA) -- the
    volatility expansion this trade was betting on has stalled or reversed
    -- or RSI hitting rsi_exit_floor/rsi_exit_ceiling, closes the position.
    EOD flatten is enforced by the bot's day-trading cycle, not this class.

    A BUY's strength averages two 0..1 sub-scores: how tight the squeeze was
    relative to its own recent average width (tighter scores higher -- more
    energy released into the breakout) and how far RSI sits within the entry
    band.
    """

    def __init__(self, bb_period: int = 20, bb_std: float = 2.0,
                 squeeze_lookback_bars: int = 60, squeeze_percentile: float = 0.2,
                 squeeze_confirm_bars: int = 3,
                 rsi_period: int = 14, rsi_floor: float = 50, rsi_ceiling: float = 75,
                 rsi_exit_floor: float = 30, rsi_exit_ceiling: float = 85):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.squeeze_lookback_bars = squeeze_lookback_bars
        self.squeeze_percentile = squeeze_percentile
        self.squeeze_confirm_bars = squeeze_confirm_bars
        self.rsi_period = rsi_period
        self.rsi_floor = rsi_floor
        self.rsi_ceiling = rsi_ceiling
        self.rsi_exit_floor = rsi_exit_floor
        self.rsi_exit_ceiling = rsi_exit_ceiling

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        return {sym: self._signal_for(df) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame) -> SignalResult:
        # Band width itself needs bb_period bars of warmup before it produces
        # a value at all, and squeeze_threshold's rolling quantile then needs
        # squeeze_lookback_bars of *valid width* on top of that -- so the
        # real requirement is the sum, not the max, of the two.
        min_bars = self.bb_period + self.squeeze_lookback_bars + 1
        if df.empty or len(df) < min_bars:
            return SignalResult(Signal.HOLD)

        close = df["Close"]
        middle, upper, _ = bollinger_bands(close, self.bb_period, self.bb_std)
        width = (upper - middle) * 2 / middle
        r = rsi(close, self.rsi_period)

        last_close = close.iloc[-1]
        last_middle, last_upper = middle.iloc[-1], upper.iloc[-1]
        last_rsi = r.iloc[-1]

        squeeze_threshold = width.rolling(self.squeeze_lookback_bars).quantile(self.squeeze_percentile)
        # Excludes the current bar -- see squeeze docstring above.
        recent_width = width.iloc[-self.squeeze_confirm_bars - 1:-1]
        recent_threshold = squeeze_threshold.iloc[-self.squeeze_confirm_bars - 1:-1]
        was_squeezed = bool((recent_width <= recent_threshold).any())

        breakout = last_close > last_upper
        momentum_ok = self.rsi_floor <= last_rsi <= self.rsi_ceiling

        below_middle = last_close < last_middle
        momentum_extreme = last_rsi < self.rsi_exit_floor or last_rsi > self.rsi_exit_ceiling

        if breakout and was_squeezed and momentum_ok:
            current_width = width.iloc[-1]
            recent_avg_width = width.iloc[-self.squeeze_lookback_bars:].mean()
            tightness_pct = 1 - (current_width / recent_avg_width) if recent_avg_width else 0.0
            tightness_score = max(0.0, min(1.0, tightness_pct))
            momentum_score = max(0.0, min(1.0,
                (last_rsi - self.rsi_floor) / (self.rsi_ceiling - self.rsi_floor)))
            strength = (tightness_score + momentum_score) / 2
            return SignalResult(Signal.BUY, strength)
        if below_middle or momentum_extreme:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)


class GapFillReversal(Strategy):
    """Day-trading strategy: buy a gap-down open that's reversing back up
    early in the session -- a fifth, structurally distinct signal (see
    EnsembleVote). Unlike OpeningRangeConfluence (an intraday breakout),
    MeanReversionPullback (an EMA-anchored pullback), VWAPReclaim (a
    volume-weighted level), or BollingerSqueezeBreakout (a volatility
    expansion), this keys off *overnight* information -- how far today's
    open gapped from yesterday's last close -- rather than anything
    computed purely from today's intraday bars. Long-only, intraday bars.

    Entry requires all of:
      - gap down: today's opening print is at least min_gap_pct below
        yesterday's last close -- a real overnight gap, not noise
      - early session: still within early_session_bars of today's open --
        gap dynamics (and whatever edge there is in fading them) decay
        fast; this isn't meant to fire hours into the session
      - reversal: the current bar's Close has both reclaimed today's
        opening print and moved back above the lowest low the session has
        made *before* this bar -- the gap is actively filling, not still
        falling. (Excludes the current bar from that low on purpose, same
        pattern as MeanReversionPullback's breakdown check -- otherwise the
        comparison is close to tautological, since a bar's Close is always
        within its own [Low, High].) HOLDs on a session's very first bar,
        since there's no prior low yet to have reversed off of.
      - momentum turning, not falling: RSI(14) between rsi_floor and
        rsi_ceiling -- recovering from oversold, not still making new lows
        (too low) and not already fully round-tripped (too high, nothing
        left to fill)

    Exit is a hair trigger, same philosophy as the other strategies here:
    the current bar's Close falling back to/through the session's prior low
    (the reversal failed), or RSI hitting rsi_exit_floor/rsi_exit_ceiling,
    closes the position. EOD flatten is enforced by the bot's day-trading
    cycle, not this class.

    A BUY's strength averages two 0..1 sub-scores: how large the original
    gap was (normalized by gap_strength_norm_pct -- a deeper gap scores
    higher, more room left to fill) and how far RSI sits within the entry
    band.
    """

    def __init__(self, min_gap_pct: float = 0.003, early_session_bars: int = 12,
                 gap_strength_norm_pct: float = 0.01,
                 rsi_period: int = 14, rsi_floor: float = 35, rsi_ceiling: float = 60,
                 rsi_exit_floor: float = 20, rsi_exit_ceiling: float = 75):
        self.min_gap_pct = min_gap_pct
        self.early_session_bars = early_session_bars
        self.gap_strength_norm_pct = gap_strength_norm_pct
        self.rsi_period = rsi_period
        self.rsi_floor = rsi_floor
        self.rsi_ceiling = rsi_ceiling
        self.rsi_exit_floor = rsi_exit_floor
        self.rsi_exit_ceiling = rsi_exit_ceiling

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        return {sym: self._signal_for(df) for sym, df in prices.items()}

    def _signal_for(self, df: pd.DataFrame) -> SignalResult:
        if df.empty or len(df) < self.rsi_period + 2:
            return SignalResult(Signal.HOLD)

        today = df.index[-1].date()
        today_bars = df[df.index.date == today]
        prior_bars = df[df.index.date < today]
        if today_bars.empty or prior_bars.empty:
            return SignalResult(Signal.HOLD)

        allow_entry = len(today_bars) <= self.early_session_bars

        prior_close = prior_bars["Close"].iloc[-1]
        today_open = today_bars["Open"].iloc[0]
        gap_pct = (today_open - prior_close) / prior_close

        last_close = df["Close"].iloc[-1]
        r = rsi(df["Close"], self.rsi_period)
        last_rsi = r.iloc[-1]

        prior_today_lows = today_bars["Low"].iloc[:-1]
        session_low_prior = prior_today_lows.min() if len(prior_today_lows) > 0 else None

        gapped_down = gap_pct <= -self.min_gap_pct
        reclaimed_open = last_close > today_open
        off_the_low = session_low_prior is not None and last_close > session_low_prior
        momentum_ok = self.rsi_floor <= last_rsi <= self.rsi_ceiling

        broke_new_low = session_low_prior is not None and last_close <= session_low_prior
        momentum_extreme = last_rsi < self.rsi_exit_floor or last_rsi > self.rsi_exit_ceiling

        if allow_entry and gapped_down and reclaimed_open and off_the_low and momentum_ok:
            gap_score = max(0.0, min(1.0, abs(gap_pct) / self.gap_strength_norm_pct))
            momentum_score = max(0.0, min(1.0,
                (last_rsi - self.rsi_floor) / (self.rsi_ceiling - self.rsi_floor)))
            strength = (gap_score + momentum_score) / 2
            return SignalResult(Signal.BUY, strength)
        if broke_new_low or momentum_extreme:
            return SignalResult(Signal.SELL)
        return SignalResult(Signal.HOLD)


class EnsembleVote(Strategy):
    """Combines multiple day-trade strategies, requiring at least min_votes
    of them to independently signal BUY before this wrapper signals BUY --
    consensus across structurally different strategies, rather than
    filtering false positives within one strategy's own signal (everything
    else this session). Exit stays a hair trigger: if ANY sub-strategy
    signals SELL, this wrapper signals SELL -- consensus is required to get
    in, but any one strategy losing confidence is enough to get out, the
    same asymmetric philosophy every other strategy here uses.

    strategies: the sub-strategies to combine (>= 2), each evaluated
    against the same `prices` (and `confirm_prices`, passed through
    unchanged -- sub-strategies that don't use it just ignore it).

    min_votes: how many of the sub-strategies must agree. None (default)
    means unanimous (all of them) -- the first thing tried, which on a
    3-strategy ensemble turned out to be far too strict to be practically
    testable (11 trades across a 60-day, 30-symbol backtest). A majority
    (e.g. 2 of 3) is meaningfully looser without dropping the cross-strategy
    consensus idea entirely.

    required: sub-strategies that must vote BUY for *any* combination to
    count, on top of min_votes -- e.g. with 3 strategies, min_votes=2 and
    required=[orb] accepts orb+mr or orb+gap but rejects mr+gap even though
    it's also "2 of 3", because a plain min_votes threshold treats every
    combination as equally valid and can't express "but not that one pair."
    Added specifically because a 60-day/30-symbol backtest showed
    MeanReversionPullback+GapFillReversal agreeing on its own is a weak,
    sparse combination (~11-18% win rate) dragging down the blended
    ensemble average, while the other two pairs (both including
    OpeningRangeConfluence) were consistently the strong ones. Must be a
    subset of `strategies` (checked by identity -- pass the same instances).
    None (default) disables the gate, matching prior behavior.

    A BUY's strength averages the strengths of only the strategies that
    voted BUY (not all of them), so a marginal-but-sufficient consensus
    still sizes smaller than a strong one. A BUY's reason is set to the
    "+"-joined class names of the strategies that voted for it (e.g.
    "MeanReversionPullback+VWAPReclaim"), so a backtest can break down
    performance by which combination of strategies agreed -- see
    _build_trades' entry_reason.
    """

    def __init__(self, strategies: list[Strategy], min_votes: int | None = None,
                 required: list[Strategy] | None = None):
        if len(strategies) < 2:
            raise ValueError("EnsembleVote needs at least 2 sub-strategies")
        self.strategies = strategies
        self.min_votes = min_votes if min_votes is not None else len(strategies)
        self.required_indices = [i for i, s in enumerate(strategies) if s in (required or [])]

    def generate_signals(self, prices: dict[str, pd.DataFrame],
                          confirm_prices: dict[str, pd.DataFrame] | None = None,
                          daily_prices: dict[str, pd.DataFrame] | None = None) -> dict[str, SignalResult]:
        all_signals = [s.generate_signals(prices, confirm_prices, daily_prices) for s in self.strategies]
        result = {}
        for sym in prices:
            sigs = [sig_map.get(sym, SignalResult(Signal.HOLD)) for sig_map in all_signals]
            if any(s.signal is Signal.SELL for s in sigs):
                result[sym] = SignalResult(Signal.SELL)
                continue
            buy_indices = [i for i, s in enumerate(sigs) if s.signal is Signal.BUY]
            required_met = all(i in buy_indices for i in self.required_indices)
            if required_met and len(buy_indices) >= self.min_votes:
                strength = sum(sigs[i].strength for i in buy_indices) / len(buy_indices)
                voters = "+".join(type(self.strategies[i]).__name__ for i in buy_indices)
                result[sym] = SignalResult(Signal.BUY, strength, reason=voters)
            else:
                result[sym] = SignalResult(Signal.HOLD)
        return result
