"""Position sizing and portfolio-level risk limits."""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class RiskManager:
    max_position_pct: float = 0.10   # hard ceiling: max fraction of account value per position
    max_open_positions: int = 5
    cash_buffer_pct: float = 0.05    # fraction of account value kept in cash
    risk_per_trade_pct: float = 0.01  # fraction of account risked per trade, via the stop distance below
    atr_multiple: float = 2.0         # stop distance = atr_multiple * ATR

    def can_open_position(self, open_positions: int) -> bool:
        return open_positions < self.max_open_positions

    def size_buy(self, account_value: float, free_cash: float, price: float,
                 atr_value: float | None = None) -> float:
        """Return the quantity to buy (fractional shares allowed), or 0 if not affordable.

        Volatility-aware: sized so a move of atr_multiple * ATR against the
        position loses about risk_per_trade_pct of account value — a wide-ATR
        (volatile) stock gets a smaller position than a narrow-ATR (calm) one
        for the same dollar risk. max_position_pct and available cash are
        still hard ceilings regardless of what the volatility formula wants.
        Falls back to the flat max_position_pct cap if no ATR is available
        (e.g. not enough price history yet).
        """
        if price <= 0:
            return 0.0

        cash_cap_shares = (free_cash - account_value * self.cash_buffer_pct) / price
        position_cap_shares = (account_value * self.max_position_pct) / price

        if atr_value and atr_value > 0:
            stop_distance = self.atr_multiple * atr_value
            risk_dollars = account_value * self.risk_per_trade_pct
            vol_cap_shares = risk_dollars / stop_distance
        else:
            vol_cap_shares = position_cap_shares

        qty = min(vol_cap_shares, position_cap_shares, cash_cap_shares)
        if qty <= 0:
            log.info("No budget for new position (free cash %.2f, buffer/volatility caps applied)",
                      free_cash)
            return 0.0
        # Round down to 2 decimals; Trading212 supports fractional shares on most equities.
        return int(qty * 100) / 100
