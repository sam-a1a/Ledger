import { execFileSync, spawn } from "node:child_process";
import { mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from "node:fs";

const REPO = new URL("../..", import.meta.url).pathname;

/** Where the consumer's pid is left for `global-teardown` to find. */
export const CONSUMER_PID_FILE = new URL("../test-results/audit-consumer.pid", import.meta.url);

/**
 * Empty the end-to-end state, then start the audit consumer.
 *
 * Specs assert counts -- one conversation in the sidebar, two answers in a
 * transcript -- so rows inherited from a previous run fail them for a reason
 * that has nothing to do with the code.
 *
 * The database is truncated rather than dropped: Playwright starts the web
 * servers *before* this runs, so dropping would pull the schema out from under
 * an API that has already migrated, and every request would then fail with
 * `relation "users" does not exist` -- which reads like a migration bug.
 */
export default async function globalSetup(): Promise<void> {
  const databaseUrl =
    process.env.LEDGER_E2E_DATABASE_URL ??
    "postgresql+asyncpg://ledger:ledger@localhost:5455/ledger_e2e";

  // The materialised audit log, cleared for the same reason as the database.
  rmSync(new URL("../../tests/fixtures/data/audit", import.meta.url), {
    recursive: true,
    force: true,
  });

  execFileSync("uv", ["run", "python", "-m", "scripts.reset_e2e_db"], {
    cwd: REPO,
    env: { ...process.env, LEDGER_DATABASE_URL: databaseUrl },
    stdio: "inherit",
  });

  await startAuditConsumer(databaseUrl);
}

/**
 * Run the audit materialiser for the duration of the suite.
 *
 * Started here rather than as a `webServer` entry because Playwright brings
 * those up in order and waits for each one's URL. A consumer has no URL of its
 * own, and pointing it at the API's readiness check deadlocks: the API is the
 * entry that comes after it.
 *
 * It exists so `/api/audit` serves a genuinely materialised log. Without it the
 * trace panel's reconciliation could only ever report `missing`, and the test
 * asserting the Kafka round trip would be asserting nothing.
 */
async function startAuditConsumer(databaseUrl: string): Promise<void> {
  mkdirSync(new URL("../test-results", import.meta.url), { recursive: true });
  // Truncated, not appended: the readiness check below looks for a marker in
  // this file, and a previous run's marker would satisfy it immediately.
  const logPath = new URL("../test-results/audit-consumer.log", import.meta.url);
  const log = openSync(logPath, "w");

  const child = spawn("uv", ["run", "ledger-audit"], {
    cwd: REPO,
    env: {
      ...process.env,
      LEDGER_KAFKA_BOOTSTRAP_SERVERS: process.env.LEDGER_KAFKA_BOOTSTRAP ?? "localhost:29092",
      LEDGER_DATA_DIR: `${REPO}tests/fixtures/data`,
      LEDGER_DATABASE_URL: databaseUrl,
      // A group of its own per run. Sharing the durable group meant a member
      // left over from the previous run still held one partition while the
      // coordinator waited out its session timeout, so this consumer was
      // assigned only the other one. Events published to the partition it did
      // not own were never materialised, and the reconciliation spec failed
      // or passed depending on which partition the run's events hashed to --
      // bimodally, which reads like flakiness but is not.
      LEDGER_KAFKA_CONSUMER_GROUP: `ledger-audit-e2e-${process.pid}`,
    },
    detached: true,
    // Logged rather than discarded: when reconciliation reports `missing`, the
    // first question is whether the consumer was running at all.
    stdio: ["ignore", log, log],
  });
  child.unref();
  if (child.pid) writeFileSync(CONSUMER_PID_FILE, String(child.pid));

  // Wait for it to join the group before letting the suite run. Joining takes
  // several seconds, and a spec that asks a question first puts its events on
  // the topic before anything is reading -- which is fine for the data, since
  // offsets are committed, but means the reconciliation spec races a consumer
  // that has not started. Waiting here is the same principle as Playwright
  // waiting on the API's readiness check.
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try {
      if (readFileSync(logPath, "utf8").includes("audit consumer reading")) return;
    } catch {
      // Not written yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error("the audit consumer did not start; see web/test-results/audit-consumer.log");
}
