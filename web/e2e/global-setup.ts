import { execFileSync } from "node:child_process";

/**
 * Empties the end-to-end database before the suite runs.
 *
 * Not a nicety: the first account created becomes an analyst, so a suite that
 * inherited rows would produce a viewer where it expected an analyst, and the
 * failure would look like an access-control bug rather than a dirty database.
 */
export default function globalSetup(): void {
  const url =
    process.env.LEDGER_E2E_DATABASE_URL ??
    "postgresql+asyncpg://ledger:ledger@localhost:5455/ledger_e2e";

  execFileSync("uv", ["run", "python", "-m", "scripts.reset_e2e_db"], {
    cwd: new URL("..", import.meta.url).pathname + "..",
    env: { ...process.env, LEDGER_DATABASE_URL: url },
    stdio: "inherit",
  });
}
