"""Per-symbol trade outcome tracking and a losing-streak circuit breaker.

Long-lived (unlike daily_target.py, this never resets) — a running record of
win/loss counts and consecutive-loss streaks per symbol per strategy. Counts
and percentages only, same reasoning as daily_target.py: this file is
git-tracked in a public repo, so it must never contain a dollar figure.

This is rule-based, not machine learning: a symbol that loses
LOSING_STREAK_LIMIT times in a row is paused (no new entries) for
LOSING_STREAK_COOLDOWN_DAYS, then automatically resumes with its streak
reset. It's an explainable circuit breaker, not the strategy quietly
rewriting its own logic — deliberately, since adapting trading rules from a
handful of outcomes is an easy way to overfit to noise rather than learn a
real edge.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "trade_stats.json"


def load() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _symbol_entry(state: dict, strategy: str, symbol: str) -> dict:
    bucket = state.setdefault(strategy, {})
    return bucket.setdefault(symbol, {
        "wins": 0, "losses": 0, "total_pnl_pct": 0.0,
        "streak": 0, "streak_type": None,
        "paused_until": None,
    })


def record_trade(state: dict, strategy: str, symbol: str, pnl_pct: float,
                  streak_limit: int, cooldown_days: int) -> None:
    entry = _symbol_entry(state, strategy, symbol)
    entry["total_pnl_pct"] += pnl_pct
    is_win = pnl_pct > 0
    if is_win:
        entry["wins"] += 1
    else:
        entry["losses"] += 1

    if entry["streak_type"] == ("win" if is_win else "loss"):
        entry["streak"] += 1
    else:
        entry["streak"] = 1
        entry["streak_type"] = "win" if is_win else "loss"

    if entry["streak_type"] == "loss" and entry["streak"] >= streak_limit:
        resume = datetime.now(timezone.utc) + timedelta(days=cooldown_days)
        entry["paused_until"] = resume.strftime("%Y-%m-%d")
        log.info("%s (%s): %d losses in a row — pausing new entries until %s",
                  symbol, strategy, entry["streak"], entry["paused_until"])


def is_paused(state: dict, strategy: str, symbol: str) -> bool:
    entry = state.get(strategy, {}).get(symbol)
    if not entry or not entry.get("paused_until"):
        return False
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today >= entry["paused_until"]:
        entry["paused_until"] = None
        entry["streak"] = 0
        entry["streak_type"] = None
        log.info("%s (%s): cooldown ended, resuming new entries", symbol, strategy)
        return False
    return True


def summary(state: dict) -> list:
    """Flat rows for the dashboard: one per symbol per strategy with a track record."""
    rows = []
    for strategy, symbols in state.items():
        for symbol, e in symbols.items():
            total = e["wins"] + e["losses"]
            if total == 0:
                continue
            rows.append({
                "strategy": strategy,
                "symbol": symbol,
                "wins": e["wins"],
                "losses": e["losses"],
                "win_rate": e["wins"] / total,
                "streak": e["streak"],
                "streak_type": e["streak_type"],
                "paused_until": e["paused_until"],
            })
    return rows
