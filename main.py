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
from t212bot.strategy import EnsembleVote, GapFillReversal, MeanReversionPullback, OpeningRangeConfluence, SMACrossover
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
    parser.add_argument("--confirm-bars", type=int, default=None,
                         help="backtest --daytrade only: 1-min confirmation bars required to hold above the "
                              "breakout before entry (default: DAYTRADE_CONFIRM_BARS config value; pass 0 to "
                              "compare against the gate disabled)")
    parser.add_argument("--stop-loss-pct", type=float, default=None,
                         help="backtest --daytrade only: fixed stop-loss as a fraction of entry price "
                              "(e.g. 0.002 for 0.2%%), checked against each bar's Low (off by default)")
    parser.add_argument("--take-profit-pct", type=float, default=None,
                         help="backtest --daytrade only: fixed take-profit as a fraction of entry price "
                              "(e.g. 0.006 for 0.6%%), checked against each bar's High (off by default)")
    parser.add_argument("--rsi-buy-min", type=float, default=None,
                         help="backtest --daytrade only: lower bound of the RSI buy band "
                              "(default: strategy default 50)")
    parser.add_argument("--rsi-buy-max", type=float, default=None,
                         help="backtest --daytrade only: upper bound of the RSI buy band "
                              "(default: strategy default 70)")
    parser.add_argument("--min-ema-spread-pct", type=float, default=None,
                         help="backtest --daytrade only: hard minimum EMA(9)/EMA(21) spread required to "
                              "enter, as a fraction of the slow EMA e.g. 0.001 for 0.1%% (off by default)")
    parser.add_argument("--min-strength", type=float, default=None,
                         help="backtest --daytrade only: hard minimum combined setup-quality score "
                              "(0-1, average of breakout/momentum/trend sub-scores) required to enter "
                              "(off by default)")
    parser.add_argument("--min-volume-ratio", type=float, default=None,
                         help="backtest --daytrade only: hard minimum ratio of breakout-bar volume to its "
                              "own trailing 20-bar average volume required to enter, e.g. 1.2 for 20%% "
                              "above average (off by default)")
    parser.add_argument("--require-retest", action="store_true",
                         help="backtest --daytrade only: replace the entry trigger with a pullback-and-hold "
                              "retest of the opening-range high instead of buying the first breakout bar "
                              "(off by default)")
    parser.add_argument("--retest-tolerance-pct", type=float, default=0.003,
                         help="backtest --daytrade only: with --require-retest, how far below the "
                              "opening-range high a pullback may go and still count as a valid retest "
                              "(default 0.003 = 0.3%%)")
    parser.add_argument("--strategy", choices=["orb", "mean_reversion", "bollinger", "gap_fill", "ensemble"],
                         default="orb",
                         help="backtest --daytrade only: 'orb' (default) is OpeningRangeConfluence -- all "
                              "the --confirm-bars/--rsi-buy-*/--min-*/--require-retest flags above only "
                              "apply to it. 'mean_reversion' is MeanReversionPullback (buys pullbacks to "
                              "the fast EMA within an uptrend instead of chasing breakouts). 'bollinger' "
                              "is BollingerSqueezeBreakout (buys a volatility-squeeze breakout above the "
                              "upper Bollinger Band). 'gap_fill' is GapFillReversal (buys a gap-down open "
                              "reversing back up early in the session). 'ensemble' requires a subset of "
                              "these to agree (see --ensemble-strategies/--ensemble-min-votes) before "
                              "buying. Non-orb strategies use their own code defaults for now")
    parser.add_argument("--ensemble-min-votes", type=int, default=None,
                         help="backtest --daytrade --strategy ensemble only: how many of the selected "
                              "sub-strategies must agree (default: unanimous, all of them)")
    parser.add_argument("--ensemble-strategies", default="orb,mr,vwap",
                         help="backtest --daytrade --strategy ensemble only: comma-separated subset of "
                              "orb,mr,vwap,bb,gap to include in the vote (default: orb,mr,vwap; try "
                              "'orb,gap' to test the opening-range + gap-fill pair)")
    parser.add_argument("--ensemble-required", default=None,
                         help="backtest --daytrade --strategy ensemble only: comma-separated subset of "
                              "--ensemble-strategies that must ALL vote BUY for any combination to count, "
                              "on top of --ensemble-min-votes -- e.g. with orb,mr,gap at min-votes 2 and "
                              "--ensemble-required orb, orb+mr and orb+gap both count but mr+gap alone "
                              "does not, even though it's also '2 of 3' (off by default)")
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
            confirm_bars = args.confirm_bars if args.confirm_bars is not None else cfg.daytrade_confirm_bars
            rsi_buy_range = None
            if args.rsi_buy_min is not None or args.rsi_buy_max is not None:
                rsi_buy_range = (args.rsi_buy_min if args.rsi_buy_min is not None else 50,
                                  args.rsi_buy_max if args.rsi_buy_max is not None else 70)
            print_daytrade_backtest(cfg.watchlist, cfg.alpaca_api_key, cfg.alpaca_api_secret,
                                     args.or_minutes, args.windows or 2, args.fee_bps, args.slippage_bps,
                                     confirm_bars=confirm_bars,
                                     stop_loss_pct=args.stop_loss_pct, take_profit_pct=args.take_profit_pct,
                                     rsi_buy_range=rsi_buy_range, min_ema_spread_pct=args.min_ema_spread_pct,
                                     min_strength=args.min_strength, min_volume_ratio=args.min_volume_ratio,
                                     require_retest=args.require_retest,
                                     retest_tolerance_pct=args.retest_tolerance_pct,
                                     strategy_name=args.strategy,
                                     ensemble_min_votes=args.ensemble_min_votes,
                                     ensemble_strategies=args.ensemble_strategies,
                                     ensemble_required=args.ensemble_required)
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
        # The validated 3-way ensemble (see `backtest --daytrade --strategy
        # ensemble --ensemble-strategies orb,mr,gap --ensemble-min-votes 2
        # --ensemble-required orb`) is the only live day-trade strategy -- 2
        # of 3 must agree AND ORB must be one of them, built exactly as
        # backtested: ORB with no confirm-bars gate and no EMA-spread gate
        # (those are separate orb-only tuning, not part of what was
        # validated for these pairs). The ORB-required gate exists because a
        # 60-day/30-symbol backtest showed MeanReversionPullback+
        # GapFillReversal agreeing on its own is weak and sparse (~11-18%
        # win rate) and drags down the blended average -- requiring ORB lets
        # both good pairings (ORB+MR, ORB+Gap) still fire while blocking
        # that one. Confirmed across two window counts (6 and 12): gating
        # raised the blended win rate from ~44-45% to ~47-48% and roughly
        # doubled-to-tripled avg pnl/trade versus the plain (ungated) vote.
        # Deliberately one ensemble rather than two separate bots (e.g.
        # ORB+MR and ORB+Gap run independently) -- both pairs share the ORB
        # breakout as their trigger, so two independent bots against the
        # same account/watchlist could both buy the same symbol in the same
        # cycle, or one bot's SELL could close a position the other bot
        # thinks it still owns (position tracking reads real broker state
        # each cycle, not "which bot bought this"). A single EnsembleVote
        # makes one decision per symbol per cycle, so that conflict can't
        # happen. The standalone/pairwise strategies are still available for
        # backtesting/comparison via `backtest --daytrade --strategy ...`,
        # just not for live trading.
        orb = OpeningRangeConfluence(or_minutes=args.or_minutes, bar_minutes=cfg.daytrade_bar_minutes,
                                     confirm_bars=0)
        strategy = EnsembleVote([
            orb,
            MeanReversionPullback(),
            GapFillReversal(),
        ], min_votes=2, required=[orb])
        run_day_trade_cycle(cfg, strategy)
    elif args.command == "test-order":
        place_test_order(cfg, args.symbol, args.qty)


if __name__ == "__main__":
    main()
