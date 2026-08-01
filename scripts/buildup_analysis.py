"""One-off diagnostic: what does the intraday "build-up" look like before a
stock gains >=1% over the rest of the day?

For each symbol-day across ~60 days of 5-minute bars for the watchlist, take a
fixed decision point (10:30 ET, i.e. after the first hour of trading) and
compute a battery of indicator features using ONLY bars up to that point (plus
prior days -- no lookahead). Label the day a HIT if price rises >= 1% from the
decision price to that day's close. Then:
  1. compare feature distributions for hits vs misses, and
  2. mine simple conjunctive rules (1-3 binary conditions) for the highest
     hit rate with meaningful support, reporting lift vs the base rate so a
     "good-looking" precision that just matches chance is visible as lift~1.

Not part of the package -- run once via a temporary workflow step, then
deleted (same pattern as the earlier regime-analysis scratch tooling).
"""

import itertools
import os
import sys
from datetime import time as dtime

sys.path.insert(0, ".")

import pandas as pd  # noqa: E402

from t212bot.config import Config  # noqa: E402
from t212bot.data import fetch_intraday  # noqa: E402
from t212bot.indicators import ema, rsi, vwap  # noqa: E402

DECISION_TIME = dtime(10, 30)   # decide after the first hour of trading
OR_BARS = 6                     # 30-minute opening range on 5-min bars
TARGET_PCT = 1.0                # ">= 1% increase" threshold
MIN_MORNING_BARS = 10           # require a mostly-complete first hour
MIN_AFTERNOON_BARS = 40         # excludes half days / gappy days
MIN_VOL_HISTORY = 5             # prior days needed for the volume ratio
MIN_SUPPORT = 40                # a mined rule must match this many days

symbols = Config().watchlist
print(f"Fetching 60 days of 5-min data for {len(symbols)} symbols...")
price_data = fetch_intraday(symbols, os.environ["ALPACA_API_KEY"],
                             os.environ["ALPACA_API_SECRET"], days=60, timeframe="5Min")
print(f"Got data for {len(price_data)} symbols.\n")

rows = []
for sym, df in sorted(price_data.items()):
    if df.empty:
        continue
    # IEX trades pre-market (from 08:00 ET) and post-market (to 17:00 ET), so
    # the feed can include bars outside regular hours where any trade printed.
    # Keep regular-session bars only; otherwise "morning"/opening-range/
    # first-hour-volume would silently absorb thin pre-market prints.
    df = df[(df.index.time >= dtime(9, 30)) & (df.index.time < dtime(16, 0))]
    if df.empty:
        continue
    # Continuous-series indicators, computed once over the full frame. All of
    # these are causal (EMA/RSI only look backward; VWAP resets each session),
    # so reading their value AT the decision bar uses no future data.
    close_full = df["Close"]
    ema9_full = ema(close_full, 9)
    ema21_full = ema(close_full, 21)
    rsi_full = rsi(close_full, 14)
    vwap_full = vwap(df)

    days = sorted(set(df.index.date))
    # Per-day first-hour volume, for the "is this morning's volume unusual"
    # ratio (compares to the SAME window on prior days, not to a whole-day
    # average, so it isn't skewed by the usual U-shaped intraday volume curve).
    fh_volume: dict = {}
    day_close: dict = {}
    day_high: dict = {}
    day_low: dict = {}
    for d in days:
        day_df = df[df.index.date == d]
        morning = day_df[day_df.index.time < DECISION_TIME]
        fh_volume[d] = morning["Volume"].sum() if len(morning) else None
        day_close[d] = day_df["Close"].iloc[-1]
        day_high[d] = day_df["High"].max()
        day_low[d] = day_df["Low"].min()

    for i, d in enumerate(days):
        if i == 0:
            continue  # need a prior day for gap/prev-day features
        day_df = df[df.index.date == d]
        morning = day_df[day_df.index.time < DECISION_TIME]
        afternoon = day_df[day_df.index.time >= DECISION_TIME]
        if len(morning) < MIN_MORNING_BARS or len(afternoon) < MIN_AFTERNOON_BARS:
            continue
        if morning.index[0].time() > dtime(9, 40):
            continue  # late-starting data; opening range would be wrong

        decision_bar = morning.index[-1]
        decision_price = morning["Close"].iloc[-1]
        today_open = morning["Open"].iloc[0]
        prev_d = days[i - 1]
        prev_close = day_close[prev_d]

        opening_range = morning.iloc[:OR_BARS]
        or_high = opening_range["High"].max()

        prior_vols = [fh_volume[days[j]] for j in range(max(0, i - 20), i)
                      if fh_volume[days[j]]]
        vol_ratio = (fh_volume[d] / (sum(prior_vols) / len(prior_vols))
                     if len(prior_vols) >= MIN_VOL_HISTORY and fh_volume[d] else None)

        ret5d = None
        if i >= 6:
            base = day_close[days[i - 6]]
            ret5d = (prev_close - base) / base * 100
        prev_range = day_high[prev_d] - day_low[prev_d]
        prev_close_pos = ((prev_close - day_low[prev_d]) / prev_range
                          if prev_range > 0 else None)
        prev_prev_close = day_close[days[i - 2]] if i >= 2 else None
        prev_day_ret = ((prev_close - prev_prev_close) / prev_prev_close * 100
                        if prev_prev_close else None)

        fwd_close_ret = (day_close[d] - decision_price) / decision_price * 100
        fwd_max_ret = (afternoon["High"].max() - decision_price) / decision_price * 100
        fwd_min_ret = (afternoon["Low"].min() - decision_price) / decision_price * 100

        rows.append({
            "symbol": sym, "date": d,
            "gap_pct": (today_open - prev_close) / prev_close * 100,
            "fh_ret": (decision_price - today_open) / today_open * 100,
            "orb_break": decision_price > or_high,
            "vwap_dist": (decision_price - vwap_full.loc[decision_bar])
                          / vwap_full.loc[decision_bar] * 100,
            "vol_ratio": vol_ratio,
            "rsi14": rsi_full.loc[decision_bar],
            "ema_spread": (ema9_full.loc[decision_bar] - ema21_full.loc[decision_bar])
                           / ema21_full.loc[decision_bar] * 100,
            "prev_day_ret": prev_day_ret,
            "prev_close_pos": prev_close_pos,
            "ret5d": ret5d,
            "fwd_close_ret": fwd_close_ret,
            "fwd_max_ret": fwd_max_ret,
            "fwd_min_ret": fwd_min_ret,
            "hit": fwd_close_ret >= TARGET_PCT,
            "hit_high": fwd_max_ret >= TARGET_PCT,
        })

