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
   free market data API instead (real-time IEX feed, 5-minute bars by
   default, configurable — see below for why). The news strategy adds
   headlines from Yahoo Finance's free per-ticker RSS feed (no key).
2. **Strategy** — a pluggable class turns price history into BUY/SELL/HOLD signals.
   Three are included: a daily SMA-crossover (swing trading), an intraday
   opening-range ensemble (day trading), and a headline-sentiment strategy
   (news trading) — see below.
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
| `DAYTRADE_BAR_MINUTES` | `5` | Bar size for the day-trade strategy's intraday data (Alpaca) |
| `DAYTRADE_CONFIRM_BARS` | `3` | Backtest only (`--strategy orb`): require this many trailing 1-min closes to hold above the breakout level before buying — `0` disables it. Not used by the live bot, which always runs the 3-way ensemble — see below |
| `DAYTRADE_ENTRY_DELAY_MINUTES` | `25` | Day-trade bot only: minutes after the cron window opens (13:00 UTC) before the bot will open new positions — analysis/signals still run from the window's start, this only delays entries, never exits — `0` disables it |
| `DAYTRADE_STOP_LOSS_PCT` / `DAYTRADE_TAKE_PROFIT_PCT` | `0.01` / `0.02` | Day-trade bot only: force-exit a position if the latest bar's Low/High crosses this far from the actual average fill price — `0` disables either side independently |
| `NEWS_BUY_THRESHOLD` / `NEWS_SELL_THRESHOLD` | `0.35` / `-0.35` | News bot only: headline score (−1..+1) at or above which to buy / at or below which to sell — see "News trading" below |
| `NEWS_MIN_HEADLINES` | `3` | News bot only: headlines the lexicon could score before a symbol's score counts at all |
| `NEWS_HALF_LIFE_HOURS` / `NEWS_MAX_AGE_HOURS` | `12` / `48` | News bot only: a headline's weight halves every half-life; older than max-age is ignored |
| `NEWS_MAX_HOLD_DAYS` | `3` | News bot only: force-exit a news position after this many calendar days |
| `NEWS_STOP_LOSS_PCT` / `NEWS_TAKE_PROFIT_PCT` | `0.03` / `0.06` | News bot only: force-exit if the day's Low/High crosses this far from the real average fill — `0` disables either side |
| `NEWS_MAX_CHASE_PCT` / `NEWS_TREND_FILTER_DAYS` | `0.05` / `20` | News bot only: skip a buy if today's move is already past the chase limit, or price is below its N-day SMA — `0` disables either |
| `NEWS_UNIVERSE` | `discover` | News bot only: `discover` (scan many feeds, trade what's in the news), `watchlist`, or `both` — see "Market-wide discovery" |
| `NEWS_FEEDS` | built-in list | News bot only: feeds to scan — `name=url;…` replaces, `+name=url;-name` extends/removes |
| `NEWS_MIN_MENTIONS` / `NEWS_MAX_CANDIDATES` / `NEWS_SEC_TOP_N` | `2` / `40` / `1500` | News bot only: candidate selection — headlines naming a company, cap per cycle, depth of the SEC name list |
| `NEWS_MIN_MARKET_CAP` / `NEWS_MIN_DOLLAR_VOLUME` | `2e9` / `2e7` | News bot only: liquidity gate for discovered names before any buy (watchlist exempt) |
| `NEWS_ENTRY_DELAY_MINUTES` / `NEWS_MAX_ENTRIES_PER_CYCLE` | `30` / `3` | News bot only: no new entries in the first N minutes after the 09:30 ET open; at most N new positions per cycle — `0` disables either |
| `NEWS_RELEVANCE_FILTER` / `NEWS_COMPANY_NAMES` | `true` / — | News bot only: drop headlines that don't name the company; extra aliases as `AMD=Advanced Micro Devices\|AMD;TSM=TSMC` |
| `NEWS_PEER_DISCOUNT` | `0.5` | News bot only: sector read-through weight for symbols without enough headlines of their own — `0` disables |
| `NEWS_SCORER` / `NEWS_LLM_MODEL` / `ANTHROPIC_API_KEY` | `lexicon` / `claude-opus-5-5` / — | News bot only: `llm` scores headlines with Claude instead of the lexicon (falls back to the lexicon on any failure) |
| `EVENT_STUDY_ENABLED` / `EVENT_STUDY_GATE` | `true` / `false` | News bot only: log how similar news moved the stock before; with the gate on, an adverse history skips the buy |
| `EVENT_STUDY_MIN_SAMPLES` / `EVENT_STUDY_HORIZON_DAYS` / `EVENT_STUDY_USE_EDGAR` / `SEC_USER_AGENT` | `5` / `5` / `true` / repo URL | Event-study details — see "News trading" below |
| `EARNINGS_BLACKOUT_DAYS` | `2` | All bots: no new position in a symbol reporting within this many days — `0` disables |
| `REGIME_FILTER_ENABLED` / `REGIME_SMA_DAYS` / `REGIME_VIX_SCALE_ABOVE` / `REGIME_VIX_BLOCK_ABOVE` | `true` / `50` / `25` / `35` | All bots: no new entries with SPY below its SMA or VIX above the block level; sizes halved above the scale level |
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
combination from the Actions tab if you'd rather not run it locally — no
secrets needed for the swing backtest, `ALPACA_API_KEY`/`ALPACA_API_SECRET`
needed for `--daytrade`. Never touches the live account either way.

### 4. Use it

```bash
python main.py account               # sanity check: show cash + positions
python main.py backtest               # walk-forward backtest the swing strategy
python main.py backtest --daytrade    # walk-forward backtest the day-trade strategy
python main.py backtest --daytrade --confirm-bars 0     # ...and again with the confirmation gate off, to compare
python main.py backtest --windows 6 --slippage-bps 10   # tune the assumptions
python main.py backtest --news        # replay archived news scores vs prices (see "News trading")
python main.py run                    # run one swing-trading cycle (dry-run by default)
python main.py daytrade               # run one day-trading cycle (dry-run by default)
python main.py news                   # run one news-driven cycle (dry-run by default)
python main.py news-history --symbol AAPL   # how did similar news move this stock before?
python main.py report --days 7        # markdown summary of what the bots have been doing
python main.py backtest --regime-filter     # any backtest mode: apply the SPY-above-SMA regime mask
```

When you're happy with dry-run output, set `DRY_RUN=false` (still on `demo`) and
let it place practice orders. Only after that consider `T212_ENV=live`.

### 5. Schedule it

The bot is intentionally stateless — one invocation = one cycle. You have two options:

**Option A — GitHub Actions (recommended, no server needed):**

Three workflows are already set up:

- `.github/workflows/trading-bot.yml` — swing trading, every 15 minutes during
  US market hours (`python main.py run`).
- `.github/workflows/trading-bot-daytrade.yml` — day trading, every 5 minutes
  during a padded market-hours window (`python main.py daytrade`).
- `.github/workflows/trading-bot-news.yml` — news trading, every 30 minutes
  during market hours (`python main.py news`). Its dry-run switch is the
  separate `NEWS_DRY_RUN` secret; no extra API key is needed. Also accepts a
  `repository_dispatch` of type `external-cron-news` (e.g. from cron-job.org).

Two more workflows support them: `tests.yml` runs the offline test suite on
every push, and `smoke-test.yml` (manual) runs every cycle against the real
feeds with dry-run hard-wired on — see "Testing" below. `weekly-report.yml`
commits a summary every Sunday.

They're independent — enable any subset. To turn them on:

1. On GitHub, go to this repo → **Settings → Secrets and variables → Actions**.
2. Under **Secrets**, click **New repository secret** and add two secrets:
   - Name: `T212_API_KEY` — Value: your Trading212 API key
   - Name: `T212_API_SECRET` — Value: your Trading212 API secret
3. If you're enabling the day-trade workflow, also add:
   - Name: `ALPACA_API_KEY` — Value: your free Alpaca API key (data only, no
     funding needed — generate one at [app.alpaca.markets](https://app.alpaca.markets))
   - Name: `ALPACA_API_SECRET` — Value: the matching Alpaca API secret
4. (Optional) **Also under Secrets** (not Variables — see the note below), add
   any of `T212_ENV`, `DRY_RUN`, `DAYTRADE_DRY_RUN`, `NEWS_DRY_RUN`, `WATCHLIST`,
   `DAYTRADE_WATCHLIST`, `NEWS_WATCHLIST`, `DAYTRADE_BAR_MINUTES`,
   `DAYTRADE_STOP_LOSS_PCT`, `DAYTRADE_TAKE_PROFIT_PCT`, any `NEWS_*` knob,
   `MAX_POSITION_PCT`, `MAX_OPEN_POSITIONS`, `CASH_BUFFER_PCT` to override the
   defaults — see the env var table above. `DRY_RUN`, `DAYTRADE_DRY_RUN` and
   `NEWS_DRY_RUN` are separate on purpose, so you can arm one mode without
   arming the others. Leave everything unset to start safely in demo/dry-run
   mode.
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
*/30 13-20 * * 1-5  cd /path/to/t212 && .venv/bin/python main.py news      >> news.log 2>&1
```

## Day trading: the 3-way ensemble, and its limits

`python main.py daytrade` runs an ensemble of three structurally different
intraday strategies (`t212bot/strategy.py`) on `DAYTRADE_BAR_MINUTES`-sized
bars (5-minute by default) — **at least 2 of the 3 must independently signal
BUY, and `OpeningRangeConfluence` must always be one of them** (`EnsembleVote`,
`min_votes=2`, `required=[orb]`):

- **`OpeningRangeConfluence`** — after the first 30 minutes of the session
  (the "opening range"), signals BUY only if *all three* agree: price breaks
  above the opening-range high, the fast EMA (9) is above the slow EMA (21),
  and RSI(14) is in a bullish-but-not-overbought band (50-70). Exit is a hair
  trigger by comparison: *any one* of a breakdown below the opening-range low,
  the EMA trend flipping down, or RSI going overbought/oversold. The live bot
  runs this with its confirm-bars and EMA-spread entry gates both disabled —
  those are separate orb-only tuning (still available via
  `DAYTRADE_CONFIRM_BARS` for `backtest --daytrade --strategy orb`), not part
  of what was validated for these pairings.
- **`MeanReversionPullback`** — a genuinely different bet: buys pullbacks to
  the fast EMA within an established uptrend instead of chasing breakouts,
  gated on a minimum pullback depth, a minimum EMA spread (established
  trend), and recent prior RSI strength (a stateless proxy for "don't keep
  re-buying the same flat chop," since a fresh strategy instance is built
  every cycle with no persistent memory). Exits on a trend flip, a breakdown
  below the recent pullback low, or RSI going overbought/oversold.
- **`GapFillReversal`** — a third distinct bet, keyed off *overnight*
  information rather than anything computed purely from today's bars: buys a
  gap-down open that's reversing back up early in the session (today's open
  at least `min_gap_pct` below yesterday's close, price having reclaimed the
  open and moved back above the session's prior low, RSI recovering from
  oversold rather than still falling). Exits on a new session low or RSI
  going overbought/oversold, same hair-trigger philosophy as the others.

Any one strategy's SELL is enough to close a position — consensus is only
required to get in, not to get out. On top of that,
`DAYTRADE_STOP_LOSS_PCT`/`DAYTRADE_TAKE_PROFIT_PCT` (default `0.01`/`0.02`, 0
disables either side) force-close the position if the latest bar's Low/High
crosses that far from the real average fill price — checked every cycle, not
a resting broker order (see `run_day_trade_cycle` in `t212bot/bot.py`).

This is deliberately **one ensemble making one decision per symbol per
cycle**, not multiple bots running in parallel against the same account —
`OpeningRangeConfluence`'s breakout is the trigger common to two of the three
pairings below, so two independent bots sharing a watchlist could both buy
the same symbol in the same cycle, or one bot's SELL could close a position
the other bot thinks it still owns (position tracking reads real broker
state each cycle, not "which bot bought this"). A single `EnsembleVote`
avoids that entire conflict class by construction.

Beyond the strategy signal itself:

3. **Post-open warm-up** — the bot fetches data, evaluates signals, and logs
   decisions from the moment the cron window opens (13:00 UTC), but won't
   open any *new* position until `DAYTRADE_ENTRY_DELAY_MINUTES` (default 25)
   has passed since then — i.e. no entries before 13:25 UTC by default. Exits
   (signal SELL, stop-loss/take-profit, EOD flatten) are never delayed by
   this, only new entries. Anchored to a fixed UTC clock time tied to the
   cron window's own start, not to any bar's exchange-local session-open
   time — see `DAYTRADE_WINDOW_START_UTC` in `t212bot/bot.py`.
4. **End-of-day flatten** — regardless of what the strategy signals, any open
   position is force-closed once the exchange-local time is within 15 minutes
   of the close (`t212bot/bot.py: run_day_trade_cycle`). No position is ever
   held overnight — that's what makes this day trading rather than swing
   trading. New entries are also refused in the last 30 minutes, since there
   isn't enough session left to manage them.
5. **Stale-data guard** — if the latest available bar is more than 20 minutes
   old (pre-market, weekend, a holiday, or just data lag), new entries are
   skipped. Closing an existing position (signal exit, stop-loss/take-profit,
   or EOD flatten) still goes through even on stale data, since a market
   order fills at the live venue price regardless of the reference price the
   bot last saw.

**Why an ensemble, and the backtest evidence behind it.** Tuning
`OpeningRangeConfluence` alone (RSI band, EMA-spread gate, volume/strength
filters, retest-confirmation entries) converged to the same ~-0.1% to -0.2%
average-return ceiling and a ~30% win rate across a 60-day/30-symbol
walk-forward backtest, regardless of how the entry was filtered — a
per-filter sweet spot every time, never a breakthrough. Neither
`MeanReversionPullback` nor `GapFillReversal` broke through that ceiling
alone either (standalone `GapFillReversal`: ~37% win rate but still a
slightly negative average return). Pairing `OpeningRangeConfluence` with
either one did, though: **ORB+MeanReversion** hit a ~44-48% win rate and
**ORB+GapFillReversal** a ~45-48% win rate, both with a consistently positive
average return, at the `DAYTRADE_STOP_LOSS_PCT`/`DAYTRADE_TAKE_PROFIT_PCT`
defaults and across a range of stop-loss/take-profit widths — driven by
actually winning more often, not a better payoff ratio, a different
mechanism than every other improvement tried on `orb` alone. Both pairings
held up when the backtest windowing changed from 6 to 3 windows (win rate
and reward:risk stayed stable in each case), meaningful corroboration
neither is a windowing artifact. **MeanReversion+GapFillReversal**, by
contrast, is a near-dead combination (7-11 trades, ~11-18% win rate across
runs) — the two rarely agree at all, since one wants price dipping within an
uptrend and the other wants a gap-down actively reversing.

A plain 2-of-3 vote can't distinguish "ORB+MR" from "ORB+Gap" from "MR+Gap"
— it counts all three the same. Since the weak MR+Gap combination is also
"2 of 3," a plain vote lets it fire and blend its losses into the overall
average. `EnsembleVote`'s `required` parameter fixes that: requiring ORB to
be part of any winning combination lets both good pairings fire while
MR+Gap can never win on its own. Confirmed across two window counts (6 and
12): gating raised the blended win rate from ~44-45% to ~47-48% and
roughly doubled-to-tripled the blended avg pnl/trade versus the ungated
vote, at the cost of only the trades MR+Gap would have contributed (which
were net-negative anyway). This is what the live bot runs today. It's
still a modest backtest sample per pairing (tens, not hundreds, of trades)
versus standalone `orb` (~700+), so treat this as the most promising
candidate found on this data, not a proven edge. Reproduce with
`python main.py backtest --daytrade --strategy ensemble --ensemble-strategies orb,mr,gap --ensemble-min-votes 2 --ensemble-required orb`,
the ungated version by dropping `--ensemble-required`, individual pairings
with `--ensemble-strategies orb,mr` or `orb,gap`, and the standalone
strategies for comparison with `--strategy orb`, `mean_reversion`, or
`gap_fill`.

**Real constraints to know before trusting this with money:**

- Intraday data comes from Alpaca's free tier (real-time IEX feed, not
  delayed like Yahoo's free intraday data) — but it's still single-exchange
  IEX, not the full consolidated tape. `DAYTRADE_BAR_MINUTES` defaults to 5
  to match the bot's default 5-minute trigger cadence (GitHub
  Actions/cron-job.org); set it to `1` for finer-grained bars, but only if
  you also tighten the trigger cadence to match — otherwise you're just
  looking at a 1-minute-old bar every 5 minutes instead of a fresh 5-minute
  one, no real benefit. Either way, this isn't built for scalping or
  anything needing sub-minute execution.
- Backtesting the day-trade strategy (`--daytrade`) still pulls 5-minute
  bars regardless of `DAYTRADE_BAR_MINUTES`, to keep the backtest quick —
  see `t212bot/backtest.py`.
- GitHub Actions' cron scheduler fires at 5-minute granularity at best, and
  isn't guaranteed to hit the exact minute under load (a third-party
  scheduler hitting the repo's `repository_dispatch` API is a more reliable
  alternative — see the workflow files' comments).
- This ensemble is a reasonable, backtested-promising pattern — not a proven
  edge. Backtest and dry-run it extensively before considering
  `DAYTRADE_DRY_RUN=false`, and separately again before `T212_ENV=live`.
- Check your account's terms for any day-trading-specific restrictions before
  going live; this repo doesn't attempt to detect or enforce any such rules
  (e.g. the US Pattern Day Trader rule, which shouldn't apply to a Trading212
  account but wasn't something I could verify against your specific account).

## News trading: headline sentiment with price confirmation

`python main.py news` is the third strategy. Each cycle it works out which
companies are in the news (see "Market-wide discovery" below — or the fixed
watchlist with `NEWS_UNIVERSE=watchlist`), pulls each one's latest headlines
from **Yahoo Finance's free per-ticker RSS feed** (no API key, no account;
Google News RSS is the per-symbol fallback if Yahoo errors), **keeps only the
ones that actually name the company** (Yahoo
files sector round-ups and other companies' stories under every ticker —
`t212bot/companies.py` holds the name aliases, `NEWS_COMPANY_NAMES` adds more;
short tickers like `V`/`KO`/`CAT` are matched by name only, never as English
words), scores each headline with a finance-specific word/phrase
lexicon ("raises guidance" +1.0, "misses estimates" −0.9, "downgraded to sell"
−0.9, generic "rises"/"falls" ±0.3, with negation handling and listicle/"should
you buy?" filler down-weighted), and combines them into one recency-weighted
score per symbol between −1 and +1 (`t212bot/news.py`).

`NewsSentiment` (`t212bot/strategy.py`) then **buys** only when all of these
hold, and **sells** a position it opened as soon as the score turns clearly
negative:

- score ≥ `NEWS_BUY_THRESHOLD` on at least `NEWS_MIN_HEADLINES` scorable
  headlines, at least 60% of them positive — broad agreement, not one story;
- the news is *fresh*: yesterday's score for that symbol wasn't already
  buy-grade (a stock that's been "good news" for days is a trend, which the
  swing bot already trades — and this also stops re-buying the same story
  every day after an exit);
- the price confirms it: up on the day and above its 20-day SMA, but **not**
  up more than `NEWS_MAX_CHASE_PCT` already (if it's gapped 8% on the story,
  the move this is after has mostly happened).

Positions it opens are closed by whichever comes first: a `NEWS_STOP_LOSS_PCT`/
`NEWS_TAKE_PROFIT_PCT` touch on the day's Low/High, `NEWS_MAX_HOLD_DAYS`
calendar days, or the SELL signal. It only ever manages positions it opened
itself (`state/position_ownership.json`, owner `news`), same as the day-trade
bot. Everything else — sizing by signal strength and ATR, sector cap, daily
target/loss limit, drawdown pause, losing-streak cooldown — is the shared
machinery the other two bots use.

**What it is and isn't.** It's a poll of a free feed every 30 minutes, not a
news wire: anyone paying for low-latency news has traded the headline long
before this bot sees it, so the thesis is the slower few-day *drift* after
clearly good news with price agreeing, not the first seconds of the reaction.
The lexicon is deliberately crude and deterministic (same headline → same
score, every time, which a reproducible backtest needs); an LLM scorer could
replace `score_headline()` later without touching anything downstream.

### Market-wide discovery: trade what's in the news, not a fixed list

By default (`NEWS_UNIVERSE=discover`) the news bot doesn't start from the
watchlist at all. Each cycle it:

1. **Scans many general feeds** (`t212bot/discovery.py`): CNBC, MarketWatch,
   Yahoo Finance (index headlines), Seeking Alpha, Investing.com, the PR
   Newswire press-release wire, Google News business — plus targeted Google
   News searches for the event phrases the strategy trades ("raises
   guidance", "misses estimates", "FDA approval", "downgraded to sell", …)
   and for `NASDAQ:` / `NYSE:`, which sweeps up press releases from every
   wire Google indexes. All free RSS; a dead feed is logged and skipped;
   `NEWS_FEEDS` replaces or extends the list. (The first live scan: 23 of 27
   feeds answered, ~400 unique headlines in 48 h.)
2. **Resolves every headline to the companies it names**: explicit tickers
   first (`(NYSE: XYZ)`, `$XYZ` — press releases always carry them), bare
   upper-case tickers of 3+ letters (never on all-caps headlines, never
   everyday acronyms like CEO/FDA/ETF), then company names from **Trading212's
   own instrument list** (the tradable universe by definition), the SEC's
   public ticker list (top `NEWS_SEC_TOP_N` by size — needs `SEC_USER_AGENT`
   with a contact e-mail or the SEC answers 403) and the curated aliases.
   Matching is deliberately conservative:
   generic single words ("Target", "Gap", "General") never match, short
   first-token shortcuts aren't used, and a shared alias goes to the larger
   company. Missing a mention costs a trade; a false match costs a wrong one.
3. **Picks candidates**: names in at least `NEWS_MIN_MENTIONS` distinct
   headlines (an explicit ticker qualifies alone), ranked by mentions, at most
   `NEWS_MAX_CANDIDATES`. Candidates must be **tradable on Trading212**
   (checked against the account's instrument list, which also supplies the
   broker's ticker when it isn't the mechanical `SYMBOL_US_EQ`) and, before any buy, pass
   a **liquidity gate** (market cap ≥ `NEWS_MIN_MARKET_CAP`, average dollar
   volume ≥ `NEWS_MIN_DOLLAR_VOLUME`, via yfinance, cached a week in
   `state/universe.json` as yes/no). Watchlist names are exempt from the gate.
4. **Goes deep on each candidate** exactly as before: its own Yahoo feed is
   merged with the headlines that surfaced it, scored, archived, and run
   through every gate (price confirmation, freshness, regime, earnings
   blackout, event study).

Positions the bot holds are always included regardless of mode;
`NEWS_UNIVERSE=watchlist` restores the fixed list, `both` unions them. The
weekly report lists what was discovered and why anything was rejected, and
`backtest --news` replays every archived name by default.

### "How did similar news move this stock before?" — the event study

Every symbol with a news signal (and every position the news bot holds) also
gets an **event study** each cycle (`t212bot/events.py`): today's headlines
are classified into event categories (earnings beat/miss, guidance raise/cut,
upgrade/downgrade, exec change, M&A, legal, approval/contract, …), and for
each category every free historical analog source is replayed against the
stock's own 10-year price history:

| Source | Free archive | Used for |
|---|---|---|
| Earnings beats / misses | yfinance earnings history (report dates + EPS surprise %) | earnings and guidance headlines |
| 8-K filings by item code | SEC EDGAR submissions feed (2.02 results, 5.02 officer change, 2.01 acquisition, 2.05 layoffs, …) — keyless; identifies itself via `SEC_USER_AGENT` | exec changes, M&A, contracts, layoffs; direction-matched to today's move since item codes carry no sign |
| Price-reaction signature | the price history itself: past days with a same-sign ≥3% move on ≥1.5× volume | the fallback for categories with no archive (analyst actions, lawsuits, product news) |
| Own score archive | `state/news.json` — past days this symbol's score crossed the buy threshold | thin at first, the most exact "similar news" later |

For each analog it measures forward returns at +1/+3/+5/+10 trading days
from that day's close, **in excess of SPY** over the same window, and
reports n / median / hit rate / worst / best. The decision log then carries a
line like *"earnings beat: 11 prior analogs → median +1.6% vs SPY over 5d,
73% positive, worst −4.1%"*, and says `insufficient` below
`EVENT_STUDY_MIN_SAMPLES` analogs rather than guessing. It is informational
by default; `EVENT_STUDY_GATE=true` makes an `adverse` verdict (median ≤ 0 or
hit rate < 50% at `EVENT_STUDY_HORIZON_DAYS`) skip the buy. Run it by hand
for any symbol:

```bash
python main.py news-history --symbol AAPL
python main.py news-history --symbol BA --event-types exec_change,legal   # force the categories
```

### Sector read-through

A symbol without enough headlines of its own borrows its sector peers'
consensus (`t212bot/sectors.py` groups), weighted by their headline counts and
multiplied by `NEWS_PEER_DISCOUNT` (0.5): good news on NVDA is weaker
evidence for AVGO than for NVDA. It needs at least two peers that each clear
`NEWS_MIN_HEADLINES`, never overrides a symbol's own score, and is tagged in
the log and the archive (`src: peers:…`) so it can be told apart later. `0`
disables it.

### Optional LLM scoring

`NEWS_SCORER=llm` with an `ANTHROPIC_API_KEY` swaps the lexicon for Claude
(`t212bot/news_llm.py`, model `NEWS_LLM_MODEL`, default `claude-opus-5-5`;
`claude-haiku-5-5` is the cheap option). Same −1..+1 output, so nothing
downstream changes; each distinct headline is scored once and cached locally,
the request uses structured JSON output, and any failure (no SDK, auth, rate
limit, refusal, bad output) falls back to the lexicon for that batch with a
warning. The archive records which scorer produced each day, since mixing
them changes what a backtest is measuring.

**It has no backtest evidence yet, and here's why.** Free RSS feeds hold no
history — only the latest ~20 headlines per ticker — so there was nothing to
replay when it was written. Instead, every cycle archives that day's
per-symbol scores to `state/news.json` (scores, counts and dates only — no
prices or dollar figures, this repo is public), and `python main.py backtest
--news` replays that archive against daily prices through the same
walk-forward engine as the swing backtest, with the same stop/take-profit/
max-hold exits. So the intended sequence is: run the workflow **dry-run** for
a few weeks to build the archive, run `backtest --news`, adjust the `NEWS_*`
thresholds if the evidence says to, and only then consider `NEWS_DRY_RUN=false`
(still on `demo`). Until there are at least ~20 archived days the backtest
says so and should be read as a plumbing check, not a result. Every default
in the `NEWS_*` table above is a starting point, not a tuned value.

## Shared guards: earnings calendar and market regime

Two gates sit in front of **every** new entry in all three bots (exits are
never affected by either):

- **Earnings blackout** (`t212bot/events.py`, `EARNINGS_BLACKOUT_DAYS`, default
  2): no new position in a symbol that reports within that many days. The
  next report date per symbol comes from yfinance and is cached in
  `state/earnings_calendar.json` (dates and surprise percentages only),
  refreshed once a day. The news bot is exempt on the report day itself — the
  report *is* its news — but still blocked in the days before it.
- **Market regime** (`t212bot/regime.py`, `REGIME_*`): no new entries while
  SPY is below its 50-day SMA or VIX is above 35; sizes halved while VIX is
  above 25. All three strategies are long-only and none of the earlier
  backtests separated "the strategy worked" from "the market went up"; this is
  the simplest honest guard against that. If SPY/VIX can't be fetched the
  cycle logs a warning and runs as if the regime were benign. The same SPY
  rule is available historically as `python main.py backtest --regime-filter`
  (any mode) so the effect can be measured against the unfiltered run rather
  than assumed — it's off in backtests by default for exactly that comparison.

## Testing

Two layers, deliberately different:

- **Offline suite** (`tests/`, `python -m pytest`, ~5 s): every cycle runs end
  to end against faked broker, price feeds, news feed, regime and calendar —
  buys, the stop/max-hold/signal exits, every skip gate (blackout, regime,
  chase, stale news, event-study gate, same-day re-entry), the lexicon, RSS
  parsing with the Google fallback, the event study on planted events, the
  LLM scorer's cache and fallbacks, the report. No credentials, no network;
  `.github/workflows/tests.yml` runs it on every push, so a change that
  breaks a cycle fails there rather than at market open.
- **Dry-run smoke test** (`.github/workflows/smoke-test.yml`, **Run
  workflow**): the parts the suite fakes — runs `account`, all three cycles,
  `news-history` and `report` against the real Trading212 demo account,
  Yahoo RSS, yfinance and EDGAR, with `DRY_RUN` hard-wired to `true` (not
  read from any secret), every step running even if an earlier one failed,
  nothing committed. Run it after changing a feed or a cycle, and when a
  feed starts behaving differently.

```bash
pip install -r requirements-dev.txt
python -m pytest            # offline suite
python -m pytest -k cycles  # just the end-to-end cycle tests
```

## Weekly report

`python main.py report --days 7` prints a markdown summary from the state
files: trades per bot (period and all-time, hit rate, realized % of account),
open positions by owner, risk-state flags and pauses, news-archive growth and
the most positive/negative symbols, upcoming earnings, and — when run on the
machine that runs the bots, where `logs/history.jsonl` lives — cycles, errors,
buys/sells and the most frequent skip-reasons per bot. `--out reports/latest.md`
writes it too; `.github/workflows/weekly-report.yml` does that every Sunday
evening and on demand. Percentages, counts and dates only, so it's safe to
commit.

## Extending

- **New strategy:** subclass `Strategy` in `t212bot/strategy.py` and implement
  `generate_signals()`. Wire it up in `main.py`. A strategy that needs a
  non-price input (like `NewsSentiment`'s headline scores) gets it via its own
  setter called from its cycle in `t212bot/bot.py` — the `generate_signals()`
  signature stays price-only so the backtest engine can drive every strategy
  the same way.
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
