"""Timestamped ledger of realized trade P&L, feeding the dashboard's
performance breakdown (trailing 1H/1D/1W/1M/1Y realized return).

Same public-repo constraint as the other state files: each entry is a
percentage of account value at the time of the trade, timestamped, never a
dollar figure. Persists in state/pnl_history.json across ephemeral GitHub
Actions runs, same mechanism as daily_target.py/trade_stats.py/portfolio_risk.py.
Capped at the most recent MAX_ENTRIES trades so the file doesn't grow
unbounded — a few thousand trades is far more than either bot's cadence
produces in a season of use.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "pnl_history.json"
MAX_ENTRIES = 2000


def load() -> list:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def record_trade(history: list, strategy: str, symbol: str, pnl_pct: float) -> None:
    history.append({
        "t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "strategy": strategy,
        "symbol": symbol,
        "pnl_pct": pnl_pct,
    })
    del history[:-MAX_ENTRIES]


def save(history: list) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(history, indent=2) + "\n")
