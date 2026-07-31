"""One-off diagnostic: does a symbol's own trailing-60-day price regime
(trend strength, volatility, choppiness) correlate with how well the live
ORB+MeanReversion+GapFillReversal ensemble backtested on it?

Not part of the package -- run once via a temporary workflow step, not
committed as permanent tooling. Per-symbol backtest performance is embedded
below (parsed from already-run backtest.yml job logs); this script only
needs to fetch price data and compute regime metrics against those numbers.
"""

import json
import os
import sys

sys.path.insert(0, ".")
from t212bot.data import fetch_intraday  # noqa: E402

with open("scripts/combined_perf.json") as f:
    BACKTEST_PERF = json.load(f)

all_symbols = sorted(BACKTEST_PERF.keys())
print(f"Fetching 60 days of 5-min data for {len(all_symbols)} symbols...")

price_data = fetch_intraday(all_symbols, os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"],
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

    perf = BACKTEST_PERF.get(sym)
    if perf is None or perf["trades"] < 2:
        continue  # too few trades for that symbol's own win rate to mean anything

    rows.append({
        "symbol": sym,
        "total_return_pct": round(total_return_pct, 2),
        "daily_range_pct": round(daily_range_pct, 3),
        "smoothness": round(smoothness, 4),
        "bt_trades": perf["trades"],
        "bt_win_rate": round(100 * perf["wins"] / perf["trades"], 1),
        "bt_avg_pnl": round(perf["pnl_sum"] / perf["trades"], 3),
    })

print(f"{len(rows)} symbols with >=2 backtest trades and enough daily bars.\n")
print(f"{'SYM':<6}{'ret%':>8}{'range%':>9}{'smooth':>9}{'trades':>8}{'btWR%':>8}{'btPnl%':>9}")
for r in sorted(rows, key=lambda r: -r["bt_win_rate"]):
    print(f"{r['symbol']:<6}{r['total_return_pct']:>8.2f}{r['daily_range_pct']:>9.3f}"
          f"{r['smoothness']:>9.4f}{r['bt_trades']:>8}{r['bt_win_rate']:>8.1f}{r['bt_avg_pnl']:>9.3f}")

# Simple Pearson correlations between each regime metric and backtest performance.
import statistics  # noqa: E402


def pearson(xs, ys):
    n = len(xs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0.0


metrics = ["total_return_pct", "daily_range_pct", "smoothness"]
targets = ["bt_win_rate", "bt_avg_pnl"]
print("\n=== Correlation (Pearson r) between regime metric and backtest performance ===")
for m in metrics:
    for t in targets:
        xs = [r[m] for r in rows]
        ys = [r[t] for r in rows]
        r = pearson(xs, ys)
        print(f"  {m:<20} vs {t:<12} r = {r:+.3f}")

# Also: absolute total return (trend strength regardless of direction) vs performance.
print()
for t in targets:
    xs = [abs(r["total_return_pct"]) for r in rows]
    ys = [r[t] for r in rows]
    r = pearson(xs, ys)
    print(f"  |total_return_pct|     vs {t:<12} r = {r:+.3f}")
