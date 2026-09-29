from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import time
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cache.store import CacheStore
from config import Config, ConfigError, load_config

logger = logging.getLogger("stock_dash.service")


class MockUpstream:
    """Deterministic offline upstream. Price changes on every call so that
    TTL-penetrating refreshes are observable."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def fetch(self, symbol: str) -> dict[str, Any]:
        await asyncio.sleep(0.01)
        self.calls[symbol] = self.calls.get(symbol, 0) + 1
        n = self.calls[symbol]
        base = int(hashlib.sha256(symbol.encode()).hexdigest()[:6], 16) % 900 + 100
        return {
            "symbol": symbol,
            "price": round(base * (1 + n / 1000), 2),
            "upstream_seq": n,
            "source": "mock",
        }

    def stats(self) -> dict[str, Any]:
        return {"kind": "mock", "calls": dict(self.calls),
                "total_calls": sum(self.calls.values())}


class YFinanceUpstream:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.calls: dict[str, int] = {}

    async def fetch(self, symbol: str) -> dict[str, Any]:
        import yfinance as yf

        def _load() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            return {
                "symbol": symbol,
                "price": round(float(info["lastPrice"]), 2),
                "currency": info.get("currency"),
                "source": "yfinance",
            }

        self.calls[symbol] = self.calls.get(symbol, 0) + 1
        try:
            return await asyncio.wait_for(asyncio.to_thread(_load), timeout=self.timeout)
        except Exception:
            raise

    def stats(self) -> dict[str, Any]:
        return {"kind": "yfinance", "calls": dict(self.calls),
                "total_calls": sum(self.calls.values())}


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _render_status(cfg: Config, store: CacheStore, upstream) -> str:
    config_rows = []
    for entry in cfg.table():
        value = html.escape(repr(entry.value))
        badge_cls = "badge-default" if entry.is_default else f"badge-{entry.source}"
        warn = ' <span class="warn" title="missing, fell back to built-in default">&#9888; fallback</span>' if entry.is_default else ''
        config_rows.append(
            f'<tr data-key="{entry.key}">'
            f"<td><code>{entry.key}</code></td>"
            f"<td><code>{value}</code></td>"
            f"<td>{entry.expected_type}</td>"
            f'<td><span class="badge {badge_cls}" data-source="{entry.source}">{entry.source}</span>{warn}</td>'
            "</tr>"
        )
    cache_rows = []
    for row in store.snapshot():
        state = "fresh" if row["fresh"] else ("expired" if row["present"] else "absent")
        cache_rows.append(
            f'<tr data-key="{html.escape(row["key"])}">'
            f"<td><code>{html.escape(row['key'])}</code></td>"
            f'<td><span class="state state-{state}">{state}</span></td>'
            f"<td>{row['hits']}</td>"
            f"<td>{row['expired']}</td>"
            f"<td>{_fmt_ts(row['last_refresh'])}</td>"
            f"<td>{row['lock_waits']}</td>"
            f"<td>{row['refresh_failures']}</td>"
            f"<td>{html.escape(str(row['last_error'] or '-'))}</td>"
            "</tr>"
        )
    if not cache_rows:
        cache_rows.append('<tr><td colspan="8" class="empty">cache is empty</td></tr>')
    up = upstream.stats()
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>stock-dash /status</title>
<style>
body {{ font-family: ui-monospace, Consolas, monospace; margin: 2rem; background: #0f1420; color: #dbe2f0; }}
h1 {{ font-size: 1.3rem; }}
h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #2a3550; padding: 6px 10px; text-align: left; font-size: 0.9rem; }}
th {{ background: #1a2236; }}
.badge {{ padding: 1px 8px; border-radius: 8px; font-size: 0.8rem; }}
.badge-env {{ background: #14532d; color: #86efac; }}
.badge-toml {{ background: #1e3a8a; color: #93c5fd; }}
.badge-default {{ background: #eab308; color: #1a1a1a; font-weight: 700; }}
.warn {{ color: #eab308; font-size: 0.8rem; margin-left: 6px; }}
.state-fresh {{ color: #86efac; }}
.state-expired {{ color: #fca5a5; }}
.state-absent {{ color: #94a3b8; }}
button {{ margin-right: 0.8rem; padding: 8px 18px; font-size: 0.95rem; border: 0; border-radius: 6px; cursor: pointer; }}
#btn-refresh {{ background: #dc2626; color: #fff; }}
#btn-prefetch {{ background: #2563eb; color: #fff; }}
#action-result {{ margin-top: 0.8rem; font-size: 0.85rem; color: #93c5fd; white-space: pre-wrap; }}
.empty {{ color: #64748b; }}
.meta {{ color: #64748b; font-size: 0.8rem; }}
</style>
</head>
<body>
<h1>stock-dash 可观测性 <span class="meta">upstream={up['kind']} total_upstream_calls={up['total_calls']}</span></h1>
<div>
  <button id="btn-refresh" type="button">强制刷新</button>
  <button id="btn-prefetch" type="button">触发预取</button>
</div>
<div id="action-result"></div>
<h2>配置（env &gt; config.toml &gt; default）</h2>
<table id="config-table">
<thead><tr><th>key</th><th>effective value</th><th>type</th><th>source</th></tr></thead>
<tbody>{''.join(config_rows)}</tbody>
</table>
<h2>缓存条目</h2>
<table id="cache-table">
<thead><tr><th>key</th><th>state</th><th>hits</th><th>expired</th><th>last refresh</th><th>lock waits</th><th>refresh failures</th><th>last error</th></tr></thead>
<tbody>{''.join(cache_rows)}</tbody>
</table>
<script>
async function act(url) {{
  const out = document.getElementById('action-result');
  out.textContent = 'running ' + url + ' ...';
  try {{
    const resp = await fetch(url, {{method: 'POST'}});
    const body = await resp.json();
    out.textContent = resp.status + ' ' + JSON.stringify(body);
    window.location.reload();
  }} catch (err) {{
    out.textContent = 'request failed: ' + err;
  }}
}}
document.getElementById('btn-refresh').addEventListener('click', () => act('/refresh'));
document.getElementById('btn-prefetch').addEventListener('click', () => act('/prefetch'));
</script>
</body>
</html>"""


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else load_config()
    logging.basicConfig(level=getattr(logging, str(cfg.get("log_level")).upper(), logging.INFO))
    store = CacheStore(cfg.get("cache_dir"), ttl=cfg.get("cache_ttl_seconds"))
    upstream_kind = str(cfg.get("upstream")).lower()
    if upstream_kind == "yfinance":
        upstream = YFinanceUpstream(timeout=cfg.get("upstream_timeout_seconds"))
    elif upstream_kind == "mock":
        upstream = MockUpstream()
    else:
        raise ConfigError(
            f"invalid value for config key 'upstream': expected one of "
            f"'mock' | 'yfinance', got {cfg.get('upstream')!r}"
        )

    app = FastAPI(title="stock-dash")
    app.state.config = cfg
    app.state.store = store
    app.state.upstream = upstream

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/status")

    @app.get("/status", response_class=HTMLResponse)
    async def status_page() -> str:
        return _render_status(cfg, store, upstream)

    @app.get("/api/status")
    async def status_json() -> dict[str, Any]:
        return {
            "config": [
                {"key": e.key, "value": e.value, "source": e.source,
                 "expected_type": e.expected_type, "is_default": e.is_default}
                for e in cfg.table()
            ],
            "cache": store.snapshot(),
            "upstream": upstream.stats(),
        }

    @app.get("/quote/{symbol}")
    async def quote(symbol: str) -> JSONResponse:
        symbol = symbol.upper()
        try:
            result = await store.get(symbol, upstream.fetch)
        except Exception as exc:
            return JSONResponse(status_code=502,
                                content={"error": f"upstream fetch failed: {exc}"})
        return JSONResponse({"symbol": symbol, "cache": result.source,
                             "data": result.value})

    async def _run(symbols: list[str], force: bool) -> dict[str, Any]:
        async def one(sym: str) -> tuple[str, str]:
            try:
                result = await store.get(sym, upstream.fetch, force=force)
                return sym, result.source
            except Exception as exc:
                logger.warning("fetch failed for %s: %s", sym, exc)
                return sym, f"error: {exc}"

        pairs = await asyncio.gather(*(one(s) for s in symbols))
        return {
            "fetched": sorted(s for s, r in pairs if r == "fetched"),
            "cached": sorted(s for s, r in pairs if r == "hit"),
            "stale_fallback": sorted(s for s, r in pairs if r == "stale_fallback"),
            "failed": sorted(s for s, r in pairs if r.startswith("error")),
            "upstream_total_calls": upstream.stats()["total_calls"],
        }

    @app.post("/prefetch")
    async def prefetch() -> dict[str, Any]:
        # Incremental: only missing/expired keys go upstream; shares the
        # same store and singleflight locks as /quote.
        symbols = [s.upper() for s in cfg.get("prefetch_symbols")]
        return await _run(symbols, force=False)

    @app.post("/refresh")
    async def refresh(symbols: str | None = Query(default=None)) -> dict[str, Any]:
        # Explicit refresh penetrates to the upstream even inside the TTL.
        if symbols:
            wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        else:
            wanted = [s.upper() for s in cfg.get("prefetch_symbols")]
        return await _run(wanted, force=True)

    return app


app = create_app()

