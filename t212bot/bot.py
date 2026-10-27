"""One trading cycle: fetch data -> generate signals -> apply risk -> execute.

Stateless by design: run it on a schedule (cron / GitHub Actions). The current
portfolio is read from Trading212 each cycle, so restarts are safe. Every
cycle — success or failure — is logged to logs/history.jsonl (see history.py),
which is what feeds the published dashboard.
"""

import datetime
import logging
from zoneinfo import ZoneInfo

import pandas as pd

from . import companies, daily_target, discovery, events, history, news, news_llm, pnl_history, portfolio_risk
from . import position_ownership, trade_stats
from . import regime as market_regime
from .client import Trading212Client, Trading212Error
from .config import Config
from .data import fetch_fx_rate, fetch_history, fetch_intraday, to_t212_ticker, to_yahoo_symbol
from .indicators import atr as compute_atr
from .risk import RiskManager
from .sectors import sector_of
from .strategy import NewsSentiment, Signal, SignalResult, Strategy

log = logging.getLogger(__name__)

# Most symbols the news cycle runs an event study for per cycle -- each one
# is a 10-year price pull plus (usually) an EDGAR request.
EVENT_STUDY_MAX_SYMBOLS = 8


def _entry_context(cfg: Config, watch_symbols: list[str], today: datetime.date):
    """Shared by all three cycles: the market regime (t212bot/regime.py)
    and the earnings calendar (t212bot/events.py), both of which gate new
    entries only -- exits never consult them."""
    reg = market_regime.fetch_regime(cfg.regime_filter_enabled, cfg.regime_sma_days,
                                     cfg.regime_vix_scale_above, cfg.regime_vix_block_above)
    log.info("Regime: %s", reg.describe())
    cal = events.load_calendar()
    if cfg.earnings_blackout_days > 0:
        events.refresh_calendar(cal, watch_symbols, today)
        events.save_calendar(cal)
        soon = events.upcoming_reports(cal, today, cfg.earnings_blackout_days)
        if soon:
            log.info("Earnings within %d day(s): %s", cfg.earnings_blackout_days,
                     ", ".join(f"{sym} {d}" for d, sym in soon))
    return reg, cal


def _context_decisions(cfg: Config, reg, regime_symbols: list, earnings_symbols: list) -> list:
    out = []
    if regime_symbols:
        out.append(history.decision(None, "warning", "Skipped",
                                    f"{', '.join(regime_symbols)} — market regime hostile ({reg.reason}), "
                                    "no new entries."))
    if earnings_symbols:
        out.append(history.decision(None, "warning", "Skipped",
                                    f"{', '.join(earnings_symbols)} — earnings report within "
                                    f"{cfg.earnings_blackout_days} day(s), not opening into it."))
    return out

# Day-trading safety knobs.
MARKET_CLOSE_TIME = datetime.time(16, 0)   # exchange-local (e.g. US equities, ET)
EOD_FLATTEN_MINUTES_BEFORE_CLOSE = 15      # force-close any open position this close to the bell
NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE = 30   # don't open a fresh day-trade this close to the bell
STALE_BAR_MINUTES = 20                     # if the latest bar is older than this, treat as market-closed/no-data
# UTC wall-clock start of the day-trade cron window (see .github/workflows/
# trading-bot-daytrade.yml's schedule: '*/5 13-20 * * 1-5') -- deliberately a
# fixed UTC clock time, not derived from any bar's exchange-local timestamp,
# since it anchors DAYTRADE_ENTRY_DELAY_MINUTES below to "minutes since the
# bot started this cycle's window," not to the real NYSE opening bell (which
# the bot doesn't otherwise track directly -- see STALE_BAR_MINUTES for how
# it infers whether the market is actually open).
DAYTRADE_WINDOW_START_UTC = datetime.time(13, 0)


def _sector_exposure(positions: dict) -> dict:
    """Current $ value held per sector, from a {t212_ticker: position} snapshot."""
    exposure = {}
    for ticker, pos in positions.items():
        sector = sector_of(to_yahoo_symbol(ticker))
        value = float(pos.get("quantity", 0)) * float(pos.get("currentPrice", 0))
        exposure[sector] = exposure.get(sector, 0.0) + value
    return exposure


def _convert_positions_fx(positions: dict, fx_rate: float) -> dict:
    """Convert each position's price fields from instrument currency to account currency.

    Trading212 prices each instrument in its own trading currency (US equities
    in USD) while account_cash() reports in the account's own currency —
    sizing and P&L math need both sides in the same currency. Assumes every
    held instrument shares one currency (true today: the watchlist is US-only,
    see YAHOO_TO_T212 in data.py); a non-US instrument would need a per-symbol
    rate instead of this single blanket one.
    """
    if fx_rate == 1.0:
        return positions
    converted = {}
    for ticker, pos in positions.items():
        pos = dict(pos)
        for key in ("averagePrice", "currentPrice"):
            if pos.get(key) is not None:
                pos[key] = float(pos[key]) * fx_rate
        converted[ticker] = pos
    return converted


def _convert_prices_fx(prices: dict, fx_rate: float) -> dict:
    """Convert a {symbol: OHLCV DataFrame} fetch (USD, from Yahoo Finance) to
    the account currency -- same rationale as _convert_positions_fx above."""
    if fx_rate == 1.0:
        return prices
    converted = {}
    for sym, df in prices.items():
        df = df.copy()
        for col in ("Open", "High", "Low", "Close"):
            if col in df.columns:
                df[col] = df[col] * fx_rate
        converted[sym] = df
    return converted


