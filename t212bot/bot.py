"""One trading cycle: fetch data -> generate signals -> apply risk -> execute.

Stateless by design: run it on a schedule (cron / GitHub Actions). The current
portfolio is read from Trading212 each cycle, so restarts are safe. Every
cycle — success or failure — is logged to logs/history.jsonl (see history.py),
which is what feeds the published dashboard.
"""

import datetime
import logging

import pandas as pd

from . import daily_target, history, pnl_history, portfolio_risk, trade_stats
from .client import Trading212Client
from .config import Config
from .data import fetch_fx_rate, fetch_history, fetch_intraday, to_t212_ticker, to_yahoo_symbol
from .indicators import atr as compute_atr
from .risk import RiskManager
from .sectors import sector_of
from .strategy import Signal, SignalResult, Strategy

log = logging.getLogger(__name__)

# Day-trading safety knobs.
MARKET_CLOSE_TIME = datetime.time(16, 0)   # exchange-local (e.g. US equities, ET)
EOD_FLATTEN_MINUTES_BEFORE_CLOSE = 15      # force-close any open position this close to the bell
NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE = 30   # don't open a fresh day-trade this close to the bell
STALE_BAR_MINUTES = 20                     # if the latest bar is older than this, treat as market-closed/no-data


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

        prices = _convert_prices_fx(fetch_history(sorted(set(cfg.watchlist) | held_symbols)), fx_rate)
        signals = strategy.generate_signals(prices)

        decisions = []
        hold_symbols = []
        blocked_symbols = []
        drawdown_symbols = []
        paused_symbols = []

        for sym, sig in sorted(signals.items()):
            signal = sig.signal
            t212_ticker = to_t212_ticker(sym)
            last_price = float(prices[sym]["Close"].iloc[-1])

            if signal is Signal.SELL and t212_ticker in positions:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL")
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "swing", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "swing", sym, pnl_pct)
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
                                     sector_value_held, sig.strength)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY")
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                sector_exposure[sector] = sector_value_held + qty * last_price
                decisions.append(history.decision(sym, "good", "Buy",
                                  f"Bought {qty:g} shares (~{qty * last_price:,.2f}), "
                                  f"signal strength {sig.strength:.0%}."))

            else:
                log.debug("%s: %s (no action)", sym, signal.value)
                hold_symbols.append(sym)

        if hold_symbols:
            decisions.append(history.decision(None, "neutral", "Hold",
                                                f"{', '.join(hold_symbols)} — no crossover, no action taken."))
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

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)
        portfolio_risk.evaluate(pr_state, cfg.max_portfolio_drawdown_pct)
        portfolio_risk.save(pr_state)
        trade_stats.save(tstats)
        pnl_history.save(pnl_hist)

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

        prices = _convert_prices_fx(fetch_intraday(sorted(set(cfg.watchlist) | held_symbols)), fx_rate)
        signals = strategy.generate_signals(prices)

        decisions = []
        stale_symbols = []
        hold_symbols = []
        blocked_symbols = []
        drawdown_symbols = []
        paused_symbols = []

        for sym, df in sorted(prices.items()):
            t212_ticker = to_t212_ticker(sym)
            last_bar_time = df.index[-1]
            last_price = float(df["Close"].iloc[-1])
            minutes_to_close = _minutes_until_close(last_bar_time)
            bar_age_minutes = (pd.Timestamp.now(tz=df.index.tz) - last_bar_time).total_seconds() / 60
            holding = t212_ticker in positions

            if holding and minutes_to_close <= EOD_FLATTEN_MINUTES_BEFORE_CLOSE:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL (EOD flatten)")
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "daytrade", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "daytrade", sym, pnl_pct)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"EOD flatten — closed {qty:g} shares."))
                continue

            if bar_age_minutes > STALE_BAR_MINUTES:
                log.info("%s: latest bar is %.0f min old, market likely closed, skipping new entries",
                          sym, bar_age_minutes)
                stale_symbols.append(sym)
                continue

            sig = signals.get(sym, SignalResult(Signal.HOLD))
            signal = sig.signal

            if signal is Signal.SELL and holding:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL")
                pnl_pct = (last_price - avg_price) * qty / account_value
                daily_target.record_trade_pct(daily_state, pnl_pct)
                portfolio_risk.record_trade_pct(pr_state, pnl_pct)
                trade_stats.record_trade(tstats, "daytrade", sym, pnl_pct,
                                          cfg.losing_streak_limit, cfg.losing_streak_cooldown_days)
                pnl_history.record_trade(pnl_hist, "daytrade", sym, pnl_pct)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"Closed {qty:g} shares (~{qty * last_price:,.2f})."))

            elif signal is Signal.BUY and not holding:
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
                                     sector_value_held, sig.strength)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY")
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                sector_exposure[sector] = sector_value_held + qty * last_price
                decisions.append(history.decision(sym, "good", "Buy",
                                  f"Bought {qty:g} shares (~{qty * last_price:,.2f}), "
                                  f"signal strength {sig.strength:.0%}."))

            else:
                log.debug("%s: %s (no action)", sym, signal.value)
                hold_symbols.append(sym)

        if stale_symbols:
            decisions.append(history.decision(None, "warning", "Skipped",
                              f"{', '.join(stale_symbols)} — latest bar too old, market likely closed."))
        if hold_symbols:
            decisions.append(history.decision(None, "neutral", "Hold",
                              f"{', '.join(hold_symbols)} — no breakout, no action taken."))
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

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)
        portfolio_risk.evaluate(pr_state, cfg.max_portfolio_drawdown_pct)
        portfolio_risk.save(pr_state)
        trade_stats.save(tstats)
        pnl_history.save(pnl_hist)

        history.append("daytrade", cfg.env, cfg.dry_run,
                        {"value": account_value, "free": free_cash, "positions": len(positions)},
                        decisions)
    except Exception as exc:
        history.append("daytrade", cfg.env, cfg.dry_run, None, [], error=str(exc))
        raise


def _minutes_until_close(bar_timestamp: pd.Timestamp) -> float:
    close_dt = bar_timestamp.replace(
        hour=MARKET_CLOSE_TIME.hour, minute=MARKET_CLOSE_TIME.minute, second=0, microsecond=0
    )
    return (close_dt - bar_timestamp).total_seconds() / 60


def _execute(client: Trading212Client, dry_run: bool, ticker: str,
             quantity: float, ref_price: float, label: str) -> None:
    # Deliberately identical output whether this was a dry run or a real
    # order, with no quantity/notional/order-id — this stdout is captured by
    # a public CI log, and even "did a real order get placed" is information
    # worth not leaking there. Full detail (figures, dry_run flag, order id)
    # goes only to the local, gitignored history.jsonl via the call site.
    if not dry_run:
        client.place_market_order(ticker, quantity)
    log.info("Signal: %s %s", label, ticker)
