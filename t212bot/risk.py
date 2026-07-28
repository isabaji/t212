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
    max_sector_exposure_pct: float = 0.25  # hard ceiling: max fraction of account value in one sector

    def can_open_position(self, open_positions: int) -> bool:
        return open_positions < self.max_open_positions

    def sector_capacity_shares(self, account_value: float, price: float, sector_value_held: float) -> float:
        """Max additional shares purchasable before max_sector_exposure_pct of account value
        would be held in this symbol's sector, counting positions already open in it."""
        if price <= 0:
            return 0.0
        remaining = account_value * self.max_sector_exposure_pct - sector_value_held
        return max(remaining, 0.0) / price

    def size_buy(self, account_value: float, free_cash: float, price: float,
                 atr_value: float | None = None, sector_value_held: float = 0.0,
                 strength: float = 1.0) -> float:
        """Return the quantity to buy (fractional shares allowed), or 0 if not affordable.

        Volatility-aware: sized so a move of atr_multiple * ATR against the
        position loses about risk_per_trade_pct of account value — a wide-ATR
        (volatile) stock gets a smaller position than a narrow-ATR (calm) one
        for the same dollar risk. max_position_pct and available cash are
        still hard ceilings regardless of what the volatility formula wants.
        Falls back to the flat max_position_pct cap if no ATR is available
        (e.g. not enough price history yet).

        Signal-strength-aware: strength (0..1, from the strategy's
        SignalResult) scales the max_position_pct ceiling itself, so a
        strong setup is sized toward the full max_position_pct and a weak
        one gets a proportionally smaller slice of it. Defaults to 1.0 (the
        full cap) for callers that don't have a strength score.

        Portfolio-level: sector_value_held (current $ already held in this
        symbol's sector, across open positions) further caps sizing so one
        correlated group of names — sector_of() in t212bot/sectors.py — can't
        eat the whole account even if each individual buy looks affordable.
        """
        if price <= 0:
            return 0.0

        strength = max(0.0, min(1.0, strength))
        cash_cap_shares = (free_cash - account_value * self.cash_buffer_pct) / price
        position_cap_shares = (account_value * self.max_position_pct * strength) / price
        sector_cap_shares = self.sector_capacity_shares(account_value, price, sector_value_held)

        if atr_value and atr_value > 0:
            stop_distance = self.atr_multiple * atr_value
            risk_dollars = account_value * self.risk_per_trade_pct
            vol_cap_shares = risk_dollars / stop_distance
        else:
            vol_cap_shares = position_cap_shares

        qty = min(vol_cap_shares, position_cap_shares, cash_cap_shares, sector_cap_shares)
        if qty <= 0:
            log.info("No budget for new position (free cash %.2f, buffer/volatility/sector caps applied)",
                      free_cash)
            return 0.0
        # Round down to 2 decimals; Trading212 supports fractional shares on most equities.
        return int(qty * 100) / 100
