import { test, expect, request as pwRequest } from '@playwright/test';
import { spawn, ChildProcess } from 'node:child_process';
import * as net from 'node:net';
import * as path from 'node:path';
import * as fs from 'node:fs';

const ROOT = path.resolve(__dirname, '..');

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const port = (srv.address() as net.AddressInfo).port;
      srv.close(() => resolve(port));
    });
  });
}

async function waitForServer(port: number, timeoutMs = 30_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`http://127.0.0.1:${port}/api/status`);
      if (resp.ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`server on port ${port} did not become ready`);
}

interface RunningServer {
  port: number;
  baseURL: string;
  cacheDir: string;
  proc: ChildProcess;
  output: () => string;
}

async function startServer(extraEnv: NodeJS.ProcessEnv = {}): Promise<RunningServer> {
  const port = await freePort();
  const cacheDir = path.join(ROOT, `.cache-pw-${port}`);
  fs.rmSync(cacheDir, { recursive: true, force: true });
  let buf = '';
  const proc = spawn(
    process.env.PYTHON || 'python',
    ['-m', 'uvicorn', 'service.main:app', '--port', String(port)],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        STOCK_DASH_CACHE_DIR: cacheDir,
        STOCK_DASH_UPSTREAM: 'mock',
        ...extraEnv,
      },
    },
  );
  proc.stdout?.on('data', (d) => (buf += d.toString()));
  proc.stderr?.on('data', (d) => (buf += d.toString()));
  await waitForServer(port);
  return {
    port,
    baseURL: `http://127.0.0.1:${port}`,
    cacheDir,
    proc,
    output: () => buf,
  };
}

async function stopServer(server: RunningServer): Promise<void> {
  server.proc.kill();
  await new Promise((r) => setTimeout(r, 300));
  fs.rmSync(server.cacheDir, { recursive: true, force: true });
}

async function apiStatus(baseURL: string) {
  const ctx = await pwRequest.newContext();
  const resp = await ctx.get(`${baseURL}/api/status`);
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  await ctx.dispose();
  return body;
}

test.describe('status page', () => {
  let server: RunningServer;

  test.beforeAll(async () => {
    server = await startServer({ STOCK_DASH_CACHE_TTL_SECONDS: '300' });
  });

  test.afterAll(async () => {
    await stopServer(server);
  });

  test('预取后命中: prefetch populates cache, quote hits without new upstream calls', async ({ page }) => {
    await page.goto(`${server.baseURL}/status`);

    // config table: env override badge and yellow default badge
    const ttlRow = page.locator('#config-table tr[data-key="cache_ttl_seconds"]');
    await expect(ttlRow.locator('.badge')).toHaveText('env');
    const dirRow = page.locator('#config-table tr[data-key="log_level"]');
    await expect(dirRow.locator('.badge')).toHaveText('default');
    await expect(dirRow.locator('.badge')).toHaveClass(/badge-default/);

    // click 触发预取 -> POST /prefetch -> page reloads with cache rows
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/prefetch') && r.request().method() === 'POST'),
      page.getByRole('button', { name: '触发预取' }).click(),
    ]);
    await page.waitForResponse((r) => r.url().endsWith('/status') && r.request().method() === 'GET');

    let status = await apiStatus(server.baseURL);
    expect(status.upstream.total_calls).toBe(3);
    expect(status.cache.map((r: any) => r.key).sort()).toEqual(['AAPL', 'GOOG', 'MSFT']);
    for (const row of status.cache) {
      expect(row.fresh).toBe(true);
      expect(row.last_refresh).not.toBeNull();
    }

    // second prefetch is incremental: no new upstream calls
    const ctx = await pwRequest.newContext();
    const again = await ctx.post(`${server.baseURL}/prefetch`);
    const againBody = await again.json();
    expect(againBody.fetched).toEqual([]);
    expect(againBody.cached.sort()).toEqual(['AAPL', 'GOOG', 'MSFT']);
    expect(againBody.upstream_total_calls).toBe(3);

    // queries now hit the cache
    const quote = await ctx.get(`${server.baseURL}/quote/AAPL`);
    expect((await quote.json()).cache).toBe('hit');
    await ctx.dispose();

    status = await apiStatus(server.baseURL);
    expect(status.upstream.total_calls).toBe(3);
    const aapl = status.cache.find((r: any) => r.key === 'AAPL');
    expect(aapl.hits).toBeGreaterThanOrEqual(2); // prefetch re-check + /quote
  });

  test('刷新穿透: forced refresh penetrates to upstream inside TTL', async ({ page }) => {
    const before = await apiStatus(server.baseURL);
    const callsBefore = before.upstream.total_calls;
    const aaplBefore = before.cache.find((r: any) => r.key === 'AAPL');

    await page.goto(`${server.baseURL}/status`);
    await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/refresh') && r.request().method() === 'POST'),
      page.getByRole('button', { name: '强制刷新' }).click(),
    ]);
    await page.waitForResponse((r) => r.url().endsWith('/status') && r.request().method() === 'GET');

    const after = await apiStatus(server.baseURL);
    // still inside the TTL window, yet upstream was called again for every symbol
    expect(after.upstream.total_calls).toBe(callsBefore + 3);
    const aaplAfter = after.cache.find((r: any) => r.key === 'AAPL');
    expect(aaplAfter.last_refresh).toBeGreaterThan(aaplBefore.last_refresh);
    expect(aaplAfter.fresh).toBe(true);
  });
});

test('非法配置启动即失败: invalid config value fails startup and names the key', async () => {
  const port = await freePort();
  let buf = '';
  const proc = spawn(
    process.env.PYTHON || 'python',
    ['-m', 'uvicorn', 'service.main:app', '--port', String(port)],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        STOCK_DASH_CACHE_TTL_SECONDS: 'not-a-number',
      },
    },
  );
  proc.stdout?.on('data', (d) => (buf += d.toString()));
  proc.stderr?.on('data', (d) => (buf += d.toString()));
  const exitCode: number = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      proc.kill();
      reject(new Error(`server did not exit within 30s; output:\n${buf}`));
    }, 30_000);
    proc.on('exit', (code) => {
      clearTimeout(timer);
      resolve(code ?? -1);
    });
  });
  expect(exitCode).not.toBe(0);
  expect(buf).toContain('cache_ttl_seconds');
  expect(buf).toContain('float');
  expect(buf).toContain('not-a-number');
});

