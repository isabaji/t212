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
┌─────────────┐    ┌──────────────┐    ┌──────────────────┐
│  Market data │ →  │   Strategy   │ →  │  Execution        │
│  (yfinance)  │    │  (signals)   │    │  (Trading212 API) │
└─────────────┘    └──────────────┘    └──────────────────┘
                          ↑
                   Risk manager (position sizing, limits)
```

1. **Data** — historical/delayed prices are pulled from Yahoo Finance (`yfinance`).
2. **Strategy** — a pluggable class turns price history into BUY/SELL/HOLD signals.
   Two are included: a daily SMA-crossover (swing trading) and an intraday
   opening-range breakout (day trading) — see below.
3. **Risk** — position sizing, max open positions, and a cash buffer are enforced
   before any order is sent.
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
| `WATCHLIST` | `AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,JPM,V,UNH,HD,XOM,JNJ,PG,DIS` | Yahoo symbols the strategy scans (15 large-caps across sectors by default) |

The bot refuses to run against `live` unless you also set
`I_UNDERSTAND_LIVE_TRADING_RISK=yes`.

### 4. Use it

```bash
python main.py account     # sanity check: show cash + positions
python main.py backtest    # backtest the SMA strategy on the watchlist
python main.py run         # run one swing-trading cycle (dry-run by default)
python main.py daytrade    # run one day-trading cycle (dry-run by default)
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
3. (Optional) Under **Variables**, add any of `T212_ENV`, `DRY_RUN`,
   `DAYTRADE_DRY_RUN`, `WATCHLIST`, `DAYTRADE_WATCHLIST`, `MAX_POSITION_PCT`,
   `MAX_OPEN_POSITIONS`, `CASH_BUFFER_PCT` to override the defaults (`demo`,
   `true`, `true`, 15 large-caps across sectors — see the table above, same
   list for `WATCHLIST`, `0.10`, `5`, `0.05`).
   `DRY_RUN` and `DAYTRADE_DRY_RUN` are separate on purpose, so you can arm one
   mode without arming the other. Leave everything unset to start safely in
   demo/dry-run mode.
4. Go to the **Actions** tab → pick a workflow → **Run workflow** to trigger
   it manually and check the logs, or just wait for the schedule.

No terminal or local Python install required — GitHub runs it for you.

**Option B — your own machine/server with cron:**

```cron
*/15 14-21 * * 1-5  cd /path/to/t212 && .venv/bin/python main.py run       >> swing.log 2>&1
*/5  13-20 * * 1-5  cd /path/to/t212 && .venv/bin/python main.py daytrade  >> daytrade.log 2>&1
```

## Day trading: what "OpeningRangeConfluence" actually does, and its limits

`python main.py daytrade` runs an intraday strategy (`OpeningRangeConfluence` in
`t212bot/strategy.py`) on 5-minute bars:

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

- Yahoo Finance's free intraday data is typically delayed ~15 minutes. This
  strategy reacts to delayed prices, not live ticks — it's not built for
  scalping or anything sub-minute.
- GitHub Actions' cron scheduler fires at 5-minute granularity at best, and
  isn't guaranteed to hit the exact minute under load.
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
