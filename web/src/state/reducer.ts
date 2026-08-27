import type { SseEvent } from "../api/types";
import type { AssistantTurn, ChatState, ToolCall, Turn } from "./types";

export type Action =
  | { type: "ask"; id: string; text: string; assistantId: string }
  | { type: "sse"; event: SseEvent }
  | { type: "finish"; status: AssistantTurn["status"]; message?: string }
  | { type: "reset" };

/** Apply `update` to the most recent assistant turn, leaving the rest alone. */
function patchLast(
  turns: Turn[],
  update: (turn: AssistantTurn) => AssistantTurn,
): Turn[] {
  const index = turns.findLastIndex((t) => t.kind === "assistant");
  if (index === -1) return turns;
  const next = turns.slice();
  next[index] = update(turns[index] as AssistantTurn);
  return next;
}

export function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "ask":
      return {
        ...state,
        streaming: true,
        turns: [
          ...state.turns,
          { id: action.id, kind: "user", text: action.text },
          {
            id: action.assistantId,
            kind: "assistant",
            text: "",
            calls: [],
            charts: [],
            status: "streaming",
          },
        ],
      };

    case "sse":
      return applyEvent(state, action.event);

    case "finish":
      return {
        ...state,
        streaming: false,
        turns: patchLast(state.turns, (turn) => ({
          ...turn,
          status: turn.status === "streaming" ? action.status : turn.status,
          errorMessage: action.message ?? turn.errorMessage,
        })),
      };

    case "reset":
      return { ...state, turns: [], conversationId: null };
  }
}

function applyEvent(state: ChatState, event: SseEvent): ChatState {
  switch (event.event) {
    case "meta":
      return {
        ...state,
        conversationId: event.data.conversation_id,
        demoMode: event.data.demo_mode,
        modelBackend: event.data.model_backend,
      };

    case "token":
      return {
        ...state,
        turns: patchLast(state.turns, (t) => ({ ...t, text: t.text + event.data.text })),
      };

    case "tool_call_start": {
      const call: ToolCall = {
        callId: event.data.call_id,
        tool: event.data.tool,
        args: event.data.args,
        turn: event.data.turn,
        status: "running",
      };
      return {
        ...state,
        turns: patchLast(state.turns, (t) => ({ ...t, calls: [...t.calls, call] })),
      };
    }

    case "tool_call_end": {
      const d = event.data;
      return {
        ...state,
        turns: patchLast(state.turns, (t) => ({
          ...t,
          calls: t.calls.map((c) =>
            c.callId === d.call_id
              ? {
                  ...c,
                  status: d.ok ? "ok" : "error",
                  rowCount: d.row_count,
                  durationMs: d.duration_ms,
                  truncated: d.truncated,
                  resultId: d.result_id,
                  sql: d.sql,
                  errorCode: d.error_code,
                  message: d.message,
                  notes: d.notes,
                }
              : c,
          ),
        })),
      };
    }

    case "chart":
      return {
        ...state,
        turns: patchLast(state.turns, (t) => ({ ...t, charts: [...t.charts, event.data] })),
      };

    case "error":
      return {
        ...state,
        turns: patchLast(state.turns, (t) => ({
          ...t,
          status: event.data.fatal ? "error" : t.status,
          errorMessage: event.data.message,
        })),
      };

    case "done":
      return {
        ...state,
        streaming: false,
        turns: patchLast(state.turns, (t) => ({
          ...t,
          status: t.status === "streaming" ? "done" : t.status,
          stopReason: event.data.stop_reason,
          usage: event.data.usage,
        })),
      };

    case "thinking":
      return state;
  }
}
