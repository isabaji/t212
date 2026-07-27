"""Structured per-cycle history, appended locally as JSON Lines.

This is the bridge between local execution (cron on your own machine) and the
published dashboard: each cycle appends one record to logs/history.jsonl, and
that file gets git-pushed (see scripts/run_and_sync.sh) so the dashboard's
scheduled refresh can read it instead of GitHub Actions run logs.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_PATH = Path(__file__).resolve().parent.parent / "logs" / "history.jsonl"


def decision(symbol: str | None, status: str, headline: str, detail: str) -> dict:
    """status is one of: good (buy), serious (sell), warning (skipped), neutral (hold)."""
    return {"symbol": symbol, "status": status, "headline": headline, "detail": detail}


def append(bot: str, env: str, dry_run: bool, account: dict | None,
           decisions: list[dict], error: str | None = None) -> None:
    record = {
        "t": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bot": bot,
        "env": env,
        "dry_run": dry_run,
        "account": account,
        "decisions": decisions,
        "error": error,
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a") as f:
        f.write(json.dumps(record) + "\n")
    log.debug("Logged cycle to %s", HISTORY_PATH)
