"""Quick single-asset backtest so you can sanity-check a strategy before running it.

This is intentionally simple: long-only, all-in/all-out per symbol, no fees or
slippage. Good for "does this idea even work directionally", not for precise
performance claims.
"""

import pandas as pd

from .data import fetch_history
from .strategy import SMACrossover


def backtest_sma(symbols: list[str], fast: int = 20, slow: int = 50,
                 period: str = "5y") -> pd.DataFrame:
    rows = []
    for sym, df in fetch_history(symbols, period=period).items():
        close = df["Close"]
        fast_sma = close.rolling(fast).mean()
        slow_sma = close.rolling(slow).mean()
        # In the market whenever fast SMA > slow SMA (position taken next bar).
        in_market = (fast_sma > slow_sma).shift(1).fillna(False)
        daily_ret = close.pct_change().fillna(0)
        strat_ret = daily_ret.where(in_market, 0)

        equity = (1 + strat_ret).cumprod()
        bh_equity = (1 + daily_ret).cumprod()
        drawdown = equity / equity.cummax() - 1

        rows.append({
            "symbol": sym,
            "strategy_return": equity.iloc[-1] - 1,
            "buy_hold_return": bh_equity.iloc[-1] - 1,
            "max_drawdown": drawdown.min(),
            "time_in_market": in_market.mean(),
            "trades": int((in_market != in_market.shift(1)).sum()),
        })
    return pd.DataFrame(rows).set_index("symbol")


def print_backtest(symbols: list[str], fast: int = 20, slow: int = 50) -> None:
    results = backtest_sma(symbols, fast, slow)
    if results.empty:
        print("No data for any symbol.")
        return
    strat = SMACrossover(fast, slow)
    print(f"SMA({strat.fast}/{strat.slow}) crossover, 5y daily, long-only, no fees:\n")
    with pd.option_context("display.float_format", "{:+.1%}".format):
        print(results[["strategy_return", "buy_hold_return", "max_drawdown", "time_in_market"]])
    print(f"\nTrades per symbol: {results['trades'].to_dict()}")
