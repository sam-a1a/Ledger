import type { ToolCall } from "../state/types";

/**
 * Derived entirely from the call events already received, which is why it is
 * complete at every instant -- including when a stream dies mid-answer.
 */
export function TracePanel({ calls }: { calls: ToolCall[] }) {
  if (calls.length === 0) return null;
  const total = calls.reduce((sum, c) => sum + (c.durationMs ?? 0), 0);

  return (
    <details className="trace" data-testid="trace">
      <summary data-testid="trace-toggle">
        {calls.length} tool call{calls.length === 1 ? "" : "s"} · {total} ms
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
