"""One-off manual order placement — verifies end-to-end connectivity to
Trading212's API (auth, symbol mapping, order execution) outside of either
bot's strategy logic. Not part of a scheduled cycle and not fed into the
dashboard's history; this is a manual, human-triggered smoke test, run via
`python main.py test-order` / the test-order.yml workflow.

Same public-repo redaction discipline as bot.py: this stdout is captured by
a public CI log, so it never prints quantity, price, or notional value —
only whether an order was placed and its order id (not sensitive).
"""

import logging

from .client import Trading212Client
from .config import Config
from .data import to_t212_ticker

log = logging.getLogger(__name__)


def place_test_order(cfg: Config, symbol: str, qty: float) -> None:
    client = Trading212Client(cfg.api_key, cfg.api_secret, cfg.base_url)
    t212_ticker = to_t212_ticker(symbol)

    cash = client.account_cash()
    log.info("Connected OK (env=%s).", cfg.env)

    if cfg.dry_run:
        log.info("DRY RUN — would place a test BUY order for %s, not sending it.", t212_ticker)
        return

    result = client.place_market_order(t212_ticker, qty)
    order_id = result.get("id") if isinstance(result, dict) else None
    log.info("Test order placed for %s (order id: %s). Check Trading212 > Orders to confirm.",
              t212_ticker, order_id)
