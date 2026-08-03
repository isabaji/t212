"""Which bot opened each currently-held position, shared across both bots.

Both the swing and day-trade cycles trade against the same Trading212
account, and (unless DAYTRADE_WATCHLIST is set to a disjoint symbol list)
the same watchlist. Without this tracking, each cycle's exit logic (signal
reversal, EOD flatten, fixed stop-loss/take-profit) applies to *any*
position it finds in the account, including ones the other bot just opened
on a completely different signal — a swing SMA-crossover entry could get
whipsawed out within minutes by the day-trade bot's much faster intraday
exits, and vice versa.

This records, per T212 ticker, which strategy ("swing" or "daytrade") owns
the currently-open position. Each cycle's exit logic (see bot.py) only
manages positions it owns; a position owned by the other strategy, or with
no record at all (pre-migration position, manual trade), is left alone by
the day-trade bot specifically, since its exits are the fast/aggressive
ones -- the slower swing bot's own signal-based exit already tolerates
holding through noise, so treating unknown positions as swing-manageable
by default is the safer failure mode than leaving them unmanaged forever.

Percentages and tickers only, no dollar figures -- same reasoning as the
other state/*.json files in this package, since this repo is public.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "position_ownership.json"


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


def set_owner(state: dict, ticker: str, strategy: str) -> None:
    state[ticker] = strategy


def clear_owner(state: dict, ticker: str) -> None:
    state.pop(ticker, None)


def owned_by(state: dict, ticker: str, strategy: str) -> bool:
    """True if `ticker` is recorded as opened by `strategy`.

    An untracked ticker (no record at all) is treated as owned by "swing"
    and not by "daytrade" -- see the module docstring for why.
    """
    owner = state.get(ticker)
    if owner is None:
        return strategy == "swing"
    return owner == strategy
