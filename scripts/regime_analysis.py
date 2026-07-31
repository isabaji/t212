"""One-off diagnostic: trailing-60-day price regime (trend, volatility,
smoothness) for the current 30-symbol watchlist, unfiltered by backtest
trade count -- used to test whether screening WITHIN the current 30 by
regime improves the live ensemble's performance. Scratch, not permanent.
"""

import os
import sys

sys.path.insert(0, ".")
from t212bot.data import fetch_intraday  # noqa: E402
from t212bot.config import Config  # noqa: E402

cfg = Config()
symbols = sorted(cfg.watchlist)
print(f"Fetching 60 days of 5-min data for {len(symbols)} symbols...")

price_data = fetch_intraday(symbols, os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"],
                             days=60, timeframe="5Min")
print(f"Got data for {len(price_data)} symbols.\n")

rows = []
for sym, df in sorted(price_data.items()):
    if df.empty:
        continue
    daily_close = df["Close"].resample("1D").last().dropna()
    daily_high = df["High"].resample("1D").max().dropna()
    daily_low = df["Low"].resample("1D").min().dropna()
    if len(daily_close) < 10:
        continue

    total_return_pct = (daily_close.iloc[-1] - daily_close.iloc[0]) / daily_close.iloc[0] * 100
    daily_range_pct = ((daily_high - daily_low) / daily_close).mean() * 100

    daily_returns = daily_close.pct_change().dropna()
    path_length = daily_returns.abs().sum()
    net_move = abs(daily_close.iloc[-1] - daily_close.iloc[0]) / daily_close.iloc[0]
    smoothness = net_move / path_length if path_length > 0 else 0.0

    rows.append((sym, total_return_pct, daily_range_pct, smoothness))

print(f"{'SYM':<6}{'ret%':>8}{'range%':>9}{'smooth':>9}")
for sym, ret, rng, smooth in sorted(rows, key=lambda r: r[2]):
    print(f"{sym:<6}{ret:>8.2f}{rng:>9.3f}{smooth:>9.4f}")

avg_range = sum(r[2] for r in rows) / len(rows)
avg_smooth = sum(r[3] for r in rows) / len(rows)
print(f"\nAverage daily_range%={avg_range:.2f}, average smoothness={avg_smooth:.3f} across {len(rows)} symbols")
