import { test, expect, type Page } from "@playwright/test";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();

type CacheRow = {
  symbol: string;
  state: string;
  hits: number;
  expired: number;
  schema_mismatch: number;
  refreshes: number;
  refresh_failures: number;
  lock_waits: number;
  stale_served: number;
  force_refreshes: number;
  last_source: string | null;
};

type StatusPayload = {
  config: {
    resolved_keys: Array<{
      key: string;
      value: string;
      source: string;
      defaulted: boolean;
    }>;
  };
  cache: { entries: CacheRow[] };
  watchlist: string[];
};

async function statusJson(page: Page): Promise<StatusPayload> {
  const response = await page.request.get("/api/status");
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as StatusPayload;
}

function rowFor(payload: StatusPayload, symbol: string): CacheRow {
  const row = payload.cache.entries.find((entry) => entry.symbol === symbol);
  if (!row) throw new Error("no cache row for " + symbol);
  return row;
}

test.describe("status page observability", () => {
  test("lists config values with env/toml/default source badges and yellow defaults", async ({ page }) => {
    await page.goto("/status");
    await expect(page.locator("#config-table")).toBeVisible();

    const envRow = page.locator('tr[data-key="upstream.provider"]');
    await expect(envRow.locator('[data-role="source-badge"]')).toHaveText("ENV");
    await expect(envRow).not.toHaveClass(/defaulted/);

    const ttlRow = page.locator('tr[data-key="cache.ttl_seconds"]');
    await expect(ttlRow.locator('[data-role="source-badge"]')).toHaveText("ENV");

    const lockRow = page.locator('tr[data-key="prefetch.lock_timeout"]');
    await expect(lockRow.locator('[data-role="source-badge"]')).toHaveText("TOML");

    // watchlist is neither in config.toml nor provided to this server -> default
    const defaultRow = page.locator('tr[data-key="watchlist"]');
    await expect(defaultRow).toHaveClass(/defaulted/);
    await expect(defaultRow.locator(".badge-default")).toBeVisible();
    await expect(defaultRow.locator('[data-role="value"]')).toHaveText("AAPL, MSFT, GOOG");
  });
});

test.describe("prefetch then query", () => {
  test("prefetch warms the cache and a follow-up query is a hit", async ({ page }) => {
    await page.goto("/status");

    await page.click("#btn-prefetch");
    await page.waitForFunction(
      () => document.querySelector("#cache-table tr[data-symbol='AAPL']") !== null
    );
    await page.waitForLoadState("networkidle");

    const before = await statusJson(page);
    const aaplBefore = rowFor(before, "AAPL");
    expect(aaplBefore.refreshes).toBe(1);
    expect(aaplBefore.last_source).toBe("upstream");

    const query = await page.request.get("/api/quotes/AAPL");
    expect(query.ok()).toBeTruthy();
    const body = await query.json();
    expect(body.cache.source).toBe("cache");
    expect(body.quote.symbol).toBe("AAPL");

    const after = await statusJson(page);
    const aaplAfter = rowFor(after, "AAPL");
    expect(aaplAfter.hits).toBe(aaplBefore.hits + 1);
    expect(aaplAfter.refreshes).toBe(1);
    expect(aaplAfter.state).toBe("fresh");

    // A repeat prefetch is incremental: fresh entries must be skipped.
    const repeat = await page.request.post("/prefetch", { data: {} });
    const summary = await repeat.json();
    expect(summary.skipped).toBeGreaterThanOrEqual(after.watchlist.length);
    expect(summary.updated).toBe(0);
  });
});

test.describe("explicit refresh penetrates upstream", () => {
  test("force refresh ignores TTL and records a new upstream pull", async ({ page }) => {
    await page.goto("/status");
    await page.click("#btn-prefetch");
    await page.waitForFunction(
      () =>
        document.querySelector("#cache-table tr[data-symbol='MSFT'] [data-role='refreshes']")
          ?.textContent === "1"
    );

    const before = await statusJson(page);
    const msftBefore = rowFor(before, "MSFT");
    expect(msftBefore.state).toBe("fresh");

    const forced = await page.request.get("/api/quotes/MSFT?refresh=true");
    expect(forced.ok()).toBeTruthy();
    const forcedBody = await forced.json();
    expect(forcedBody.cache.source).toBe("upstream");
    expect(forcedBody.cache.ttl_seconds).toBe(60);

    await page.goto("/status");
    const after = await statusJson(page);
    const msftAfter = rowFor(after, "MSFT");
    expect(msftAfter.force_refreshes).toBeGreaterThanOrEqual(1);
    expect(msftAfter.refreshes).toBe(msftBefore.refreshes + 1);
  });
});

test.describe("prefetch failure keeps stale cache", () => {
  test("upstream failure on a stale entry serves and preserves the old quote", async ({ page }) => {
    // FLAKY always fails upstream; seed an existing stale entry at the
    // current schema so the failure path can prove it is not overwritten.
    fs.mkdirSync(path.join(ROOT, "cache_test_e2e"), { recursive: true });
    const entry = {
      schema: 1,
      symbol: "FLAKY",
      fetched_at: Date.now() / 1000 - 3600,
      quote: { symbol: "FLAKY", price: 123.45, currency: "USD" },
    };
    fs.writeFileSync(
      path.join(ROOT, "cache_test_e2e", "FLAKY.json"),
      JSON.stringify(entry),
      "utf-8"
    );

    const prefetched = await page.request.post("/prefetch", {
      data: { symbols: ["FLAKY"] },
    });
    const summary = await prefetched.json();
    expect(summary.stale).toBe(1);
    expect(summary.failed).toBe(0);

    const served = await page.request.get("/api/quotes/FLAKY");
    const body = await served.json();
    expect(body.cache.source).toBe("stale");
    expect(body.quote.price).toBe(123.45);

    const payload = await statusJson(page);
    const flaky = rowFor(payload, "FLAKY");
    expect(flaky.stale_served).toBeGreaterThanOrEqual(1);
    expect(flaky.refresh_failures).toBeGreaterThanOrEqual(1);
    expect(flaky.price).toBe(123.45);
  });
});

test.describe("invalid configuration fails startup", () => {
  test("uvicorn exits non-zero naming the bad key and expected type", async () => {
    const result = await new Promise<{ code: number | null; output: string }>(
      (resolve, reject) => {
        const child = spawn(
          "python",
          ["-m", "uvicorn", "service.main:app", "--host", "127.0.0.1", "--port", "8399"],
          {
            cwd: ROOT,
            env: {
              ...process.env,
              STOCK_UPSTREAM_PROVIDER: "fake",
              STOCK_CACHE_DIR: "cache_test_badconfig",
              STOCK_CACHE_TTL_SECONDS: "banana",
            },
          }
        );
        let output = "";
        child.stdout.on("data", (chunk) => (output += chunk.toString()));
        child.stderr.on("data", (chunk) => (output += chunk.toString()));
        const timer = setTimeout(() => {
          child.kill();
          reject(new Error("bad-config server unexpectedly stayed alive"));
        }, 20_000);
        child.on("exit", (code) => {
          clearTimeout(timer);
          resolve({ code, output });
        });
        child.on("error", reject);
      }
    );

    expect(result.code).not.toBe(0);
    expect(result.output).toMatch(/cache\.ttl_seconds/);
    expect(result.output).toMatch(/positive number/);
    expect(result.output).toMatch(/banana/);
  });
});
