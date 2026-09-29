"""FastAPI entrypoint: quotes, prefetch scheduling and the /status page.

Run with::

    uvicorn service.main:app --port 8000

Configuration is resolved once at import/startup; invalid configuration makes
the process fail fast with the offending key and expected type.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ConfigError, load_settings  # noqa: E402
from cache.store import LockTimeoutError, validate_symbol  # noqa: E402
from upstream.provider import UpstreamError, build_provider  # noqa: E402

logging.basicConfig(
    level=os.environ.get("STOCK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stock_service")


def _build_state():
    """Resolve config and build the shared provider/cache; fail fast."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("startup aborted: %s", exc)
        raise SystemExit(f"configuration error: {exc}") from exc

    provider = build_provider(settings.get("upstream.provider"))
    from cache.store import QuoteStore

    store = QuoteStore(
        cache_dir=settings.get("cache.dir"),
        ttl_seconds=settings.get("cache.ttl_seconds"),
        provider=provider,
        lock_timeout=settings.get("prefetch.lock_timeout"),
        request_timeout=settings.get("upstream.request_timeout"),
    )
    for warning in settings.warnings:
        logger.warning(warning)
    logger.info(
        "service ready: provider=%s ttl=%ss cache_dir=%s watchlist=%s",
        settings.get("upstream.provider"),
        settings.get("cache.ttl_seconds"),
        settings.get("cache.dir"),
        ",".join(settings.get("watchlist")),
    )
    return settings, store


SETTINGS, STORE = _build_state()

app = FastAPI(title="AI Stock Dashboard - Quote Service")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _error(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    payload = {"error": code, "message": message}
    if details:
        payload["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(ValueError)
async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    return _error(400, "bad_symbol", str(exc))


@app.exception_handler(UpstreamError)
async def _upstream_error_handler(_request: Request, exc: UpstreamError) -> JSONResponse:
    return _error(502, "upstream_error", str(exc))


@app.exception_handler(LockTimeoutError)
async def _lock_timeout_handler(_request: Request, exc: LockTimeoutError) -> JSONResponse:
    return _error(503, "lock_timeout", str(exc))


@app.get("/api/quotes/{symbol}")
async def get_quote(symbol: str, refresh: bool = False):
    """Read-through query; ``?refresh=true`` forces an upstream pull."""
    symbol = validate_symbol(symbol)
    quote, info = await STORE.get(symbol, force=refresh)
    return {"symbol": symbol, "quote": quote, "cache": info}


async def _run_jobs(symbols: list[str], force: bool) -> dict:
    import asyncio

    async def _job(symbol: str) -> dict:
        try:
            quote, info = await STORE.get(symbol, force=force)
            return {
                "symbol": symbol,
                "status": "updated" if info["source"] == "upstream" else (
                    "stale" if info["source"] == "stale" else "skipped"
                ),
                "source": info["source"],
                "price": quote.get("price"),
                "age_seconds": info["age_seconds"],
            }
        except UpstreamError as exc:
            return {"symbol": symbol, "status": "failed", "error": str(exc)}

    results = await asyncio.gather(*(_job(symbol) for symbol in symbols))
    updated = [row for row in results if row["status"] == "updated"]
    skipped = [row for row in results if row["status"] == "skipped"]
    failed = [row for row in results if row["status"] == "failed"]
    stale = [row for row in results if row["status"] == "stale"]
    return {
        "updated": len(updated),
        "skipped": len(skipped),
        "failed": len(failed),
        "stale": len(stale),
        "results": results,
    }


def _normalize_symbols(payload_symbols) -> list[str]:
    if payload_symbols is None:
        symbols = SETTINGS.get("watchlist")
    elif isinstance(payload_symbols, str):
        symbols = [part.strip() for part in payload_symbols.split(",") if part.strip()]
    elif isinstance(payload_symbols, list):
        symbols = [str(part).strip() for part in payload_symbols if str(part).strip()]
    else:
        raise HTTPException(status_code=400, detail="symbols must be a list or string")
    return [validate_symbol(symbol) for symbol in symbols]


@app.post("/prefetch")
async def prefetch(request: Request):
    """Incremental prefetch: fresh entries are skipped, others are pulled.

    Shares the same cache and per-key locks as GET /api/quotes, so a
    concurrent query is single-flight merged with a prefetch.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    symbols = _normalize_symbols(body.get("symbols"))
    summary = await _run_jobs(symbols, force=False)
    summary["mode"] = "prefetch"
    summary["requested"] = symbols
    return summary


@app.post("/refresh")
async def force_refresh(request: Request):
    """Explicit refresh: bypass TTL and pull straight from upstream."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    symbols = _normalize_symbols(body.get("symbols"))
    summary = await _run_jobs(symbols, force=True)
    summary["mode"] = "force-refresh"
    summary["requested"] = symbols
    return summary


@app.get("/api/status")
async def api_status():
    cache_snapshot = STORE.snapshot()
    return {
        "config": {
            "path": str(SETTINGS.config_path),
            "warnings": SETTINGS.warnings,
            "resolved_keys": [
                {
                    "key": item.key,
                    "value": SETTINGS.display(item.key),
                    "source": item.source,
                    "defaulted": item.defaulted,
                    "expected": item.expected,
                    "description": item.description,
                }
                for item in (SETTINGS.resolved[key] for key in sorted(SETTINGS.resolved))
            ],
        },
        "cache": cache_snapshot,
        "watchlist": SETTINGS.get("watchlist"),
    }


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    import datetime as _dt

    payload = await api_status()

    def fmt_time(epoch):
        if epoch is None:
            return "—"
        return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")

    return TEMPLATES.TemplateResponse(
        request=request,
        name="status.html",
        context={
            "payload": payload,
            "fmt_time": fmt_time,
            "watchlist": payload["watchlist"],
            "provider": SETTINGS.get("upstream.provider"),
        },
    )


@app.get("/")
async def root():
    return {"service": "stock-quote", "status": "/status", "prefetch": "POST /prefetch"}