def run_cycle(cfg: Config, strategy: Strategy) -> None:
    try:
        client = Trading212Client(cfg.api_key, cfg.api_secret, cfg.base_url)
        risk = RiskManager(cfg.max_position_pct, cfg.max_open_positions, cfg.cash_buffer_pct,
                            cfg.risk_per_trade_pct, cfg.atr_multiple, cfg.max_sector_exposure_pct)

        cash = client.account_cash()
        free_cash = float(cash.get("free", 0))
        account_value = float(cash.get("total", free_cash))
        account_currency = client.account_info().get("currencyCode", "USD")
        fx_rate = fetch_fx_rate(account_currency)
        positions = _convert_positions_fx({p["ticker"]: p for p in client.portfolio()}, fx_rate)
        held_symbols = {to_yahoo_symbol(t) for t in positions}
        sector_exposure = _sector_exposure(positions)
        log.info("Account snapshot retrieved (%d open position%s)",
                 len(positions), "" if len(positions) == 1 else "s")

        daily_state = daily_target.load()
        blocked = daily_target.entries_blocked(daily_state)
        pr_state = portfolio_risk.load()
        drawdown_blocked = portfolio_risk.entries_blocked(pr_state)
        tstats = trade_stats.load()
        pnl_hist = pnl_history.load()
        pos_owner = position_ownership.load()

        watch_symbols = sorted(set(cfg.watchlist) | held_symbols)
        prices = _convert_prices_fx(fetch_history(watch_symbols), fx_rate)
        signals = strategy.generate_signals(prices)
        today_et = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
        reg, cal = _entry_context(cfg, watch_symbols, today_et)

        decisions = []
        hold_symbols = []
        chased_symbols = []
        blocked_symbols = []
        drawdown_symbols = []
        paused_symbols = []
        regime_symbols = []
        earnings_symbols = []

        for sym, sig in sorted(signals.items()):
            signal = sig.signal
            t212_ticker = to_t212_ticker(sym)
            last_price = float(prices[sym]["Close"].iloc[-1])

            if (signal is Signal.SELL and t212_ticker in positions
                    and position_ownership.owned_by(pos_owner, t212_ticker, "swing")):
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                if not _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL"):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      "Sell order rejected by Trading212 -- position may already be closed."))
                    continue
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "swing", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "swing", sym, pnl_pct)
                position_ownership.clear_owner(pos_owner, t212_ticker)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"Closed {qty:g} shares (~{qty * last_price:,.2f})."))

            elif signal is Signal.BUY and t212_ticker not in positions:
                if blocked:
                    log.debug("BUY %s skipped: daily target/loss-limit already reached", sym)
                    blocked_symbols.append(sym)
                    continue
                if drawdown_blocked:
                    log.debug("BUY %s skipped: account-wide drawdown pause in effect", sym)
                    drawdown_symbols.append(sym)
                    continue
                if trade_stats.is_paused(tstats, "swing", sym):
                    log.info("BUY %s skipped: in a losing-streak cooldown", sym)
                    paused_symbols.append(sym)
                    continue
                if not reg.entries_allowed:
                    regime_symbols.append(sym)
                    continue
                if events.in_earnings_blackout(cal, sym, today_et, cfg.earnings_blackout_days):
                    log.info("BUY %s skipped: earnings within %d day(s)", sym, cfg.earnings_blackout_days)
                    earnings_symbols.append(sym)
                    continue
                if not risk.can_open_position(len(positions)):
                    log.info("BUY %s skipped: max open positions reached", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        "Max open positions reached."))
                    continue
                sector = sector_of(sym)
                sector_value_held = sector_exposure.get(sector, 0.0)
                if risk.sector_capacity_shares(account_value, last_price, sector_value_held) <= 0:
                    log.info("BUY %s skipped: %s sector exposure cap reached", sym, sector)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        f"{sector} sector exposure cap reached."))
                    continue
                atr_value = compute_atr(prices[sym], period=cfg.atr_period).iloc[-1]
                qty = risk.size_buy(account_value, free_cash, last_price, atr_value,
                                     sector_value_held, sig.strength * reg.size_scale)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                if not _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY"):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      "Buy order rejected by Trading212."))
                    continue
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                sector_exposure[sector] = sector_value_held + qty * last_price
                position_ownership.set_owner(pos_owner, t212_ticker, "swing")
                decisions.append(history.decision(sym, "good", "Buy",
                                  f"Bought {qty:g} shares (~{qty * last_price:,.2f}), "
                                  f"signal strength {sig.strength:.0%}."))

            elif sig.reason == "chased":
                log.info("BUY %s skipped: too far past the signal trigger, not chasing", sym)
                chased_symbols.append(sym)

            else:
                log.debug("%s: %s (no action)", sym, signal.value)
                hold_symbols.append(sym)

        if hold_symbols:
            decisions.append(history.decision(None, "neutral", "Hold",
                                                f"{', '.join(hold_symbols)} — no crossover, no action taken."))
        if chased_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(chased_symbols)} — price already too far past the signal "
                              "trigger by the time it was checked, skipped to avoid chasing."))
        if blocked_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(blocked_symbols)} — daily profit target or loss limit "
                              "already reached, no new entries today."))
        if drawdown_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(drawdown_symbols)} — account-wide drawdown pause in effect, "
                              "no new entries until it recovers."))
        if paused_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(paused_symbols)} — in a losing-streak cooldown."))
        decisions += _context_decisions(cfg, reg, regime_symbols, earnings_symbols)

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)
        portfolio_risk.evaluate(pr_state, cfg.max_portfolio_drawdown_pct)
        portfolio_risk.save(pr_state)
        trade_stats.save(tstats)
        pnl_history.save(pnl_hist)
        position_ownership.save(pos_owner)

        history.append("swing", cfg.env, cfg.dry_run,
                        {"value": account_value, "free": free_cash, "positions": len(positions)},
                        decisions)
    except Exception as exc:
        history.append("swing", cfg.env, cfg.dry_run, None, [], error=str(exc))
        raise


