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
    daytrade_bar_minutes: int = field(default_factory=lambda: int(os.getenv("DAYTRADE_BAR_MINUTES", "1")))
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