data = pd.DataFrame(rows)
n = len(data)
base_rate = data["hit"].mean() * 100
base_rate_high = data["hit_high"].mean() * 100
print(f"{n} usable symbol-days.")
print(f"Base rate: {base_rate:.1f}% of days gain >={TARGET_PCT}% from 10:30 to close "
      f"({base_rate_high:.1f}% touch >={TARGET_PCT}% intraday high after 10:30).")
print(f"Mean forward close return (all days): {data['fwd_close_ret'].mean():+.3f}%\n")

print("=== Feature means: hit days vs miss days (close target) ===")
features = ["gap_pct", "fh_ret", "vwap_dist", "vol_ratio", "rsi14", "ema_spread",
            "prev_day_ret", "prev_close_pos", "ret5d"]
hits, misses = data[data["hit"]], data[~data["hit"]]
print(f"{'feature':<16}{'hit mean':>10}{'miss mean':>11}")
for f in features:
    print(f"{f:<16}{hits[f].mean():>10.3f}{misses[f].mean():>11.3f}")
orb_hit = data[data["orb_break"]]["hit"].mean() * 100
orb_miss = data[~data["orb_break"]]["hit"].mean() * 100
print(f"\nhit rate when orb_break=True: {orb_hit:.1f}%  vs False: {orb_miss:.1f}%\n")

# --- Rule mining: conjunctions of 1-3 binary conditions -------------------
# Thresholds fixed a priori (not tuned on this data) to limit the garden of
# forking paths; the count of combinations scanned is printed so the
# multiple-comparisons risk is explicit, not hidden.
conds = {
    "gap_up": data["gap_pct"] > 0.3,
    "gap_dn": data["gap_pct"] < -0.3,
    "fh_up": data["fh_ret"] > 0,
    "fh_strong": data["fh_ret"] > 0.5,
    "fh_down": data["fh_ret"] < 0,
    "orb_break": data["orb_break"],
    "above_vwap": data["vwap_dist"] > 0,
    "vwap_ext": data["vwap_dist"] > 0.3,
    "vol_warm": data["vol_ratio"] > 1.2,
    "vol_hot": data["vol_ratio"] > 1.5,
    "rsi_bull": (data["rsi14"] >= 55) & (data["rsi14"] <= 75),
    "rsi_hot": data["rsi14"] > 70,
    "rsi_cold": data["rsi14"] < 40,
    "ema_up": data["ema_spread"] > 0,
    "ema_strong": data["ema_spread"] > 0.15,
    "prev_up": data["prev_day_ret"] > 0,
    "prev_strong_close": data["prev_close_pos"] > 0.7,
    "mom5": data["ret5d"] > 0,
}
conds = {k: v.fillna(False) for k, v in conds.items()}

results = []
names = list(conds)
n_scanned = 0
for r in (1, 2, 3):
    for combo in itertools.combinations(names, r):
        n_scanned += 1
        mask = conds[combo[0]].copy()
        for c in combo[1:]:
            mask &= conds[c]
        support = int(mask.sum())
        if support < MIN_SUPPORT:
            continue
        sub = data[mask]
        results.append({
            "rule": "+".join(combo), "n": support,
            "hit%": sub["hit"].mean() * 100,
            "lift": (sub["hit"].mean() * 100) / base_rate if base_rate else 0,
            "avg_fwd%": sub["fwd_close_ret"].mean(),
            "avg_min%": sub["fwd_min_ret"].mean(),
        })
res = pd.DataFrame(results)
print(f"=== Rule mining: {n_scanned} combos scanned, {len(res)} with support >= {MIN_SUPPORT} ===")
pd.set_option("display.width", 160)
print(f"\nTop 25 by hit rate (close target, base rate {base_rate:.1f}%):")
print(res.sort_values("hit%", ascending=False).head(25).to_string(
    index=False, float_format=lambda x: f"{x:.2f}"))
print("\nTop 15 by average forward close return:")
print(res.sort_values("avg_fwd%", ascending=False).head(15).to_string(
    index=False, float_format=lambda x: f"{x:.2f}"))
