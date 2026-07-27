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
   A simple SMA-crossover example is included.
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
| `WATCHLIST` | `AAPL,MSFT,GOOGL` | Yahoo symbols the strategy scans |

The bot refuses to run against `live` unless you also set
`I_UNDERSTAND_LIVE_TRADING_RISK=yes`.

### 4. Use it

```bash
python main.py account     # sanity check: show cash + positions
python main.py backtest    # backtest the SMA strategy on the watchlist
python main.py run         # run one strategy cycle (dry-run by default)
```

When you're happy with dry-run output, set `DRY_RUN=false` (still on `demo`) and
let it place practice orders. Only after that consider `T212_ENV=live`.

### 5. Schedule it

The bot is intentionally stateless — one invocation = one cycle. You have two options:

**Option A — GitHub Actions (recommended, no server needed):**

A workflow at `.github/workflows/trading-bot.yml` is already set up to run the bot
every 15 minutes during US market hours. To turn it on:

1. On GitHub, go to this repo → **Settings → Secrets and variables → Actions**.
2. Under **Secrets**, click **New repository secret** and add two secrets:
   - Name: `T212_API_KEY` — Value: your Trading212 API key
   - Name: `T212_API_SECRET` — Value: your Trading212 API secret
3. (Optional) Under **Variables**, add any of `T212_ENV`, `DRY_RUN`,
   `WATCHLIST`, `MAX_POSITION_PCT`, `MAX_OPEN_POSITIONS`, `CASH_BUFFER_PCT` to
   override the defaults (`demo`, `true`, `AAPL,MSFT,GOOGL`, `0.10`, `5`, `0.05`).
   Leave them unset to start safely in demo/dry-run mode.
4. Go to the **Actions** tab → **Trading212 Bot** → **Run workflow** to trigger
   it manually and check the logs, or just wait for the schedule.

No terminal or local Python install required — GitHub runs it for you.

**Option B — your own machine/server with cron:**

```cron
*/15 14-21 * * 1-5  cd /path/to/t212 && .venv/bin/python main.py run >> bot.log 2>&1
```

or a GitHub Actions scheduled workflow, or any server/Raspberry Pi.

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
