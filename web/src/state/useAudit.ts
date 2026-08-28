import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../api/account";
import type { ToolCall } from "./types";

export type AuditStatus = "idle" | "pending" | "verified" | "diverged" | "missing";

export interface Reconciliation {
  status: AuditStatus;
  found: number;
  expected: number;
}

/**
 * How long to keep asking before calling it missing rather than late.
 *
 * Generous on purpose. A consumer restart or a Kafka group rebalance takes
 * tens of seconds, and during one the events are on the topic and simply not
 * materialised yet. Reporting a governance failure then would be a false
 * accusation, and a badge that cries wolf gets ignored when it is right.
 */
const BUDGET_MS = 30_000;
const INTERVAL_MS = 800;

/**
 * Reconcile the trace against the durable audit log.
 *
 * The trace itself is derived from the SSE stream, and stays that way: reading
 * the log during streaming would race the consumer, and a panel that lagged
 * the answer would look broken rather than careful.
 *
 * Afterwards the two should agree, and checking that they do is what turns
 * "the trace is a view over an event log" from a claim in the README into
 * something visible in the product. The consumer commits after each write, so
 * events arrive within a second or so; until they do the state is `pending`,
 * which is different from `missing` and is displayed differently.
 */
export function useAudit(
  token: string | null,
  conversationId: string | null,
  calls: ToolCall[],
  enabled: boolean,
): Reconciliation {
  const [state, setState] = useState<Reconciliation>({
    status: "idle",
    found: 0,
    expected: 0,
  });
  const startedAt = useRef<number | null>(null);

  const expectedIds = calls.map((call) => call.callId).sort();
  const key = expectedIds.join(",");

  const check = useCallback(async (): Promise<AuditStatus> => {
    if (!token || !conversationId) return "idle";
    const { events } = await api.auditFor(token, conversationId);

    // One call produces a `requested` and a `completed` event. Matching on the
    // call id rather than counting events keeps this honest when a call is
    // refused before execution and produces only the first of the pair.
    const seen = new Set(events.map((event) => event.call_id).filter(Boolean));
    const found = expectedIds.filter((id) => seen.has(id)).length;

    setState({
      status: found === expectedIds.length ? "verified" : "pending",
      found,
      expected: expectedIds.length,
    });
    return found === expectedIds.length ? "verified" : "pending";
    // `expectedIds` is derived from `key`, which is the dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, conversationId, key]);

  useEffect(() => {
    if (!enabled || !token || !conversationId || expectedIds.length === 0) return;

    let live = true;
    startedAt.current = Date.now();

    const poll = async () => {
      if (!live) return;
      let status: AuditStatus;
      try {
        status = await check();
      } catch {
        // A failed read is not a divergence: the log may be unreachable for
        // reasons that say nothing about whether the events were written.
        setState((prior) => ({ ...prior, status: "pending" }));
        status = "pending";
      }
      if (!live || status === "verified") return;

      if (Date.now() - (startedAt.current ?? 0) > BUDGET_MS) {
        setState((prior) => ({
          ...prior,
          // Some events arrived but not all: the two records disagree, which
          // is a different and more serious thing than none arriving.
          status: prior.found > 0 ? "diverged" : "missing",
        }));
        return;
      }
      window.setTimeout(poll, INTERVAL_MS);
    };

    void poll();
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, token, conversationId, key, check]);

  return state;
}
