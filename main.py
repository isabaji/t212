#!/usr/bin/env python3
"""CLI entry point.

  python main.py account             # show cash and open positions
  python main.py backtest            # walk-forward backtest the swing strategy
  python main.py backtest --daytrade # walk-forward backtest the day-trade strategy
  python main.py run                 # run one trading cycle (respects DRY_RUN)
  python main.py test-order          # place one small manual order (respects DRY_RUN)
"""

import argparse
import logging

from t212bot.backtest import DEFAULT_FEE_BPS, DEFAULT_SLIPPAGE_BPS, print_backtest, print_daytrade_backtest
from t212bot.bot import run_cycle, run_day_trade_cycle
from t212bot.client import Trading212Client
from t212bot.config import Config
from t212bot.strategy import OpeningRangeConfluence, SMACrossover
from t212bot.test_order import place_test_order

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def cmd_account(cfg: Config) -> None:
    client = Trading212Client(cfg.api_key, cfg.api_secret, cfg.base_url)
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
    parser.add_argument("command", choices=["account", "backtest", "run", "daytrade", "test-order"])
    parser.add_argument("--symbol", default="AAPL", help="test-order command only: Yahoo symbol to buy")
    parser.add_argument("--qty", type=float, default=0.01,
                         help="test-order command only: share quantity to buy (small, fractional)")
    parser.add_argument("--fast", type=int, default=20, help="fast SMA window (swing strategy)")
    parser.add_argument("--slow", type=int, default=50, help="slow SMA window (swing strategy)")
    parser.add_argument("--or-minutes", type=int, default=30, help="opening range window (day-trade strategy)")
    parser.add_argument("--daytrade", action="store_true",
                         help="backtest command only: backtest the day-trade strategy instead of swing")
    parser.add_argument("--windows", type=int, default=None,
                         help="backtest command only: number of walk-forward windows (default 4 swing / 2 daytrade)")
    parser.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS,
                         help="backtest command only: simulated fee in basis points per side")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS,
                         help="backtest command only: simulated slippage in basis points per side")
    parser.add_argument("--stop-atr-multiple", type=float, default=None,
                         help="backtest command only (swing): force-exit at entry - N x ATR(14), "
                              "modeling a real stop order (off by default)")
    parser.add_argument("--trend-filter", type=int, default=None,
                         help="backtest command only (swing): require price above its own N-day SMA "
                              "to take a BUY signal (off by default)")
    args = parser.parse_args()

    cfg = Config()

    if args.command == "backtest":
        if args.daytrade:
            print_daytrade_backtest(cfg.watchlist, args.or_minutes, args.windows or 2,
                                     args.fee_bps, args.slippage_bps)
        else:
            print_backtest(cfg.watchlist, args.fast, args.slow, args.windows or 4,
                            args.fee_bps, args.slippage_bps,
                            stop_atr_multiple=args.stop_atr_multiple, trend_filter=args.trend_filter)
        return

    cfg.validate()
    if args.command == "account":
        cmd_account(cfg)
    elif args.command == "run":
        run_cycle(cfg, SMACrossover(args.fast, args.slow))
    elif args.command == "daytrade":
        run_day_trade_cycle(cfg, OpeningRangeConfluence(or_minutes=args.or_minutes))
    elif args.command == "test-order":
        place_test_order(cfg, args.symbol, args.qty)


if __name__ == "__main__":
    main()
