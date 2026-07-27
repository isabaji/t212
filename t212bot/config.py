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
    env: str = field(default_factory=lambda: os.getenv("T212_ENV", "demo").strip().lower())
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", "true"))
    live_ack: bool = field(default_factory=lambda: _bool("I_UNDERSTAND_LIVE_TRADING_RISK", "no"))
    watchlist: list[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in os.getenv("WATCHLIST", "AAPL,MSFT,GOOGL").split(",")
            if s.strip()
        ]
    )
    max_position_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "0.10")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "5")))
    cash_buffer_pct: float = field(default_factory=lambda: float(os.getenv("CASH_BUFFER_PCT", "0.05")))

    @property
    def base_url(self) -> str:
        return f"https://{'live' if self.env == 'live' else 'demo'}.trading212.com"

    def validate(self) -> None:
        if not self.api_key:
            raise SystemExit("T212_API_KEY is not set. Copy .env.example to .env and fill it in.")
        if self.env not in ("demo", "live"):
            raise SystemExit(f"T212_ENV must be 'demo' or 'live', got {self.env!r}")
        if self.env == "live" and not self.live_ack:
            raise SystemExit(
                "Refusing to run against a LIVE account. "
                "Set I_UNDERSTAND_LIVE_TRADING_RISK=yes to enable live trading."
            )
