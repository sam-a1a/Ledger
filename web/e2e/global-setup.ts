import { execFileSync } from "node:child_process";

/**
 * Empties the end-to-end database before the suite runs.
 *
 * Specs assert counts -- one conversation in the sidebar, two answers in a
 * transcript -- so rows inherited from a previous run fail them for a reason
 * that has nothing to do with the code. It truncates rather than drops:
 * Playwright starts the web servers before this runs, so dropping would pull
 * the schema out from under an API that has already migrated.
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
