"""Upstream quote providers.

A provider exposes ``fetch_quote(symbol, timeout)`` returning a dict with
trade fields. The default provider wraps yfinance; ``fake`` is a deterministic
in-process provider used by the acceptance tests so runs never depend on the
network. ``FLAKY`` always fails upstream so stale-cache semantics can be
exercised deterministically.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("stock_service.upstream")


class UpstreamError(Exception):
    """Upstream could not serve a quote."""


class BaseProvider:
    name = "base"

    async def fetch_quote(self, symbol: str, timeout: float) -> dict:
        raise NotImplementedError


class YFinanceProvider(BaseProvider):
    name = "yfinance"

    async def fetch_quote(self, symbol: str, timeout: float) -> dict:
        def _blocking() -> dict:
            import yfinance as yf  # imported lazily so tests stay fast

            ticker = yf.Ticker(symbol)
            fast = ticker.fast_info
            price = getattr(fast, "last_price", None)
            if price is None:
                price = fast.get("lastPrice") if hasattr(fast, "get") else None
            if price is None:
                raise UpstreamError(f"no price returned for {symbol}")

            previous = getattr(fast, "previous_close", None)
            if previous is None and hasattr(fast, "get"):
                previous = fast.get("previousClose")
            currency = getattr(fast, "currency", "USD")
            change = (price - previous) if previous else 0.0
            change_pct = (change / previous * 100.0) if previous else 0.0
            return {
                "symbol": symbol.upper(),
                "price": round(float(price), 4),
                "previous_close": round(float(previous), 4) if previous else None,
                "change": round(float(change), 4),
                "change_percent": round(float(change_pct), 4),
                "currency": currency or "USD",
            }

        return await asyncio.wait_for(asyncio.to_thread(_blocking), timeout=timeout)


class FakeProvider(BaseProvider):
    """Deterministic provider: price drifts with wall-clock seconds.

    ``FLAKY`` always fails. The generated price changes over time and changes
    per symbol, so tests can distinguish fresh upstream pulls from cache hits.
    """

    name = "fake"

    async def fetch_quote(self, symbol: str, timeout: float) -> dict:
        await asyncio.sleep(0)
        normalized = symbol.strip().upper()
        if not normalized:
            raise UpstreamError("empty symbol")
        if normalized == "FLAKY":
            raise UpstreamError("simulated upstream failure for FLAKY")
        await asyncio.sleep(0.01)

        symbol_sum = sum(ord(char) for char in normalized)
        bucket = int(time.time())
        price = round(100.0 + (symbol_sum % 50) + (bucket % 7) * 0.37, 2)
        previous = round(price - ((symbol_sum % 5) - 2) * 0.25, 2)
        change = round(price - previous, 2)
        change_pct = round(change / previous * 100.0, 4) if previous else 0.0
        return {
            "symbol": normalized,
            "price": price,
            "previous_close": previous,
            "change": change,
            "change_percent": change_pct,
            "currency": "USD",
        }


def build_provider(name: str) -> BaseProvider:
    if name == "fake":
        return FakeProvider()
    if name == "yfinance":
        return YFinanceProvider()
    raise UpstreamError(f"unknown upstream provider: {name}")
