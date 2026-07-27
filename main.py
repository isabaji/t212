#!/usr/bin/env python3
"""CLI entry point.

  python main.py account    # show cash and open positions
  python main.py backtest   # backtest the SMA strategy on the watchlist
  python main.py run        # run one trading cycle (respects DRY_RUN)
"""

import argparse
import logging

from t212bot.backtest import print_backtest
from t212bot.bot import run_cycle
from t212bot.client import Trading212Client
from t212bot.config import Config
from t212bot.strategy import SMACrossover

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def cmd_account(cfg: Config) -> None:
    client = Trading212Client(cfg.api_key, cfg.base_url)
    cash = client.account_cash()
    print(f"Environment: {cfg.env}")
    print(f"Free cash:   {cash.get('free')}")
    print(f"Invested:    {cash.get('invested')}")
    print(f"Total:       {cash.get('total')}")
    positions = client.portfolio()
    if positions:
        print("\nOpen positions:")
        for p in positions:
            print(f"  {p['ticker']:<14} qty {p['quantity']:>10}  "
                  f"avg {p['averagePrice']}  now {p['currentPrice']}")
    else:
        print("\nNo open positions.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading212 algorithmic trading bot")
    parser.add_argument("command", choices=["account", "backtest", "run"])
    parser.add_argument("--fast", type=int, default=20, help="fast SMA window")
    parser.add_argument("--slow", type=int, default=50, help="slow SMA window")
    args = parser.parse_args()

    cfg = Config()

    if args.command == "backtest":
        print_backtest(cfg.watchlist, args.fast, args.slow)
        return

    cfg.validate()
    if args.command == "account":
        cmd_account(cfg)
    elif args.command == "run":
        run_cycle(cfg, SMACrossover(args.fast, args.slow))


if __name__ == "__main__":
    main()
