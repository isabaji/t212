"""Daily profit-target / loss-limit tracking, shared across both bots.

This does NOT and cannot guarantee any particular daily return — it's a
stop mechanism: once realized gains for the day reach the target, new
entries pause and existing positions just get managed normally; symmetrically,
if losses reach the limit, new entries also pause so a bad day doesn't
compound. Hitting the target is never guaranteed; this only stops the bot
from pushing past a boundary once it does happen.

Tracks cumulative *realized percentage* gain/loss from closed trades today —
never a dollar figure. This state file is git-tracked and this repo is
public, so it must never contain the account's actual value. Persists in
state/daily_target.json; the workflow commits it back after each cycle so it
survives between separate, ephemeral GitHub Actions runs.

Known approximation: for the swing bot specifically, a single sale can
realize gains accrued over many prior days (positions aren't always closed
same-day), so a big close can look like a big "today" in this tracker.
Treated as acceptable — pausing new entries after a large realized gain is a
reasonable protective behavior even when it technically accrued earlier.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "daily_target.json"


def load() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = None
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            state = None
    if not state or state.get("date") != today:
        if state:
            log.info("New trading day — daily target/loss-limit tracking reset")
        state = {"date": today, "cumulative_pct": 0.0, "target_hit": False, "loss_limit_hit": False}
    return state


def record_trade_pct(state: dict, pnl_fraction_of_account: float) -> None:
    state["cumulative_pct"] += pnl_fraction_of_account


def evaluate(state: dict, profit_target_pct: float, loss_limit_pct: float) -> None:
    if not state["target_hit"] and state["cumulative_pct"] >= profit_target_pct:
        state["target_hit"] = True
        log.info("Daily profit target reached (%.2f%% realized) — no new entries for the rest of today",
                  state["cumulative_pct"] * 100)
    if not state["loss_limit_hit"] and state["cumulative_pct"] <= -loss_limit_pct:
        state["loss_limit_hit"] = True
        log.info("Daily loss limit reached (%.2f%% realized) — no new entries for the rest of today",
                  state["cumulative_pct"] * 100)


def entries_blocked(state: dict) -> bool:
    return state["target_hit"] or state["loss_limit_hit"]


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
