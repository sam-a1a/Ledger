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
 * The API runs on 8077 rather than 8000. Port 8000 is the single most contested
 * default on a developer machine, and a collision here does not fail cleanly --
 * Playwright either reuses somebody else's server or dies on bind.
 */
const KAFKA = process.env.LEDGER_KAFKA_BOOTSTRAP ?? "localhost:29092";
const REPO = new URL("..", import.meta.url).pathname;

export default defineConfig({
  testDir: "./e2e",
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

  webServer: [
    {
      command: "uv run uvicorn ledger.api.app:app --host 127.0.0.1 --port 8077",
      cwd: REPO,
      url: "http://127.0.0.1:8077/api/ready",
      reuseExistingServer: !process.env.CI,
      timeout: 90_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        LEDGER_KAFKA_BOOTSTRAP_SERVERS: KAFKA,
        LEDGER_DATA_DIR: `${REPO}tests/fixtures/data`,
        LEDGER_MODEL: "fake",
        LEDGER_CATALOG_MODE: "auto",
        // A visible delay in exactly one place, so `chat.spec` can prove the
        // answer renders incrementally rather than arriving as one blob.
        LEDGER_FAKE_TOKEN_DELAY_MS: "12",
      },
    },
    {
      // VITE_API_BASE is baked in at build time, so the build has to happen
      // here rather than relying on a previously built dist.
      command: "npm run build && npm run preview",
      env: { VITE_API_BASE: "http://127.0.0.1:8077" },
      url: "http://127.0.0.1:4173",
      reuseExistingServer: !process.env.CI,
      timeout: 60_000,
    },
  ],
});
