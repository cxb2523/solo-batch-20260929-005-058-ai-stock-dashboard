import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  globalSetup: "./tests/global-setup.ts",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8300",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "python -m uvicorn service.main:app --host 127.0.0.1 --port 8300",
    url: "http://127.0.0.1:8300/api/status",
    timeout: 30_000,
    reuseExistingServer: false,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      STOCK_UPSTREAM_PROVIDER: "fake",
      STOCK_CACHE_TTL_SECONDS: "60",
      STOCK_CACHE_DIR: "cache_test_e2e",
      STOCK_UPSTREAM_REQUEST_TIMEOUT: "5",
      STOCK_LOG_LEVEL: "INFO",
    },
  },
});