def run_day_trade_cycle(cfg: Config, strategy: Strategy) -> None:
    """Intraday cycle: no position is ever held overnight.

    Any position still open as the close approaches is force-flattened
    regardless of what the strategy's own signal says. New entries are
    refused once we're too close to the bell to realistically manage them,
    and stale (pre-market/weekend/lagged) data blocks new entries outright —
    it's still fine for closing an existing position, since that executes at
    the live market price regardless of the reference price we last saw.

    A held position is also force-closed if the latest bar's Low/High
    touches cfg.daytrade_stop_loss_pct/daytrade_take_profit_pct away from
    the actual average fill price (from Trading212, not an estimate) —
    checked every cycle before the strategy's own signal, same as the EOD
    flatten. This is a polling check against each completed bar's range,
    not a resting broker order.

    All three of the above (EOD flatten, stop/take-profit, signal SELL)
    only apply to positions this cycle itself opened — see
    position_ownership.py. The swing bot trades the same Trading212 account
    and (by default) the same watchlist; without this check, this cycle's
    much faster exits would whipsaw a swing position out within minutes of
    the swing bot opening it, on a signal that has nothing to do with why
    swing bought it.
    """
    try:
        client = Trading212Client(cfg.api_key, cfg.api_secret, cfg.base_url)
        risk = RiskManager(cfg.max_position_pct, cfg.max_open_positions, cfg.cash_buffer_pct,
                            cfg.risk_per_trade_pct, cfg.atr_multiple, cfg.max_sector_exposure_pct)

        cash = client.account_cash()
        free_cash = float(cash.get("free", 0))
        account_value = float(cash.get("total", free_cash))
        account_currency = client.account_info().get("currencyCode", "USD")
        fx_rate = fetch_fx_rate(account_currency)
        positions = _convert_positions_fx({p["ticker"]: p for p in client.portfolio()}, fx_rate)
        held_symbols = {to_yahoo_symbol(t) for t in positions}
        sector_exposure = _sector_exposure(positions)
        log.info("Account snapshot retrieved (%d open position%s)",
                 len(positions), "" if len(positions) == 1 else "s")

        daily_state = daily_target.load()
        blocked = daily_target.entries_blocked(daily_state)
        pr_state = portfolio_risk.load()
        drawdown_blocked = portfolio_risk.entries_blocked(pr_state)
        tstats = trade_stats.load()
        pnl_hist = pnl_history.load()
        pos_owner = position_ownership.load()

        watch_symbols = sorted(set(cfg.watchlist) | held_symbols)
        prices = _convert_prices_fx(
            fetch_intraday(watch_symbols, cfg.alpaca_api_key, cfg.alpaca_api_secret,
                           timeframe=f"{cfg.daytrade_bar_minutes}Min"),
            fx_rate,
        )
        # The live day-trade strategy is always the validated 3-way
        # ORB+MeanReversion+GapFillReversal ensemble (see main.py), which
        # builds ORB with confirm_bars=0 itself, so the 1-minute confirmation
        # series (t212bot/strategy.py: OpeningRangeConfluence.confirm_bars)
        # is never needed here.
        signals = strategy.generate_signals(prices, confirm_prices=None)

        # Entry-side-only warm-up gate: analysis/signals above already ran
        # regardless, this just delays new positions for
        # daytrade_entry_delay_minutes after the cron window opens -- see
        # DAYTRADE_WINDOW_START_UTC and Config.daytrade_entry_delay_minutes.
        too_early_for_entries = _too_early_for_entries(
            datetime.datetime.now(datetime.timezone.utc), cfg.daytrade_entry_delay_minutes)
        today_et = datetime.datetime.now(datetime.timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
        reg, cal = _entry_context(cfg, watch_symbols, today_et)

        decisions = []
        stale_symbols = []
        hold_symbols = []
        chased_symbols = []
        unconfirmed_symbols = []
        blocked_symbols = []
        drawdown_symbols = []
        paused_symbols = []
        warmup_symbols = []
        regime_symbols = []
        earnings_symbols = []

        for sym, df in sorted(prices.items()):
            t212_ticker = to_t212_ticker(sym)
            last_bar_time = df.index[-1]
            last_price = float(df["Close"].iloc[-1])
            minutes_to_close = _minutes_until_close(last_bar_time)
            bar_age_minutes = (pd.Timestamp.now(tz=df.index.tz) - last_bar_time).total_seconds() / 60
            holding = t212_ticker in positions
            daytrade_owned = holding and position_ownership.owned_by(pos_owner, t212_ticker, "daytrade")

            if daytrade_owned and minutes_to_close <= EOD_FLATTEN_MINUTES_BEFORE_CLOSE:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                if not _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL (EOD flatten)"):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      "EOD flatten order rejected by Trading212 -- position may already be closed."))
                    continue
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "daytrade", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "daytrade", sym, pnl_pct)
                position_ownership.clear_owner(pos_owner, t212_ticker)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"EOD flatten — closed {qty:g} shares."))
                continue

            if daytrade_owned and (cfg.daytrade_stop_loss_pct or cfg.daytrade_take_profit_pct):
                avg_price = float(positions[t212_ticker]["averagePrice"])
                stop_price = avg_price * (1 - cfg.daytrade_stop_loss_pct) if cfg.daytrade_stop_loss_pct else None
                target_price = avg_price * (1 + cfg.daytrade_take_profit_pct) if cfg.daytrade_take_profit_pct else None
                bar_low, bar_high = float(df["Low"].iloc[-1]), float(df["High"].iloc[-1])
                hit_stop = stop_price is not None and bar_low <= stop_price
                hit_target = target_price is not None and bar_high >= target_price
                # Checked against the just-completed bar's Low/High, same
                # granularity the backtest validated this against -- not
                # placed as a resting broker order, so a move that touches
                # and reverses within one bar is still caught (the bar's
                # range covers it) but only detected on the next cycle, not
                # instantly. If both are touched in the same bar, the stop
                # takes priority (can't know which happened first intrabar).
                if hit_stop or hit_target:
                    qty = float(positions[t212_ticker]["quantity"])
                    reason = "stop-loss" if hit_stop else "take-profit"
                    if not _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, f"SELL ({reason})"):
                        decisions.append(history.decision(sym, "warning", "Skipped",
                                          f"{reason} order rejected by Trading212 -- position may already be closed."))
                        continue
                    pnl_pct = (last_price - avg_price) * qty / account_value
                    daily_target.record_trade_pct(daily_state, pnl_pct)
                    portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                    trade_stats.record_trade(tstats, "daytrade", sym, pnl_pct,
                                              cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                    pnl_history.record_trade(pnl_hist, "daytrade", sym, pnl_pct)
                    position_ownership.clear_owner(pos_owner, t212_ticker)
                    decisions.append(history.decision(sym, "serious", "Sell",
                                                       f"Closed {qty:g} shares ({reason} hit)."))
                    continue

            if bar_age_minutes > STALE_BAR_MINUTES:
                log.info("%s: latest bar is %.0f min old, market likely closed, skipping new entries",
                          sym, bar_age_minutes)
                stale_symbols.append(sym)
                continue

            sig = signals.get(sym, SignalResult(Signal.HOLD))
            signal = sig.signal

            if signal is Signal.SELL and daytrade_owned:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                if not _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL"):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      "Sell order rejected by Trading212 -- position may already be closed."))
                    continue
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "daytrade", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "daytrade", sym, pnl_pct)
                position_ownership.clear_owner(pos_owner, t212_ticker)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"Closed {qty:g} shares (~{qty * last_price:,.2f})."))

            elif signal is Signal.BUY and not holding:
                if too_early_for_entries:
                    log.debug("BUY %s skipped: still within the post-open warm-up window", sym)
                    warmup_symbols.append(sym)
                    continue
                if blocked:
                    log.debug("BUY %s skipped: daily target/loss-limit already reached", sym)
                    blocked_symbols.append(sym)
                    continue
                if drawdown_blocked:
                    log.debug("BUY %s skipped: account-wide drawdown pause in effect", sym)
                    drawdown_symbols.append(sym)
                    continue
                if trade_stats.is_paused(tstats, "daytrade", sym):
                    log.info("BUY %s skipped: in a losing-streak cooldown", sym)
                    paused_symbols.append(sym)
                    continue
                if not reg.entries_allowed:
                    regime_symbols.append(sym)
                    continue
                if events.in_earnings_blackout(cal, sym, today_et, cfg.earnings_blackout_days):
                    log.info("BUY %s skipped: earnings within %d day(s)", sym, cfg.earnings_blackout_days)
                    earnings_symbols.append(sym)
                    continue
                if minutes_to_close <= NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE:
                    log.info("BUY %s skipped: too close to the close for a new day-trade entry", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        "Too close to the close for a new entry."))
                    continue
                if not risk.can_open_position(len(positions)):
                    log.info("BUY %s skipped: max open positions reached", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        "Max open positions reached."))
                    continue
                sector = sector_of(sym)
                sector_value_held = sector_exposure.get(sector, 0.0)
                if risk.sector_capacity_shares(account_value, last_price, sector_value_held) <= 0:
                    log.info("BUY %s skipped: %s sector exposure cap reached", sym, sector)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        f"{sector} sector exposure cap reached."))
                    continue
                atr_value = compute_atr(df, period=cfg.atr_period).iloc[-1]
                qty = risk.size_buy(account_value, free_cash, last_price, atr_value,
                                     sector_value_held, sig.strength * reg.size_scale)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                if not _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY"):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      "Buy order rejected by Trading212."))
                    continue
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                sector_exposure[sector] = sector_value_held + qty * last_price
                position_ownership.set_owner(pos_owner, t212_ticker, "daytrade")
                decisions.append(history.decision(sym, "good", "Buy",
                                  f"Bought {qty:g} shares (~{qty * last_price:,.2f}), "
                                  f"signal strength {sig.strength:.0%}."))

            elif sig.reason == "chased":
                log.info("BUY %s skipped: too far past the signal trigger, not chasing", sym)
                chased_symbols.append(sym)

            elif sig.reason == "unconfirmed":
                log.info("BUY %s skipped: breakout didn't hold on the finer confirmation bars", sym)
                unconfirmed_symbols.append(sym)

            else:
                log.debug("%s: %s (no action)", sym, signal.value)
                hold_symbols.append(sym)

        if stale_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(stale_symbols)} — latest bar too old, market likely closed."))
        if warmup_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(warmup_symbols)} — still within the "
                              f"{cfg.daytrade_entry_delay_minutes}-minute post-open warm-up window, "
                              "no new entries yet."))
        if hold_symbols:
            decisions.append(history.decision(None, "neutral", "Hold",
                              f"{', '.join(hold_symbols)} — no breakout, no action taken."))
        if chased_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(chased_symbols)} — price already too far past the signal "
                              "trigger by the time it was checked, skipped to avoid chasing."))
        if unconfirmed_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(unconfirmed_symbols)} — breakout didn't hold across the "
                              "confirmation window on finer bars, skipped."))
        if blocked_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(blocked_symbols)} — daily profit target or loss limit "
                              "already reached, no new entries today."))
        if drawdown_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(drawdown_symbols)} — account-wide drawdown pause in effect, "
                              "no new entries until it recovers."))
        if paused_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(paused_symbols)} — in a losing-streak cooldown."))
        decisions += _context_decisions(cfg, reg, regime_symbols, earnings_symbols)

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)
        portfolio_risk.evaluate(pr_state, cfg.max_portfolio_drawdown_pct)
        portfolio_risk.save(pr_state)
        trade_stats.save(tstats)
        pnl_history.save(pnl_hist)
        position_ownership.save(pos_owner)

        history.append("daytrade", cfg.env, cfg.dry_run,
                        {"value": account_value, "free": free_cash, "positions": len(positions)},
                        decisions)
    except Exception as exc:
        history.append("daytrade", cfg.env, cfg.dry_run, None, [], error=str(exc))
        raise


