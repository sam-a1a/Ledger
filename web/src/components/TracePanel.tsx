import { useState } from "react";
import { useAudit } from "../state/useAudit";
import type { AuditStatus } from "../state/useAudit";
import type { ToolCall } from "../state/types";

const BADGE: Record<AuditStatus, { label: string; title: string }> = {
  idle: { label: "", title: "" },
  pending: {
    label: "checking audit log",
    title: "Waiting for the consumer to materialise these events.",
  },
  verified: {
    label: "audit ✓",
    title: "Every call in this trace is in the durable audit log.",
  },
  diverged: {
    label: "audit diverged",
    title:
      "Some calls in this trace are not in the audit log. The two records should agree; they do not.",
  },
  missing: {
    label: "audit missing",
    title: "None of these calls reached the audit log within the time allowed.",
  },
};

interface Props {
  calls: ToolCall[];
  token?: string | null;
  conversationId?: string | null;
  /** Reconciliation waits for the turn: reading mid-stream races the consumer. */
  settled?: boolean;
}

/**
 * The live trace is derived entirely from the call events already received,
 * which is why it is complete at every instant -- including when a stream dies
 * mid-answer. Reading `/api/audit` instead would lag the answer.
 *
 * Once the turn has settled the two are reconciled, because "the trace is a
 * view over an event log" is a claim worth making visible rather than
 * asserting in a README. Same data, two paths.
 */
export function TracePanel({ calls, token, conversationId, settled }: Props) {
  const [opened, setOpened] = useState(false);
  const audit = useAudit(token ?? null, conversationId ?? null, calls, opened && !!settled);

  if (calls.length === 0) return null;
  const total = calls.reduce((sum, c) => sum + (c.durationMs ?? 0), 0);
  const badge = BADGE[audit.status];

  return (
    <details
      className="trace"
      data-testid="trace"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary data-testid="trace-toggle">
        {calls.length} tool call{calls.length === 1 ? "" : "s"} · {total} ms
        {badge.label && (
          <span
            className={`audit-badge audit-${audit.status}`}
            data-testid="audit-badge"
            data-status={audit.status}
            title={badge.title}
          >
            {badge.label}
            {audit.status === "diverged" && ` (${audit.found}/${audit.expected})`}
          </span>
        )}
      </summary>
      <table>
        <thead>
          <tr>
            <th>tool</th>
            <th>arguments</th>
            <th style={{ textAlign: "right" }}>rows</th>
            <th style={{ textAlign: "right" }}>ms</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((call) => (
            <tr key={call.callId} data-testid="trace-row" data-tool={call.tool}>
              <td className={call.status === "error" ? "failed" : undefined}>{call.tool}</td>
              <td className="args">
                <code>{JSON.stringify(call.args)}</code>
                {call.status === "error" && call.message && (
                  <div className="failed">{call.message}</div>
                )}
                {call.notes?.map((note) => (
                  <div className="note" key={note}>
                    {note}
                  </div>
                ))}
              </td>
              <td className="num" data-testid="trace-rows">
                {call.rowCount?.toLocaleString() ?? "—"}
              </td>
              <td className="num" data-testid="trace-ms">
                {call.durationMs ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}
