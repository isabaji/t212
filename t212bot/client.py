"""Minimal rate-limit-aware client for the Trading212 Public API (v0).

Docs: https://t212public-api-docs.redoc.ly/
Sell orders use a negative quantity — that is the API's convention.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

MAX_RETRIES = 5


class Trading212Error(RuntimeError):
    pass


class Trading212Client:
    def __init__(self, api_key: str, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": api_key})

    def _request(self, method: str, path: str, json: dict | None = None) -> dict | list:
        url = f"{self.base_url}/api/v0{path}"
        for attempt in range(MAX_RETRIES):
            resp = self.session.request(method, url, json=json, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                log.warning("Rate limited on %s %s, retrying in %ss", method, path, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                raise Trading212Error("Unauthorized: check T212_API_KEY and its scopes.")
            if not resp.ok:
                raise Trading212Error(f"{method} {path} failed ({resp.status_code}): {resp.text}")
            return resp.json() if resp.text else {}
        raise Trading212Error(f"{method} {path}: still rate limited after {MAX_RETRIES} retries")

    # --- Account ---
    def account_cash(self) -> dict:
        """Free/total/invested cash. Keys include 'free' and 'total'."""
        return self._request("GET", "/equity/account/cash")

    def account_info(self) -> dict:
        return self._request("GET", "/equity/account/info")

    # --- Portfolio ---
    def portfolio(self) -> list:
        """All open positions. Each has 'ticker', 'quantity', 'averagePrice', 'currentPrice'."""
        return self._request("GET", "/equity/portfolio")

    def position(self, ticker: str) -> dict:
        return self._request("GET", f"/equity/portfolio/{ticker}")

    # --- Instruments ---
    def instruments(self) -> list:
        """All tradeable instruments (large response; cache it)."""
        return self._request("GET", "/equity/metadata/instruments")

    # --- Orders ---
    def pending_orders(self) -> list:
        return self._request("GET", "/equity/orders")

    def place_market_order(self, ticker: str, quantity: float) -> dict:
        """Positive quantity = buy, negative = sell."""
        return self._request("POST", "/equity/orders/market", {"ticker": ticker, "quantity": quantity})

    def place_limit_order(self, ticker: str, quantity: float, limit_price: float,
                          time_validity: str = "DAY") -> dict:
        return self._request("POST", "/equity/orders/limit", {
            "ticker": ticker,
            "quantity": quantity,
            "limitPrice": limit_price,
            "timeValidity": time_validity,
        })

    def place_stop_order(self, ticker: str, quantity: float, stop_price: float,
                         time_validity: str = "DAY") -> dict:
        return self._request("POST", "/equity/orders/stop", {
            "ticker": ticker,
            "quantity": quantity,
            "stopPrice": stop_price,
            "timeValidity": time_validity,
        })

    def cancel_order(self, order_id: int) -> dict:
        return self._request("DELETE", f"/equity/orders/{order_id}")

    def order_history(self, ticker: str | None = None, limit: int = 20) -> dict:
        path = f"/equity/history/orders?limit={limit}"
        if ticker:
            path += f"&ticker={ticker}"
        return self._request("GET", path)