def run_news_cycle(cfg: Config, strategy: NewsSentiment) -> None:
    """News-driven cycle: daily bars (yfinance) + free headline scores
    (t212bot/news.py) -> NewsSentiment signals -> the same risk gates and
    bookkeeping as the other two cycles, under its own owner tag "news".

    Exits, checked in this order for every position this cycle opened and
    before the strategy's own signal is consulted:
    1. stop-loss / take-profit against the latest daily bar's Low/High and
       the real average fill (cfg.news_stop_loss_pct / news_take_profit_pct)
       -- a polling check per cycle, not a resting broker order;
    2. max hold: cfg.news_max_hold_days calendar days since entry (the
       post-news drift this trades decays within days; past that it's just
       an untended position);
    3. signal SELL: the headlines turned clearly negative.
    Positions owned by the swing/day-trade bots, or untracked ones, are
    never touched (see position_ownership.py).

    New entries need a daily bar dated today (US/Eastern) -- on a holiday
    or before the open yfinance's last bar is yesterday's and the "today's
    move" the strategy confirms against would be stale -- and a symbol
    closed by this cycle earlier today isn't re-bought the same day.

    Every cycle also archives the day's per-symbol scores to
    state/news.json (scores/counts/dates only -- public repo), which is the
    history `backtest --news` replays: RSS feeds don't keep any.
    """
    try:
        client = Trading212Client(cfg.api_key, cfg.api_secret, cfg.base_url)
        risk = RiskManager(cfg.max_position_pct, cfg.max_open_positions, cfg.cash_buffer_pct,
                            cfg.risk_per_trade_pct, cfg.atr_multiple, cfg.max_sector_exposure_pct)

        cash = client.account_cash()
        free_cash = float(cash.get("free", 0))
        account_value = float(cash.get("total", free_cash))
        account_currency = client.account_info().get("currencyCode", "USD")
        fx_rate = fetch_fx_rate(account_currency)
        positions = _convert_positions_fx({p["ticker"]: p for p in client.portfolio()}, fx_rate)
        held_symbols = {to_yahoo_symbol(t) for t in positions}
        sector_exposure = _sector_exposure(positions)
        log.info("Account snapshot retrieved (%d open position%s)",
                 len(positions), "" if len(positions) == 1 else "s")

        daily_state = daily_target.load()
        blocked = daily_target.entries_blocked(daily_state)
        pr_state = portfolio_risk.load()
        drawdown_blocked = portfolio_risk.entries_blocked(pr_state)
        tstats = trade_stats.load()
        pnl_hist = pnl_history.load()
        pos_owner = position_ownership.load()
        news_state = news.load_state()

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        today_et = now_utc.astimezone(ZoneInfo("America/New_York")).date()
        today = today_et.isoformat()
        # Housekeeping: same-day re-entry blocks expire overnight; entry-date
        # records for positions no longer held (closed by hand, or by a
        # rejected-then-retried order) go too, along with their owner tag.
        news_state["closed"] = {t: d for t, d in news_state.get("closed", {}).items() if d == today}
        for ticker in list(news_state.get("positions", {})):
            if ticker not in positions:
                del news_state["positions"][ticker]
                if pos_owner.get(ticker) == "news":
                    position_ownership.clear_owner(pos_owner, ticker)

        # Universe: a fixed watchlist, or whatever is in the news today
        # (t212bot/discovery.py), or both. Held positions always included.
        names = companies.names_for(cfg.news_company_names)
        discovered: dict[str, list] = {}
        discovered_names: dict[str, str] = {}
        if cfg.news_universe in ("discover", "both"):
            market = discovery.fetch_market_headlines(discovery.feed_list(cfg.news_feeds), now=now_utc,
                                                      max_age_hours=cfg.news_max_age_hours)
            resolver = discovery.TickerResolver.build(names, events.load_sec_entries(), cfg.news_sec_top_n)
            names = resolver.names
            discovered = discovery.discover(market, resolver, cfg.news_min_mentions, cfg.news_max_candidates)
            tradable = discovery.tradable_tickers(client)
            if tradable is not None:
                untradable = [s for s in discovered if to_t212_ticker(s) not in tradable]
                if untradable:
                    log.info("Discovery: not tradable on Trading212, dropped: %s", ", ".join(untradable))
                    discovered = {s: h for s, h in discovered.items() if s not in untradable}
            discovered_names = {s: (names.get(s) or [s])[0] for s in discovered}
        base_symbols = set(cfg.watchlist) if cfg.news_universe in ("watchlist", "both") else set()
        watch_symbols = sorted(base_symbols | held_symbols | set(discovered))
        prices = _convert_prices_fx(fetch_history(watch_symbols, period="6mo"), fx_rate)
        headlines = news.fetch_headlines(watch_symbols, max_age_hours=cfg.news_max_age_hours, now=now_utc,
                                         company_names=names, require_relevance=cfg.news_relevance_filter)
        for sym, extra in discovered.items():
            have = {news._normalize_title(h.title) for h in headlines.get(sym, [])}
            headlines.setdefault(sym, []).extend(h for h in extra if news._normalize_title(h.title) not in have)
        universe = discovery.load_universe()
        discovery.refresh_universe(universe, {s: discovered_names.get(s, s) for s in watch_symbols}, today_et,
                                   cfg.news_min_market_cap, cfg.news_min_dollar_volume, exempt=set(cfg.watchlist))
        scorer = news_llm.make_scorer(cfg.news_scorer, cfg.news_llm_model)
        scores = news.aggregate_all(headlines, now=now_utc, half_life_hours=cfg.news_half_life_hours,
                                    scorer=scorer)
        scores = news.peer_scores(scores, sector_of, cfg.news_min_headlines, cfg.news_peer_discount)
        news.record_scores(news_state, today, scores)
        score_hist = news.score_history(news_state)
        strategy.set_news(scores, history=score_hist)
        signals = strategy.generate_signals(prices)
        reg, cal = _entry_context(cfg, watch_symbols, today_et)

        n_headlines = sum(len(h) for h in headlines.values())
        n_informative = sum(1 for s in scores.values() if s.n_scored >= cfg.news_min_headlines)
        log.info("News (%s scorer): %d headlines across %d symbols, %d symbol%s with >= %d scored headlines",
                 "llm" if scorer else "lexicon", n_headlines, len(scores), n_informative,
                 "" if n_informative == 1 else "s", cfg.news_min_headlines)
        for sym in sorted(scores):
            ns = scores[sym]
            if ns.n_scored >= cfg.news_min_headlines:
                log.info("News %s: %+.2f over %d scored headlines (%d%% positive)%s",
                         sym, ns.score, ns.n_scored, round(ns.pos_frac * 100),
                         "" if ns.source == "own" else f" via {ns.source}")

        # Event study for every symbol with a signal (and anything we hold):
        # how did similar news move this stock before? Informational unless
        # cfg.event_study_gate -- see t212bot/events.py.
        studies: dict = {}
        if cfg.event_study_enabled:
            study_symbols = [s for s, sg in sorted(signals.items()) if sg.signal is not Signal.HOLD]
            study_symbols += [s for s in sorted(held_symbols)
                              if position_ownership.owned_by(pos_owner, to_t212_ticker(s), "news")
                              and s not in study_symbols]
            study_symbols = study_symbols[:EVENT_STUDY_MAX_SYMBOLS]
            if study_symbols:
                long_prices = fetch_history(study_symbols + [market_regime.SPY_SYMBOL], period="10y")
                spy_long = long_prices.get(market_regime.SPY_SYMBOL)
                for sym in study_symbols:
                    if sym not in long_prices:
                        continue
                    try:
                        studies[sym] = events.run_event_study(
                            sym, headlines.get(sym, []), long_prices[sym], spy_long, cal, score_hist,
                            cfg.news_buy_threshold, cfg.news_min_headlines, cfg.event_study_min_samples,
                            use_edgar=cfg.event_study_use_edgar)
                    except Exception as exc:  # noqa: BLE001 - never let the study break the cycle
                        log.warning("Event study failed for %s: %s", sym, exc)
                        continue
                    kind, summary = studies[sym].verdict(cfg.event_study_horizon_days, cfg.event_study_min_samples)
                    log.info("History %s (%s): %s -> %s", sym,
                             "+".join(studies[sym].event_types) or "uncategorised", kind, summary)

        decisions = []
        buckets = {key: [] for key in ("no_bar", "hold", "negative", "chased", "unconfirmed", "stale_news",
                                       "downtrend", "blocked", "drawdown", "paused", "reentry",
                                       "history_adverse", "illiquid")}
        regime_symbols = []
        earnings_symbols = []

        for sym, df in sorted(prices.items()):
            t212_ticker = to_t212_ticker(sym)
            last_price = float(df["Close"].iloc[-1])
            bar_date = pd.Timestamp(df.index[-1]).date()
            holding = t212_ticker in positions
            news_owned = holding and position_ownership.owned_by(pos_owner, t212_ticker, "news")
            sig = signals.get(sym, SignalResult(Signal.HOLD))
            ns = scores.get(sym)
            headline_note = f" Top headline: \"{ns.top_headline}\"" if ns and ns.top_headline else ""
            if ns and ns.source != "own":
                headline_note += f" (sector read-through via {ns.source[6:]})"
            study = studies.get(sym)
            history_note = ""
            history_kind = None
            if study is not None:
                history_kind, history_summary = study.verdict(cfg.event_study_horizon_days,
                                                              cfg.event_study_min_samples)
                history_note = f" History ({'+'.join(study.event_types) or 'uncategorised'}): {history_summary}."
                decisions.append(history.decision(sym, "neutral", "History",
                                                  f"{'+'.join(study.event_types) or 'uncategorised'} — "
                                                  f"{history_kind}: {history_summary}."
                                                  + (" " + " | ".join(study.lines(cfg.event_study_horizon_days))
                                                     if len(study.sources) > 1 else "")))

            def _close(label: str, detail: str) -> bool:
                """Sell the whole news-owned position and do the bookkeeping;
                False if Trading212 rejected the order."""
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                if not _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, label):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      f"{label} order rejected by Trading212 -- position may already be closed."))
                    return False
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "news", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "news", sym, pnl_pct)
                position_ownership.clear_owner(pos_owner, t212_ticker)
                news_state["positions"].pop(t212_ticker, None)
                news_state["closed"][t212_ticker] = today
                decisions.append(history.decision(sym, "serious", "Sell", f"Closed {qty:g} shares {detail}"))
                return True

            if news_owned:
                avg_price = float(positions[t212_ticker]["averagePrice"])
                stop_price = avg_price * (1 - cfg.news_stop_loss_pct) if cfg.news_stop_loss_pct else None
                target_price = avg_price * (1 + cfg.news_take_profit_pct) if cfg.news_take_profit_pct else None
                bar_low, bar_high = float(df["Low"].iloc[-1]), float(df["High"].iloc[-1])
                hit_stop = stop_price is not None and bar_low <= stop_price
                hit_target = target_price is not None and bar_high >= target_price
                if hit_stop or hit_target:
                    reason = "stop-loss" if hit_stop else "take-profit"
                    _close(f"SELL ({reason})", f"({reason} hit).")
                    continue
                entered = news_state["positions"].get(t212_ticker, {}).get("entered")
                held_days = (today_et - datetime.date.fromisoformat(entered)).days if entered else None
                if held_days is not None and held_days >= cfg.news_max_hold_days:
                    _close("SELL (max hold)", f"(held {held_days} days, max {cfg.news_max_hold_days}).")
                    continue
                if sig.signal is Signal.SELL:
                    _close("SELL", f"(news turned negative, {sig.reason}).{headline_note}")
                    continue

            if sig.signal is Signal.BUY and not holding:
                if bar_date != today_et:
                    log.info("BUY %s skipped: latest daily bar is %s, no bar for today yet", sym, bar_date)
                    buckets["no_bar"].append(sym)
                    continue
                if blocked:
                    buckets["blocked"].append(sym)
                    continue
                if drawdown_blocked:
                    buckets["drawdown"].append(sym)
                    continue
                if trade_stats.is_paused(tstats, "news", sym):
                    log.info("BUY %s skipped: in a losing-streak cooldown", sym)
                    buckets["paused"].append(sym)
                    continue
                if news_state["closed"].get(t212_ticker) == today:
                    log.info("BUY %s skipped: already closed by the news bot today", sym)
                    buckets["reentry"].append(sym)
                    continue
                if not reg.entries_allowed:
                    regime_symbols.append(sym)
                    continue
                # Report day itself is allowed: the report *is* the news.
                if events.in_earnings_blackout(cal, sym, today_et, cfg.earnings_blackout_days,
                                               allow_report_day=True):
                    log.info("BUY %s skipped: earnings within %d day(s)", sym, cfg.earnings_blackout_days)
                    earnings_symbols.append(sym)
                    continue
                if cfg.event_study_gate and history_kind == "adverse":
                    log.info("BUY %s skipped: this stock's history after similar news is adverse", sym)
                    buckets["history_adverse"].append(sym)
                    continue
                if sym not in cfg.watchlist and not discovery.is_liquid(universe, sym):
                    log.info("BUY %s skipped: discovered name failed the liquidity/eligibility check (%s)",
                             sym, (universe.get(sym) or {}).get("reason", "unchecked"))
                    buckets["illiquid"].append(sym)
                    continue
                if not risk.can_open_position(len(positions)):
                    log.info("BUY %s skipped: max open positions reached", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Max open positions reached."))
                    continue
                sector = sector_of(sym)
                sector_value_held = sector_exposure.get(sector, 0.0)
                if risk.sector_capacity_shares(account_value, last_price, sector_value_held) <= 0:
                    log.info("BUY %s skipped: %s sector exposure cap reached", sym, sector)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        f"{sector} sector exposure cap reached."))
                    continue
                atr_value = compute_atr(df, period=cfg.atr_period).iloc[-1]
                qty = risk.size_buy(account_value, free_cash, last_price, atr_value,
                                     sector_value_held, sig.strength * reg.size_scale)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                if not _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY"):
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                      "Buy order rejected by Trading212."))
                    continue
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                sector_exposure[sector] = sector_value_held + qty * last_price
                position_ownership.set_owner(pos_owner, t212_ticker, "news")
                news_state["positions"][t212_ticker] = {"entered": today}
                decisions.append(history.decision(sym, "good", "Buy",
                                  f"Bought {qty:g} shares (~{qty * last_price:,.2f}), "
                                  f"signal strength {sig.strength:.0%}, {sig.reason}.{headline_note}{history_note}"))

            elif sig.reason in buckets and sig.signal is Signal.HOLD:
                log.info("BUY %s skipped: %s", sym, sig.reason)
                buckets[sig.reason].append(sym)

            elif sig.signal is Signal.SELL:
                # Clearly negative news on a symbol this bot doesn't hold:
                # long-only, so nothing to do -- but worth logging apart from
                # a plain "no signal" so the dashboard shows the read.
                buckets["negative"].append(sym)

            else:
                log.debug("%s: %s (no action)", sym, sig.signal.value)
                buckets["hold"].append(sym)

        summaries = {
            "no_bar": ("warning", "Skipped", "no daily bar for today yet (market closed or holiday), no new entries."),
            "hold": ("neutral", "Hold", "no clear news signal, no action taken."),
            "negative": ("neutral", "Hold", "clearly negative news but no news-bot position to sell (long-only), no action."),
            "chased": ("warning", "Skipped", "positive news but today's move already past the chase limit, skipped."),
            "unconfirmed": ("warning", "Skipped", "positive news but the price isn't confirming it (down on the day), skipped."),
            "stale_news": ("warning", "Skipped", "positive news but it was already positive yesterday -- not a fresh event, skipped."),
            "downtrend": ("warning", "Skipped", f"positive news but price is below its {cfg.news_trend_filter_days}-day SMA, skipped."),
            "blocked": ("warning", "Skipped", "daily profit target or loss limit already reached, no new entries today."),
            "drawdown": ("warning", "Skipped", "account-wide drawdown pause in effect, no new entries until it recovers."),
            "paused": ("warning", "Skipped", "in a losing-streak cooldown."),
            "reentry": ("warning", "Skipped", "already closed by the news bot today, not re-entering the same day."),
            "history_adverse": ("warning", "Skipped", "this stock's own history after similar news is adverse "
                                                       "(EVENT_STUDY_GATE), skipped."),
            "illiquid": ("warning", "Skipped", "discovered in the news but too small/illiquid (or no data) to "
                                               "trade, skipped."),
        }
        for key, syms in buckets.items():
            if syms:
                status, headline, text = summaries[key]
                decisions.append(history.decision(None, status, headline, f"{', '.join(syms)} — {text}"))
        decisions += _context_decisions(cfg, reg, regime_symbols, earnings_symbols)

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)
        portfolio_risk.evaluate(pr_state, cfg.max_portfolio_drawdown_pct)
        portfolio_risk.save(pr_state)
        trade_stats.save(tstats)
        pnl_history.save(pnl_hist)
        position_ownership.save(pos_owner)
        news.save_state(news_state)
        discovery.save_universe(universe)

        history.append("news", cfg.env, cfg.dry_run,
                        {"value": account_value, "free": free_cash, "positions": len(positions)},
                        decisions)
    except Exception as exc:
        history.append("news", cfg.env, cfg.dry_run, None, [], error=str(exc))
        raise


