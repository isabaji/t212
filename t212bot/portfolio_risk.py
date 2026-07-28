"""Account-wide drawdown throttle, shared across both bots.

Unlike daily_target.py (resets every UTC midnight), this tracks a long-horizon
high-water mark of *realized* cumulative return and never resets on its own —
it's meant to catch a losing stretch that plays out over days or weeks, not
one bad day. Once the drawdown from the high-water mark reaches
MAX_PORTFOLIO_DRAWDOWN_PCT, both bots stop opening new positions (existing
positions still get managed and closed normally) until the drawdown recovers
to less than half that threshold — the hysteresis avoids flapping open/closed
every cycle right at the boundary.

Same public-repo constraint as daily_target.py/trade_stats.py: state
(state/portfolio_risk.json) tracks only a compounded running percentage,
never a dollar figure. Persists across separate, ephemeral GitHub Actions
runs the same way (the workflow commits state/ back after each cycle).
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "portfolio_risk.json"


def load() -> dict:
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text())
            state.setdefault("cumulative_return_pct", 0.0)
            state.setdefault("peak_return_pct", 0.0)
            state.setdefault("paused", False)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"cumulative_return_pct": 0.0, "peak_return_pct": 0.0, "paused": False}


def record_trade_pct(state: dict, pnl_fraction_of_account: float) -> None:
    """Compound pnl_fraction_of_account into the running return index and update the peak."""
    state["cumulative_return_pct"] = (
        (1 + state["cumulative_return_pct"]) * (1 + pnl_fraction_of_account) - 1
    )
    if state["cumulative_return_pct"] > state["peak_return_pct"]:
        state["peak_return_pct"] = state["cumulative_return_pct"]


def evaluate(state: dict, max_drawdown_pct: float) -> None:
    drawdown = state["peak_return_pct"] - state["cumulative_return_pct"]
    if not state["paused"] and drawdown >= max_drawdown_pct:
        state["paused"] = True
        log.info("Portfolio drawdown reached %.2f%% from peak — pausing new entries account-wide "
                  "until it recovers", drawdown * 100)
    elif state["paused"] and drawdown < max_drawdown_pct / 2:
        state["paused"] = False
        log.info("Portfolio drawdown recovered to %.2f%% from peak — resuming new entries", drawdown * 100)


def entries_blocked(state: dict) -> bool:
    return state["paused"]


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
