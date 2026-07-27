"""One trading cycle: fetch data -> generate signals -> apply risk -> execute.

Stateless by design: run it on a schedule (cron / GitHub Actions). The current
portfolio is read from Trading212 each cycle, so restarts are safe. Every
cycle — success or failure — is logged to logs/history.jsonl (see history.py),
which is what feeds the published dashboard.
"""

import datetime
import logging

import pandas as pd

from . import daily_target, history
from .client import Trading212Client
from .config import Config
from .data import fetch_history, fetch_intraday, to_t212_ticker, to_yahoo_symbol
from .risk import RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)

# Day-trading safety knobs.
MARKET_CLOSE_TIME = datetime.time(16, 0)   # exchange-local (e.g. US equities, ET)
EOD_FLATTEN_MINUTES_BEFORE_CLOSE = 15      # force-close any open position this close to the bell
NO_NEW_ENTRIES_MINUTES_BEFORE_CLOSE = 30   # don't open a fresh day-trade this close to the bell
STALE_BAR_MINUTES = 20                     # if the latest bar is older than this, treat as market-closed/no-data


def run_cycle(cfg: Config, strategy: Strategy) -> None:
    try:
        client = Trading212Client(cfg.api_key, cfg.api_secret, cfg.base_url)
        risk = RiskManager(cfg.max_position_pct, cfg.max_open_positions, cfg.cash_buffer_pct)

        cash = client.account_cash()
        free_cash = float(cash.get("free", 0))
        account_value = float(cash.get("total", free_cash))
        positions = {p["ticker"]: p for p in client.portfolio()}
        held_symbols = {to_yahoo_symbol(t) for t in positions}
        log.info("Account snapshot retrieved (%d open position%s)",
                 len(positions), "" if len(positions) == 1 else "s")

        daily_state = daily_target.load()
        blocked = daily_target.entries_blocked(daily_state)

        prices = fetch_history(sorted(set(cfg.watchlist) | held_symbols))
        signals = strategy.generate_signals(prices)

        decisions = []
        hold_symbols = []
        blocked_symbols = []

        for sym, signal in sorted(signals.items()):
            t212_ticker = to_t212_ticker(sym)
            last_price = float(prices[sym]["Close"].iloc[-1])

            if signal is Signal.SELL and t212_ticker in positions:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL")
                daily_target.record_trade_pct(daily_state, (last_price - avg_price) * qty / account_value)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"Closed {qty:g} shares (~{qty * last_price:,.2f})."))

            elif signal is Signal.BUY and t212_ticker not in positions:
                if blocked:
                    log.debug("BUY %s skipped: daily target/loss-limit already reached", sym)
                    blocked_symbols.append(sym)
                    continue
                if not risk.can_open_position(len(positions)):
                    log.info("BUY %s skipped: max open positions reached", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped",
                                                        "Max open positions reached."))
                    continue
                qty = risk.size_buy(account_value, free_cash, last_price)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY")
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                decisions.append(history.decision(sym, "good", "Buy",
                                                   f"Bought {qty:g} shares (~{qty * last_price:,.2f})."))

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

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)

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
        risk = RiskManager(cfg.max_position_pct, cfg.max_open_positions, cfg.cash_buffer_pct)

        cash = client.account_cash()
        free_cash = float(cash.get("free", 0))
        account_value = float(cash.get("total", free_cash))
        positions = {p["ticker"]: p for p in client.portfolio()}
        held_symbols = {to_yahoo_symbol(t) for t in positions}
        log.info("Account snapshot retrieved (%d open position%s)",
                 len(positions), "" if len(positions) == 1 else "s")

        daily_state = daily_target.load()
        blocked = daily_target.entries_blocked(daily_state)

        prices = fetch_intraday(sorted(set(cfg.watchlist) | held_symbols))
        signals = strategy.generate_signals(prices)

        decisions = []
        stale_symbols = []
        hold_symbols = []
        blocked_symbols = []

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
                daily_target.record_trade_pct(daily_state, (last_price - avg_price) * qty / account_value)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"EOD flatten — closed {qty:g} shares."))
                continue

            if bar_age_minutes > STALE_BAR_MINUTES:
                log.info("%s: latest bar is %.0f min old, market likely closed, skipping new entries",
                          sym, bar_age_minutes)
                stale_symbols.append(sym)
                continue

            signal = signals.get(sym, Signal.HOLD)

            if signal is Signal.SELL and holding:
                qty = float(positions[t212_ticker]["quantity"])
                avg_price = float(positions[t212_ticker]["averagePrice"])
                _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL")
                daily_target.record_trade_pct(daily_state, (last_price - avg_price) * qty / account_value)
                decisions.append(history.decision(sym, "serious", "Sell",
                                                   f"Closed {qty:g} shares (~{qty * last_price:,.2f})."))

            elif signal is Signal.BUY and not holding:
                if blocked:
                    log.debug("BUY %s skipped: daily target/loss-limit already reached", sym)
                    blocked_symbols.append(sym)
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
                qty = risk.size_buy(account_value, free_cash, last_price)
                if qty <= 0:
                    log.info("BUY %s skipped: insufficient budget", sym)
                    decisions.append(history.decision(sym, "warning", "Skipped", "Insufficient budget."))
                    continue
                _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY")
                free_cash -= qty * last_price
                positions[t212_ticker] = {"quantity": qty}
                decisions.append(history.decision(sym, "good", "Buy",
                                                   f"Bought {qty:g} shares (~{qty * last_price:,.2f})."))

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

        daily_target.evaluate(daily_state, cfg.daily_profit_target_pct, cfg.daily_loss_limit_pct)
        daily_target.save(daily_state)

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