def _minutes_until_close(bar_timestamp: pd.Timestamp) -> float:
    close_dt = bar_timestamp.replace(
        hour=MARKET_CLOSE_TIME.hour, minute=MARKET_CLOSE_TIME.minute, second=0, microsecond=0
    )
    return (close_dt - bar_timestamp).total_seconds() / 60


def _too_early_for_entries(now_utc: datetime.datetime, delay_minutes: int) -> bool:
    """True if it's still within delay_minutes of the cron window's start
    (DAYTRADE_WINDOW_START_UTC), i.e. too early to open a new position.
    delay_minutes <= 0 always returns False (gate disabled)."""
    if delay_minutes <= 0:
        return False
    window_start = now_utc.replace(hour=DAYTRADE_WINDOW_START_UTC.hour,
                                    minute=DAYTRADE_WINDOW_START_UTC.minute,
                                    second=0, microsecond=0)
    entries_open_at = window_start + datetime.timedelta(minutes=delay_minutes)
    return now_utc < entries_open_at


def _execute(client: Trading212Client, dry_run: bool, ticker: str,
             quantity: float, ref_price: float, label: str) -> bool:
    """Places the order; returns False (instead of raising) if Trading212
    rejects it, e.g. a stale local position snapshot tries to sell shares
    already closed out from under the bot. One rejected order must not abort
    the rest of the cycle -- the caller skips that symbol and moves on.
    """
    # Deliberately identical log output whether this was a dry run or a real
    # order, with no quantity/notional/order-id — this stdout is captured by
    # a public CI log, and even "did a real order get placed" is information
    # worth not leaking there. Full detail (figures, dry_run flag, order id)
    # goes only to the local, gitignored history.jsonl via the call site.
    if not dry_run:
        try:
            client.place_market_order(ticker, quantity)
        except Trading212Error as exc:
            log.warning("Order rejected: %s %s (%s)", label, ticker, exc)
            return False
    log.info("Signal: %s %s", label, ticker)
    return True
