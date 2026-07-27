"""Position sizing and portfolio-level risk limits."""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RiskManager:
    max_position_pct: float = 0.10   # max fraction of account value per position
    max_open_positions: int = 5
    cash_buffer_pct: float = 0.05    # fraction of account value kept in cash

    def can_open_position(self, open_positions: int) -> bool:
        return open_positions < self.max_open_positions

    def size_buy(self, account_value: float, free_cash: float, price: float) -> float:
        """Return the quantity to buy (fractional shares allowed), or 0 if not affordable."""
        if price <= 0:
            return 0.0
        budget = min(
            account_value * self.max_position_pct,
            free_cash - account_value * self.cash_buffer_pct,
        )
        if budget <= 0:
            log.info("No budget for new position (free cash %.2f, buffer applies)", free_cash)
            return 0.0
        # Round down to 2 decimals; Trading212 supports fractional shares on most equities.
        qty = int(budget / price * 100) / 100
        return max(qty, 0.0)
