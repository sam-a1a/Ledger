import type { ToolCall } from "../state/types";

/** The inline status strip shown while a call runs, before it folds into the trace. */
export function ToolStatusRow({ call }: { call: ToolCall }) {
  return (
    <div className={`tool-row ${call.status}`} data-testid="tool-row" data-tool={call.tool}>
      <span className="name">{call.tool}</span>
      <span className="dot">·</span>
      <span>{summarise(call)}</span>
    </div>
  );
}

function summarise(call: ToolCall): string {
  if (call.status === "running") return "running…";
  if (call.status === "error") return call.errorCode ?? "failed";
  const rows = call.rowCount === undefined ? "" : `${call.rowCount.toLocaleString()} rows`;
  const ms = call.durationMs === undefined ? "" : `${call.durationMs} ms`;
  return [rows, ms].filter(Boolean).join("  ·  ");
}
