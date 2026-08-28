import type { AssistantTurn, Turn } from "../state/types";
import { ChartCard } from "./ChartCard";
import { ToolStatusRow } from "./ToolStatusRow";
import { TracePanel } from "./TracePanel";

const SUGGESTIONS = [
  "Which pickup zones are busiest?",
  "How did fares change after the congestion charge took effect?",
  "What is the average tip by payment type?",
];

export function Transcript({
  turns,
  onSuggestion,
  token,
  conversationId,
}: {
  turns: Turn[];
  onSuggestion: (text: string) => void;
  token?: string | null;
  conversationId?: string | null;
}) {
  return (
    <div className="transcript" data-testid="transcript">
      <div className="transcript-inner">
        {turns.length === 0 ? (
          <EmptyState onSuggestion={onSuggestion} />
        ) : (
          turns.map((turn) =>
            turn.kind === "user" ? (
              <div className="turn user" key={turn.id}>
                <div className="bubble">{turn.text}</div>
              </div>
            ) : (
              <AssistantBlock
                key={turn.id}
                turn={turn}
                token={token}
                conversationId={conversationId}
              />
            ),
          )
        )}
      </div>
    </div>
  );
}

function AssistantBlock({
  turn,
  token,
  conversationId,
}: {
  turn: AssistantTurn;
  token?: string | null;
  conversationId?: string | null;
}) {
  const running = turn.calls.filter((c) => c.status === "running");
  return (
    <div className="turn assistant" data-testid="assistant-turn" data-status={turn.status}>
      {running.map((call) => (
        <ToolStatusRow key={call.callId} call={call} />
      ))}

      <div
        className={`answer ${turn.status === "streaming" ? "streaming" : ""}`}
        data-testid="answer"
      >
        {turn.text}
      </div>

      {turn.charts.map((chart) => (
        <ChartCard key={chart.chart_id} payload={chart} />
      ))}

      {turn.errorMessage && <div className="error-note">{turn.errorMessage}</div>}

      <TracePanel
        calls={turn.calls}
        token={token}
        conversationId={conversationId}
        settled={turn.status !== "streaming"}
      />
    </div>
  );
}

function EmptyState({ onSuggestion }: { onSuggestion: (text: string) => void }) {
  return (
    <div className="empty" data-testid="empty-state">
      <h2>Ask a question about 10.7 million taxi trips.</h2>
      <p>
        The model never sees a row. It calls typed tools over a profiled catalogue, and every
        call is recorded before its result is served — expand the trace under any answer to see
        exactly what ran.
      </p>
      <div className="suggestions">
        {SUGGESTIONS.map((text) => (
          <button key={text} type="button" onClick={() => onSuggestion(text)}>
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}
