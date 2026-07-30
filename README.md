# Trading212 Algorithmic Trading Bot

A Python starter framework for running automated trading strategies against your
Trading212 account, using the [official Trading212 Public API](https://t212public-api-docs.redoc.ly/).

> **Disclaimer:** This is educational software. Algorithmic trading can lose money
> quickly. Always develop and test against Trading212's **Practice (demo) account**
> first, and only trade live with money you can afford to lose. Nothing here is
> financial advice.

## How it works

Trading212's public API handles **execution** (placing/cancelling orders, reading
your portfolio and cash) but does **not** provide real-time market data. So the bot
is split into three layers:

```
┌───────────────────┐    ┌──────────────┐    ┌──────────────────┐
│  Market data       │ →  │   Strategy   │ →  │  Execution        │
│  (yfinance/Alpaca) │    │  (signals)   │    │  (Trading212 API) │
└───────────────────┘    └──────────────┘    └──────────────────┘
                                ↑
                         Risk manager (position sizing, limits)
```

1. **Data** — the swing strategy's daily bars come from Yahoo Finance
   (`yfinance`); the day-trade strategy's intraday bars come from Alpaca's
   free market data API instead (real-time IEX feed, 1-minute bars by
   default — see below for why).
2. **Strategy** — a pluggable class turns price history into BUY/SELL/HOLD signals.
   Two are included: a daily SMA-crossover (swing trading) and an intraday
   opening-range breakout (day trading) — see below.
3. **Risk** — position sizing, max open positions, and a cash buffer are enforced
   before any order is sent. A daily profit-target/loss-limit sits alongside this
   (see below) — it's a stop mechanism, not a return guarantee.
4. **Execution** — a rate-limit-aware client for the Trading212 REST API places
   market orders (practice or live).

## Setup

### 1. Get an API key

In the Trading212 app: **Settings → API (Beta) → Generate API key**.
Generate it while switched to your **Practice account** to get a demo key.
Grant it the scopes you need (account data, portfolio, orders — execute).

