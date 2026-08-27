import { defineConfig, devices } from "@playwright/test";

/**
 * The API runs against the deterministic fixture with the scripted model, so
 * the suite is free, offline, and repeatable. Determinism comes from the fake
 * backend, never from sleeps -- there is no `waitForTimeout` anywhere in these
 * specs.
 *
 * Kafka is a hard dependency of the application, so a broker must be running:
 * `docker compose up -d kafka` provides one on localhost:29092.
 *
 * The app talks to the API directly rather than through Vite's proxy, which
 * buffers server-sent events -- a stream that works with curl hangs in the
 * browser. Production serves both from one origin behind nginx.
 *
 */
const KAFKA = process.env.LEDGER_KAFKA_BOOTSTRAP ?? "localhost:29092";

/**
 * A port of the suite's own, distinct from both the dev server (8077) and
 * anything Compose publishes.
 *
 * `reuseExistingServer` will happily attach to whatever answers on a matching
 * port, including a *different* server. The suite silently ran against the
 * Compose stack once -- which has no scripted token delay, so the test that
 * proves the answer renders incrementally failed for a reason that had nothing
 * to do with the code. Reuse is therefore off, and the port is unshared.
 */
const API_PORT = 8079;

/**
 * A database of its own. Account behaviour depends on how many accounts exist
 * — the first one created becomes an analyst — so a suite inheriting rows from
 * a previous run would pass or fail depending on what ran before it.
 */
const DATABASE_URL =
  process.env.LEDGER_E2E_DATABASE_URL ??
  "postgresql+asyncpg://ledger:ledger@localhost:5455/ledger_e2e";
const REPO = new URL("..", import.meta.url).pathname;

export default defineConfig({
  // `screenshots/` holds the README capture run, which is invoked explicitly
  // via `npm run screenshots` rather than on every test run.
  testDir: ".",
  testMatch: process.env.SCREENSHOTS ? "screenshots/*.spec.ts" : "e2e/*.spec.ts",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],
  timeout: 45_000,
  expect: { timeout: 15_000 },

  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "on-first-retry",
    video: "retain-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  // Empty schema before every run, for the reason above.
  globalSetup: "./e2e/global-setup.ts",

  webServer: [
    {
      command: `uv run uvicorn ledger.api.app:app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: REPO,
      url: `http://127.0.0.1:${API_PORT}/api/ready`,
      reuseExistingServer: false,
      timeout: 90_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        LEDGER_KAFKA_BOOTSTRAP_SERVERS: KAFKA,
        LEDGER_DATA_DIR: `${REPO}tests/fixtures/data`,
        // Any address at this domain signs up as an analyst, so a spec that
      // needs one does not depend on running before every other spec.
      LEDGER_ANALYST_EMAILS: "@analyst.example.com",
      LEDGER_MODEL: "fake",
        LEDGER_CATALOG_MODE: "auto",
        // Widens the streaming window so `chat.spec` can observe a turn
        // mid-flight on a loaded runner. The assertion does not depend on the
        // exact value -- this only makes the observation comfortable.
        LEDGER_FAKE_TOKEN_DELAY_MS: "25",
        LEDGER_DATABASE_URL: DATABASE_URL,
      },
    },
    {
      // VITE_API_BASE is baked in at build time, so the build has to happen
      // here rather than relying on a previously built dist.
      command: "npm run build && npm run preview",
      reuseExistingServer: false,
      env: { VITE_API_BASE: `http://127.0.0.1:${API_PORT}` },
      url: "http://127.0.0.1:4173",
      timeout: 60_000,
    },
  ],
});
