"""One trading cycle: fetch data -> generate signals -> apply risk -> execute.

Stateless by design: run it on a schedule (cron / GitHub Actions). The current
portfolio is read from Trading212 each cycle, so restarts are safe.
"""

import logging

from .client import Trading212Client
from .config import Config
from .data import fetch_history, to_t212_ticker, to_yahoo_symbol
from .risk import RiskManager
from .strategy import Signal, Strategy

log = logging.getLogger(__name__)


def run_cycle(cfg: Config, strategy: Strategy) -> None:
    client = Trading212Client(cfg.api_key, cfg.base_url)
    risk = RiskManager(cfg.max_position_pct, cfg.max_open_positions, cfg.cash_buffer_pct)

    cash = client.account_cash()
    free_cash = float(cash.get("free", 0))
    account_value = float(cash.get("total", free_cash))
    positions = {p["ticker"]: p for p in client.portfolio()}
    held_symbols = {to_yahoo_symbol(t) for t in positions}
    log.info("Account value %.2f, free cash %.2f, %d open positions (%s mode%s)",
             account_value, free_cash, len(positions), cfg.env,
             ", DRY RUN" if cfg.dry_run else "")

    prices = fetch_history(sorted(set(cfg.watchlist) | held_symbols))
    signals = strategy.generate_signals(prices)

    for sym, signal in sorted(signals.items()):
        t212_ticker = to_t212_ticker(sym)
        last_price = float(prices[sym]["Close"].iloc[-1])

        if signal is Signal.SELL and t212_ticker in positions:
            qty = float(positions[t212_ticker]["quantity"])
            _execute(client, cfg.dry_run, t212_ticker, -qty, last_price, "SELL")

        elif signal is Signal.BUY and t212_ticker not in positions:
            if not risk.can_open_position(len(positions)):
                log.info("BUY %s skipped: max open positions reached", sym)
                continue
            qty = risk.size_buy(account_value, free_cash, last_price)
            if qty <= 0:
                log.info("BUY %s skipped: insufficient budget", sym)
                continue
            _execute(client, cfg.dry_run, t212_ticker, qty, last_price, "BUY")
            free_cash -= qty * last_price
            positions[t212_ticker] = {"quantity": qty}

        else:
            log.debug("%s: %s (no action)", sym, signal.value)


def _execute(client: Trading212Client, dry_run: bool, ticker: str,
             quantity: float, ref_price: float, label: str) -> None:
    notional = abs(quantity) * ref_price
    if dry_run:
        log.info("[DRY RUN] %s %s x %.2f (~%.2f)", label, ticker, abs(quantity), notional)
        return
    result = client.place_market_order(ticker, quantity)
    log.info("%s %s x %.2f (~%.2f) placed: order id %s",
             label, ticker, abs(quantity), notional, result.get("id"))
