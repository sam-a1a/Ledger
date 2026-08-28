import { readFileSync, rmSync } from "node:fs";
import { CONSUMER_PID_FILE } from "./global-setup";

/** Stop the audit consumer `global-setup` left running. */
export default function globalTeardown(): void {
  let pid: number;
  try {
    pid = Number(readFileSync(CONSUMER_PID_FILE, "utf8").trim());
  } catch {
    return; // never started, or already cleaned up
  }
  try {
    process.kill(pid, "SIGTERM");
  } catch {
    // Already gone. Nothing to do, and nothing worth failing a run over.
  }
  rmSync(CONSUMER_PID_FILE, { force: true });
}