### 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# edit .env and paste your API key
```

Key settings in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `T212_API_KEY` | — | Your API key |
| `T212_API_SECRET` | — | Your API secret (shown alongside the key when you generate it) |
| `T212_ENV` | `demo` | `demo` (practice) or `live` |
| `DRY_RUN` | `true` | Log intended orders instead of sending them |
| `ALPACA_API_KEY` / `ALPACA_API_SECRET` | — | Day-trade bot only: free Alpaca account, data-only (no funding needed) — get one at [app.alpaca.markets](https://app.alpaca.markets) |
| `DAYTRADE_BAR_MINUTES` | `1` | Bar size for the day-trade strategy's intraday data (Alpaca) |
| `WATCHLIST` | `AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,JPM,V,UNH,HD,XOM,JNJ,PG,DIS,AVGO,CRM,NFLX,NKE,MCD,BAC,MA,PFE,ABBV,CVX,KO,WMT,BA,CAT,NEE` | Symbols the strategy scans (30 large-caps across 9 sectors by default) — same tickers work against both Yahoo and Alpaca for US equities |
| `DAILY_PROFIT_TARGET_PCT` | `0.0075` | Stop opening new positions once today's realized gain hits this fraction of account value |
| `DAILY_LOSS_LIMIT_PCT` | `0.01` | Stop opening new positions once today's realized loss hits this fraction of account value |
| `LOSING_STREAK_LIMIT` | `3` | Pause a symbol's new entries after this many losses in a row on it |
| `LOSING_STREAK_COOLDOWN_DAYS` | `5` | How long a symbol stays paused before resuming with a reset streak |
| `RISK_PER_TRADE_PCT` | `0.01` | Fraction of account risked per trade (see volatility-aware sizing below) |
| `ATR_MULTIPLE` | `2.0` | Stop distance for sizing = this × ATR |
| `ATR_PERIOD` | `14` | Bars used to compute ATR (daily bars for swing, `DAYTRADE_BAR_MINUTES`-sized bars for day-trade) |
| `MAX_SECTOR_EXPOSURE_PCT` | `0.35` | Max fraction of account value held in one sector at once (see portfolio-level risk below) |
| `MAX_PORTFOLIO_DRAWDOWN_PCT` | `0.15` | Pause new entries account-wide once realized returns fall this far below their high-water mark |

The bot refuses to run against `live` unless you also set
`I_UNDERSTAND_LIVE_TRADING_RISK=yes`.

### About the daily profit target

**No algorithm can guarantee a specific daily return, and this one doesn't
pretend to.** 0.5–1% *every single day* compounds to triple-digit annual
returns — not something any legitimate rules-based retail strategy delivers
consistently. What `DAILY_PROFIT_TARGET_PCT`/`DAILY_LOSS_LIMIT_PCT` actually do
is simpler and honest: once **realized** gains from closed trades today reach
the target, both bots stop opening *new* positions for the rest of the day —
existing positions still get managed and closed normally, the win just isn't
pushed further. The loss limit is the mirror image: past that threshold, no
new positions either, so a bad day doesn't compound. Some days will hit the
target, some won't, some will hit the loss limit instead — that's markets,
not a bug.

Implementation notes, since this involves cross-run state on a public repo:

- State (`state/daily_target.json`) tracks **only a running percentage**,
  never a dollar figure — the file is git-tracked and this repo is public, so
  it must never contain the account's actual value. Each workflow run commits
  it back so the target/limit persists across separate, ephemeral GitHub
  Actions runs; it resets at UTC midnight.
- The percentage is *realized* P&L only (from actual closed trades), not
  unrealized mark-to-market on open positions. For the day-trade bot (always
  flat by end of day) this is a complete picture of the day. For the swing
  bot, a single sale can realize gains that accrued over many prior days —
  so a big close can look like a big "today" in this tracker. That's an
  accepted approximation: pausing new entries after a large realized gain is
  still reasonable behavior even when it technically accrued earlier.
- Both bots share the same daily state (one target for the whole account, not
  one each), since the goal was framed as "the account," not "each bot."

### About trade tracking and the losing-streak pause

The bot **does not learn or adapt its own strategy logic from outcomes** — the
SMA-crossover and opening-range parameters are fixed regardless of results.
That's deliberate: letting a strategy quietly retune itself from a small
number of live trades is a classic way to overfit to noise rather than find a
real edge, with no visibility into why it changed. Instead there are two
honest, explainable pieces:

1. **Trade outcome tracking** (`t212bot/trade_stats.py`) — every closed trade
   is recorded as a win or loss per symbol per strategy: win/loss counts,
   cumulative realized %, and the current streak. This is a track record for
   *you* to look at (surfaced on the dashboard), not something the bot acts
   on beyond point 2 below.
2. **A per-symbol losing-streak circuit breaker** — after `LOSING_STREAK_LIMIT`
   losses in a row on one symbol, new entries on *that symbol only* pause for
   `LOSING_STREAK_COOLDOWN_DAYS`, then automatically resume with the streak
   reset. It's a simple, transparent rule ("this specific thing keeps not
   working, stop trying it for a while"), not a model rewriting its own
   decision logic.

Same public-repo constraint as the daily target: `state/trade_stats.json`
tracks only win/loss counts and percentages, never a dollar figure.

### About the performance breakdown chart

The dashboard also shows realized P&L across five trailing windows — last
hour, day, week, month, and year — as a bar chart, so you can see whether
recent performance looks different from the longer-run picture at a glance.

Every closed trade (both bots) is appended to a timestamped ledger
(`t212bot/pnl_history.py`, `state/pnl_history.json`) as `{time, strategy,
symbol, pnl_pct}` — same public-repo constraint as everywhere else, a
percentage of account value at the time of the trade, never a dollar figure.
Each window's bar is just the sum of `pnl_pct` for trades closed within that
window — the same "realized % relative to account value at each trade"
approximation the daily target uses, not a precisely compounded return. The
ledger is capped at the most recent 2000 trades so the file doesn't grow
unbounded. Until trades have actually closed, all five windows correctly
show 0% / no data — the chart never fabricates a number.

### About volatility-aware position sizing

Position sizing used to be flat: every symbol got up to the same percentage
of the account, regardless of how much it actually moves. That's not how
professional risk management works — a stock that swings 5% a day and one
that swings 0.5% a day aren't equally risky at the same dollar size.

`RiskManager` now sizes each position from its own recent volatility
(**A**verage **T**rue **R**ange, `t212bot/indicators.py`): the position is
sized so that a move of `ATR_MULTIPLE × ATR` against it costs roughly
`RISK_PER_TRADE_PCT` of the account — a volatile stock gets fewer shares than
a calm one for the same dollar risk, rather than every position getting an
identical percentage regardless of how it actually behaves. `MAX_POSITION_PCT`
still applies as a hard ceiling on top of this (so a very calm stock can't
get sized arbitrarily large), and available cash is still respected. If
there isn't enough price history yet to compute ATR, sizing falls back to
the flat `MAX_POSITION_PCT` cap.

### About signal-strength position sizing

On top of volatility-aware sizing, each BUY now carries a strength score
(0..1) from the strategy itself, and `RiskManager.size_buy` scales the
`MAX_POSITION_PCT` ceiling by that score — a strong signal is sized toward
the full cap, a marginal one gets a proportionally smaller slice of it. The
other caps (volatility, cash, sector) still apply on top via the same `min()`
as before, so strength only ever shrinks the position further, never grows
it past what those already allow.

What "strength" means is strategy-specific:
- **Swing (`SMACrossover`)**: how far price has already extended above its
  own slow SMA at the moment of the crossover, as a fraction of that SMA.
  A crossover that barely triggered sizes small; one that's already running
  sizes near the cap.
- **Day-trade (`OpeningRangeConfluence`)**: the average of three sub-scores —
  how far price broke past the opening range, how close RSI sits to the top
  of the bullish 50–70 band, and how wide the fast/slow EMA spread is.

Each normalization constant (e.g. `strength_norm_pct`) is a constructor
parameter with a sane default, not a new env var — tune it in code if the
defaults size too aggressively or too conservatively for a given strategy.

### About the anti-chase guard

Both strategies also have a `max_chase_pct` — if a check is late (a delayed
cron tick, a gap between manual triggers) and price has already run well past
the signal's own trigger level by the time it's evaluated, the BUY is
suppressed entirely rather than sized down. "Too far" means: more than 15%
above the slow SMA for swing, or more than 2% past the opening-range high for
day-trade — both configurable per strategy instance, and both independent of
`strength_norm_pct` (which controls sizing *within* the allowed range, not
whether a signal is allowed at all). This shows up in run history as its own
"Skipped ... to avoid chasing" line, distinct from a genuine no-signal Hold.
Set `max_chase_pct=None` on a strategy instance to disable it.

Backtests share this logic for free (same `generate_signals()` code path,
see `t212bot/backtest.py`) — a suppressed BUY just shows up as a HOLD in the
simulated trade history, same as live.

### About currency conversion

Yahoo Finance quotes US equities in USD, but a Trading212 account can be
denominated in any currency (e.g. GBP). Every cycle, the bot reads the
account's actual currency from `client.account_info()["currencyCode"]` and,
if it isn't USD, converts every USD price it works with — the watchlist
quotes it just fetched, and the `averagePrice`/`currentPrice` on each open
position from Trading212's own portfolio endpoint — into the account
currency using a live rate (`t212bot/data.py:fetch_fx_rate`, from Yahoo
Finance's `<ACCOUNT><INSTRUMENT>=X` pair, e.g. `GBPUSD=X`). This keeps
position sizing, sector exposure, and realized P&L math internally
consistent — without it, an account not denominated in USD would compare a
USD price against an account value in a different currency, sizing
positions off by roughly the exchange rate rather than the intended
percentage.

This assumes every instrument in the watchlist shares one currency (true
today — it's US-equities only, see `YAHOO_TO_T212` in `t212bot/data.py`); a
non-US instrument would need its own rate rather than this single blanket
conversion. If no FX rate can be fetched, the cycle fails loudly (logged as
a run error) rather than silently sizing a position as if two different
currencies were the same number.

### About portfolio-level risk controls

Everything above sizes and gates *one symbol at a time*. Two more checks look
at the account as a whole:

1. **Sector exposure cap** (`t212bot/sectors.py`, `MAX_SECTOR_EXPOSURE_PCT`) —
   the default watchlist is tagged with a rough sector for each symbol (e.g.
   AAPL/MSFT/NVDA → Technology, JPM/V → Financials). Before opening a new
   position, the bot totals up what's already held in that symbol's sector
   across open positions and caps the buy so the sector as a whole never
   exceeds `MAX_SECTOR_EXPOSURE_PCT` of account value — even if each
   individual buy looks affordable on its own. Five max-sized tech positions
   would all be exposed to the same sector-wide move; this stops that from
   happening by accident. Symbols outside the built-in map (anything you add
   to `WATCHLIST` that isn't in the default 30) each get treated as their own
   single-symbol sector, since there's no classification for them.
2. **Account-wide drawdown throttle** (`t212bot/portfolio_risk.py`,
   `MAX_PORTFOLIO_DRAWDOWN_PCT`) — tracks a running high-water mark of
   *realized* cumulative return, similar in spirit to the daily target but
   never resetting on its own. If realized returns fall `MAX_PORTFOLIO_DRAWDOWN_PCT`
   below that high-water mark, both bots stop opening new positions
   account-wide — existing positions still get managed and closed normally —
   until the drawdown recovers to less than half the threshold. That
   hysteresis is deliberate: without it, a drawdown sitting right at the
   boundary would flip the pause on and off every cycle.

Same public-repo constraint as the other state files: `state/portfolio_risk.json`
tracks only a compounded running percentage, never a dollar figure, and
persists across runs the same way (the workflow commits it back after each
cycle, shared between both bots).

### About the backtest

`python main.py backtest` is no longer a single total-return number over one
historical window — that style of backtest is easy to accidentally overfit
to (or get lucky/unlucky with) without realizing it. It now:

- **Models trading costs.** Trading212 charges no stock commission, but market
  orders still pay the bid-ask spread — approximated as slippage in basis
  points (`--slippage-bps`, default 5bps per side). A cost-free backtest
  overstates performance, especially for strategies that trade often.
- **Walks forward across multiple windows** (`--windows`, default 4 for swing,
  2 for day-trade) instead of one blended period, so a strategy that only
  "worked" in one lucky stretch shows up as inconsistent across windows
  rather than hiding inside a single average.
- **Reports risk-adjusted metrics** — CAGR, Sharpe, Sortino, max drawdown, win
  rate, profit factor, max consecutive losses — not just total return.
- **Runs the strategy's actual `generate_signals()`** at each historical
  point using only data available up to that point. This is the most
  important property: the backtest exercises the *exact same code* the live
  bot runs, not a separate re-implementation that could quietly diverge from
  it and give you a false sense of confidence.

Try it and read the output skeptically — on a first run against the built-in
SMA-crossover strategy on real recent data, don't be surprised to see it lose
in most windows. That's the backtest doing its job, not a bug: a genuinely
profitable strategy should look consistent *across* windows, not just show
one attractive blended number.

#### Optional: stop-loss and trend filter (swing only)

Two things worth testing before trusting the swing strategy with real money,
both off by default so existing behavior doesn't silently change:

- `--stop-atr-multiple N` — force-exits a position the first day its bar Low
  touches `entry_price - N × ATR(14)`, modeling a real stop order, instead of
  only ever exiting on the (slow) reverse crossover. Notably, `RiskManager`
  already *sizes* positions as if a stop like this exists (via
  `RISK_PER_TRADE_PCT`/`ATR_MULTIPLE`) — without one, that sizing assumption
  was never actually enforced. `--stop-atr-multiple 2.0` tests the same
  multiple the live bot already assumes.
- `--trend-filter N` — only takes a BUY signal when price is above its own
  N-day SMA, skipping crossover signals in a longer-term downtrend. 200 is
  the conventional choice (roughly a "golden cross" regime filter).

Both can be combined and run through the same walk-forward/cost-aware
machinery as everything else. `.github/workflows/backtest.yml` runs any
combination from the Actions tab (no secrets needed — this never touches the
live account) if you'd rather not run it locally.

### 4. Use it

```bash
python main.py account               # sanity check: show cash + positions
python main.py backtest               # walk-forward backtest the swing strategy
python main.py backtest --daytrade    # walk-forward backtest the day-trade strategy
python main.py backtest --windows 6 --slippage-bps 10   # tune the assumptions
python main.py run                    # run one swing-trading cycle (dry-run by default)
python main.py daytrade               # run one day-trading cycle (dry-run by default)
```

When you're happy with dry-run output, set `DRY_RUN=false` (still on `demo`) and
let it place practice orders. Only after that consider `T212_ENV=live`.

### 5. Schedule it

The bot is intentionally stateless — one invocation = one cycle. You have two options:

**Option A — GitHub Actions (recommended, no server needed):**

Two workflows are already set up:

- `.github/workflows/trading-bot.yml` — swing trading, every 15 minutes during
  US market hours (`python main.py run`).
- `.github/workflows/trading-bot-daytrade.yml` — day trading, every 5 minutes
  during a padded market-hours window (`python main.py daytrade`).

They're independent — enable one, both, or neither. To turn them on:

1. On GitHub, go to this repo → **Settings → Secrets and variables → Actions**.
2. Under **Secrets**, click **New repository secret** and add two secrets:
   - Name: `T212_API_KEY` — Value: your Trading212 API key
   - Name: `T212_API_SECRET` — Value: your Trading212 API secret
3. If you're enabling the day-trade workflow, also add:
   - Name: `ALPACA_API_KEY` — Value: your free Alpaca API key (data only, no
     funding needed — generate one at [app.alpaca.markets](https://app.alpaca.markets))
   - Name: `ALPACA_API_SECRET` — Value: the matching Alpaca API secret
4. (Optional) **Also under Secrets** (not Variables — see the note below), add
   any of `T212_ENV`, `DRY_RUN`, `DAYTRADE_DRY_RUN`, `WATCHLIST`,
   `DAYTRADE_WATCHLIST`, `DAYTRADE_BAR_MINUTES`, `MAX_POSITION_PCT`,
   `MAX_OPEN_POSITIONS`, `CASH_BUFFER_PCT` to override the defaults (`demo`,
   `true`, `true`, 15 large-caps across sectors — see the table above, same
   list for `WATCHLIST`, `1`, `0.10`, `5`, `0.05`).
   `DRY_RUN` and `DAYTRADE_DRY_RUN` are separate on purpose, so you can arm one
   mode without arming the other. Leave everything unset to start safely in
   demo/dry-run mode.
5. Go to the **Actions** tab → pick a workflow → **Run workflow** to trigger
   it manually and check the logs, or just wait for the schedule.

No terminal or local Python install required — GitHub runs it for you.

> **Why config lives in Secrets, not Variables, on this repo:** this repo is
> public, so its GitHub Actions run logs are public too. GitHub automatically
> masks any value stored as a **Secret** wherever it appears in a log, but
> **Variables print in plain text** — so if `T212_ENV` or `WATCHLIST` were
> Variables, anyone could read them straight off the Actions tab, no access
> needed. Storing them as Secrets instead (even though they're not
> credentials) gets them the same automatic masking. On top of that, the
> bot's own logging deliberately never prints account value, cash, position
> sizes, or trade dollar amounts, and a BUY/SELL log line looks identical
> whether it was a dry run or a real order — none of that is something
> auto-masking can protect (it only works on values GitHub already knows to
> look for), so it's scrubbed at the source instead. Full detail (exact
> figures, order IDs, the dry-run flag) is still written locally each cycle
> to `logs/history.jsonl` (gitignored, never pushed) if you want it for your
> own use — it just doesn't flow into the public log stream.

**Option B — your own machine/server with cron:**

```cron
*/15 13-21 * * 1-5  cd /path/to/t212 && .venv/bin/python main.py run       >> swing.log 2>&1
*/5  13-20 * * 1-5  cd /path/to/t212 && .venv/bin/python main.py daytrade  >> daytrade.log 2>&1
```

## Day trading: what "OpeningRangeConfluence" actually does, and its limits

`python main.py daytrade` runs an intraday strategy (`OpeningRangeConfluence` in
`t212bot/strategy.py`) on `DAYTRADE_BAR_MINUTES`-sized bars (1-minute by default):

1. **Entry** — after the first 30 minutes of the session (the "opening range"),
   it buys only if *all three* agree: price breaks above the opening-range
   high, the fast EMA (9) is above the slow EMA (21), and RSI(14) is in a
   bullish-but-not-overbought band (50-70). Requiring confluence means fewer,
   higher-conviction trades rather than firing on any single indicator.
2. **Exit** — a hair trigger by comparison: *any one* of a breakdown below the
   opening-range low, the EMA trend flipping down, or RSI going overbought/
   oversold closes the position.
3. **End-of-day flatten** — regardless of what the strategy signals, any open
   position is force-closed once the exchange-local time is within 15 minutes
   of the close (`t212bot/bot.py: run_day_trade_cycle`). No position is ever
   held overnight — that's what makes this day trading rather than swing
   trading. New entries are also refused in the last 30 minutes, since there
   isn't enough session left to manage them.
4. **Stale-data guard** — if the latest available bar is more than 20 minutes
   old (pre-market, weekend, a holiday, or just data lag), new entries are
   skipped. Closing an existing position still goes through even on stale
   data, since a market order fills at the live venue price regardless of the
   reference price the bot last saw.

**Real constraints to know before trusting this with money:**

- Intraday data comes from Alpaca's free tier (real-time IEX feed, not
  delayed like Yahoo's free intraday data) — but it's still single-exchange
  IEX, not the full consolidated tape, and the trigger cadence (every 5
  minutes by default via GitHub Actions/cron-job.org) is coarser than the
  1-minute bars themselves. Reacting to 1-minute bars only every 5 minutes
  still narrows the reaction window versus 5-minute bars, but this isn't
  built for scalping or anything needing sub-minute execution.
- Backtesting the day-trade strategy (`--daytrade`) still pulls 5-minute
  bars regardless of `DAYTRADE_BAR_MINUTES`, to keep the backtest quick —
  see `t212bot/backtest.py`.
- GitHub Actions' cron scheduler fires at 5-minute granularity at best, and
  isn't guaranteed to hit the exact minute under load (a third-party
  scheduler hitting the repo's `repository_dispatch` API is a more reliable
  alternative — see the workflow files' comments).
- `OpeningRangeConfluence` is a reasonable, well-known starting pattern — not
  a proven edge. Backtest and dry-run it extensively before considering
  `DAYTRADE_DRY_RUN=false`, and separately again before `T212_ENV=live`.
- Check your account's terms for any day-trading-specific restrictions before
  going live; this repo doesn't attempt to detect or enforce any such rules
  (e.g. the US Pattern Day Trader rule, which shouldn't apply to a Trading212
  account but wasn't something I could verify against your specific account).

## Extending

- **New strategy:** subclass `Strategy` in `t212bot/strategy.py` and implement
  `generate_signals()`. Wire it up in `main.py`.
- **Ticker mapping:** Trading212 uses tickers like `AAPL_US_EQ`; Yahoo uses `AAPL`.
  `t212bot/data.py` maps between them — extend `YAHOO_TO_T212` for non-US listings.

## Trading212 API notes & limitations

- Base URLs: `https://demo.trading212.com` (practice), `https://live.trading212.com`.
- Auth: HTTP Basic (`Authorization: Basic base64(api_key:api_secret)`). Demo and
  live keys/secrets are each valid only against their matching base URL.
- Sell orders are placed by sending a **negative quantity**.
- Endpoints are rate-limited (the client auto-retries on HTTP 429 with backoff).
- Max 50 pending orders per ticker per account.
- No streaming/real-time quotes from the API — hence the external data feed.
- The API is in beta; check the [official docs](https://t212public-api-docs.redoc.ly/)
  for current behavior.
