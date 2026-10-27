"""Configuration loaded from environment variables (.env supported)."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    api_key: str = field(default_factory=lambda: os.getenv("T212_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("T212_API_SECRET", ""))
    # Market data for the day-trade cycle's intraday bars (t212bot/data.py).
    # Free Alpaca account, data-only -- no funding needed. Swing doesn't use
    # these (it still runs on yfinance daily bars), so they're not required
    # by validate() below; a missing key only fails when daytrade actually
    # fetches intraday data.
    alpaca_api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    alpaca_api_secret: str = field(default_factory=lambda: os.getenv("ALPACA_API_SECRET", ""))
    daytrade_bar_minutes: int = field(default_factory=lambda: int(os.getenv("DAYTRADE_BAR_MINUTES", "5")))
    # Backtest-only default (`backtest --daytrade --strategy orb`): 1-minute
    # confirmation bars required before entry (t212bot/strategy.py:
    # OpeningRangeConfluence's confirm_bars). The live day-trade bot always
    # runs the 3-way ORB+MeanReversion+GapFillReversal ensemble (2 of 3 must
    # agree, and ORB must be one of them), whose ORB component is built with
    # confirm_bars=0 regardless of this setting (see main.py).
    daytrade_confirm_bars: int = field(default_factory=lambda: int(os.getenv("DAYTRADE_CONFIRM_BARS", "3")))
    # Minutes after the day-trade cron window's start (13:00 UTC -- see
    # t212bot/bot.py: DAYTRADE_WINDOW_START_UTC) before the bot will open any
    # new position. The bot still fetches data, evaluates signals, and logs
    # decisions from the window's start -- this only delays *entries*, not
    # analysis; exits (signal SELL, stop-loss/take-profit, EOD flatten) are
    # never delayed by this. 0 disables the gate (entries allowed as soon as
    # the window starts). Default 25 matches "analyse from 13:00 UTC, only
    # place orders from 13:25 UTC" (2pm/2:25pm BST during UK summer time).
    daytrade_entry_delay_minutes: int = field(
        default_factory=lambda: int(os.getenv("DAYTRADE_ENTRY_DELAY_MINUTES", "25"))
    )
    # Fixed stop-loss / take-profit checked each cycle against the latest
    # bar's Low/High, exiting via a market order regardless of the strategy's
    # own signal (t212bot/bot.py: run_day_trade_cycle). 0 disables either
    # side independently. Backtested: 1%/2% cut worst-case single-window
    # drawdown roughly in half (-7.8% -> -4.7%) with no meaningful return cost.
    daytrade_stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("DAYTRADE_STOP_LOSS_PCT", "0.01")))
    daytrade_take_profit_pct: float = field(
        default_factory=lambda: float(os.getenv("DAYTRADE_TAKE_PROFIT_PCT", "0.02"))
    )
    # News-driven strategy knobs (t212bot/news.py + NewsSentiment in
    # t212bot/strategy.py, run by `python main.py news`). All untuned
    # starting points -- there was no headline history to backtest against
    # when they were set; `backtest --news` replays the archive the bot
    # builds while paper-trading. Thresholds are on the -1..1 lexicon score.
    news_buy_threshold: float = field(default_factory=lambda: float(os.getenv("NEWS_BUY_THRESHOLD", "0.35")))
    news_sell_threshold: float = field(default_factory=lambda: float(os.getenv("NEWS_SELL_THRESHOLD", "-0.35")))
    # Headlines the lexicon could score needed before a score counts at all.
    news_min_headlines: int = field(default_factory=lambda: int(os.getenv("NEWS_MIN_HEADLINES", "3")))
    # Recency weighting: a headline's weight halves every this many hours;
    # anything older than max_age is ignored outright.
    news_half_life_hours: float = field(default_factory=lambda: float(os.getenv("NEWS_HALF_LIFE_HOURS", "12")))
    news_max_age_hours: float = field(default_factory=lambda: float(os.getenv("NEWS_MAX_AGE_HOURS", "48")))
    # Exit rules for positions the news bot opened (bot.py: run_news_cycle):
    # force-close after this many calendar days regardless of signal (the
    # drift this trades decays within days), or when the day's Low/High
    # crosses the stop/take-profit from the real average fill. 0 disables
    # the stop/TP sides independently.
    news_max_hold_days: int = field(default_factory=lambda: int(os.getenv("NEWS_MAX_HOLD_DAYS", "3")))
    news_stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("NEWS_STOP_LOSS_PCT", "0.03")))
    news_take_profit_pct: float = field(default_factory=lambda: float(os.getenv("NEWS_TAKE_PROFIT_PCT", "0.06")))
    # Don't buy if today's move is already past this (the drift is priced
    # in), and only buy above the trend_filter-day SMA. 0 disables either.
    news_max_chase_pct: float = field(default_factory=lambda: float(os.getenv("NEWS_MAX_CHASE_PCT", "0.05")))
    news_trend_filter_days: int = field(default_factory=lambda: int(os.getenv("NEWS_TREND_FILTER_DAYS", "20")))
    # Sector read-through (t212bot/news.py: peer_scores): a symbol with too
    # few headlines of its own borrows its sector peers' consensus times
    # this discount. 0 disables.
    news_peer_discount: float = field(default_factory=lambda: float(os.getenv("NEWS_PEER_DISCOUNT", "0.5")))
    # "lexicon" (default, free, deterministic) or "llm" (t212bot/news_llm.py;
    # needs ANTHROPIC_API_KEY, falls back to the lexicon on any failure).
    news_scorer: str = field(default_factory=lambda: os.getenv("NEWS_SCORER", "lexicon").strip().lower())
    news_llm_model: str = field(default_factory=lambda: os.getenv("NEWS_LLM_MODEL", "claude-opus-5-5").strip())
    # Event study (t212bot/events.py): "how did similar news move this stock
    # before?" -- logged for every news signal; only gates entries when
    # EVENT_STUDY_GATE=true (skip a BUY whose best-matching history shows
    # median excess return <= 0 or hit rate < 50% at EVENT_STUDY_HORIZON_DAYS,
    # given at least EVENT_STUDY_MIN_SAMPLES analogs).
    event_study_enabled: bool = field(default_factory=lambda: _bool("EVENT_STUDY_ENABLED", "true"))
    event_study_gate: bool = field(default_factory=lambda: _bool("EVENT_STUDY_GATE", "false"))
    event_study_min_samples: int = field(default_factory=lambda: int(os.getenv("EVENT_STUDY_MIN_SAMPLES", "5")))
    event_study_horizon_days: int = field(default_factory=lambda: int(os.getenv("EVENT_STUDY_HORIZON_DAYS", "5")))
    event_study_use_edgar: bool = field(default_factory=lambda: _bool("EVENT_STUDY_USE_EDGAR", "true"))
    # Earnings-calendar awareness (all three bots): don't open a new
    # position when the symbol reports within this many days. The news bot
    # is exempt on the report day itself (the report is its news). 0 disables.
    earnings_blackout_days: int = field(default_factory=lambda: int(os.getenv("EARNINGS_BLACKOUT_DAYS", "2")))
    # Market-regime filter (t212bot/regime.py, all three bots): no new
    # entries while SPY is below its SMA or VIX is above the block level;
    # sizes halved above the scale level. `backtest --regime-filter`
    # applies the same SPY mask historically.
    regime_filter_enabled: bool = field(default_factory=lambda: _bool("REGIME_FILTER_ENABLED", "true"))
    regime_sma_days: int = field(default_factory=lambda: int(os.getenv("REGIME_SMA_DAYS", "50")))
    regime_vix_scale_above: float = field(default_factory=lambda: float(os.getenv("REGIME_VIX_SCALE_ABOVE", "25")))
    regime_vix_block_above: float = field(default_factory=lambda: float(os.getenv("REGIME_VIX_BLOCK_ABOVE", "35")))
    env: str = field(default_factory=lambda: os.getenv("T212_ENV", "demo").strip().lower())
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", "true"))
    live_ack: bool = field(default_factory=lambda: _bool("I_UNDERSTAND_LIVE_TRADING_RISK", "no"))
    watchlist: list[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in os.getenv(
                "WATCHLIST",
                "AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,JPM,V,UNH,HD,XOM,JNJ,PG,DIS,"
                "AVGO,CRM,NFLX,NKE,MCD,BAC,MA,PFE,ABBV,CVX,KO,WMT,BA,CAT,NEE",
            ).split(",")
            if s.strip()
        ]
    )
    max_position_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "0.35")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "10")))
    cash_buffer_pct: float = field(default_factory=lambda: float(os.getenv("CASH_BUFFER_PCT", "0.05")))
    daily_profit_target_pct: float = field(
        default_factory=lambda: float(os.getenv("DAILY_PROFIT_TARGET_PCT", "0.0075"))
    )
    daily_loss_limit_pct: float = field(
        default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.01"))
    )
    losing_streak_limit: int = field(default_factory=lambda: int(os.getenv("LOSING_STREAK_LIMIT", "3")))
    losing_streak_cooldown_days: int = field(
        default_factory=lambda: int(os.getenv("LOSING_STREAK_COOLDOWN_DAYS", "5"))
    )
    risk_per_trade_pct: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE_PCT", "0.01")))
    atr_multiple: float = field(default_factory=lambda: float(os.getenv("ATR_MULTIPLE", "2.0")))
    atr_period: int = field(default_factory=lambda: int(os.getenv("ATR_PERIOD", "14")))
    max_sector_exposure_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_SECTOR_EXPOSURE_PCT", "0.35"))
    )
    max_portfolio_drawdown_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_PORTFOLIO_DRAWDOWN_PCT", "0.15"))
    )

    @property
    def base_url(self) -> str:
        return f"https://{'live' if self.env == 'live' else 'demo'}.trading212.com"

    def validate(self) -> None:
        if not self.api_key:
            raise SystemExit("T212_API_KEY is not set. Copy .env.example to .env and fill it in.")
        if not self.api_secret:
            raise SystemExit("T212_API_SECRET is not set. Copy .env.example to .env and fill it in.")
        if self.env not in ("demo", "live"):
            raise SystemExit(f"T212_ENV must be 'demo' or 'live', got {self.env!r}")
        if self.env == "live" and not self.live_ack:
            raise SystemExit(
                "Refusing to run against a LIVE account. "
                "Set I_UNDERSTAND_LIVE_TRADING_RISK=yes to enable live trading."
            )
