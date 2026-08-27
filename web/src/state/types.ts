import type { ChartPayload, UsagePayload } from "../api/types";

export type Role = "analyst" | "viewer";

export interface ToolCall {
  callId: string;
  tool: string;
  args: Record<string, unknown>;
  turn: number;
  status: "running" | "ok" | "error";
  rowCount?: number;
  durationMs?: number;
  truncated?: boolean;
  resultId?: string;
  sql?: string;
  errorCode?: string;
  message?: string;
  notes?: string[];
}

export interface UserTurn {
  id: string;
  kind: "user";
  text: string;
}

export interface AssistantTurn {
  id: string;
  kind: "assistant";
  text: string;
  /** Ordered, and the only source the trace panel renders from. */
  calls: ToolCall[];
  charts: ChartPayload[];
  status: "streaming" | "done" | "error" | "cancelled";
  stopReason?: string;
  usage?: UsagePayload;
  errorMessage?: string;
}

export type Turn = UserTurn | AssistantTurn;

export interface ChatState {
  turns: Turn[];
  streaming: boolean;
  conversationId: string | null;
  demoMode: boolean;
  modelBackend: string | null;
}

export const initialState: ChatState = {
  turns: [],
  streaming: false,
  conversationId: null,
  demoMode: false,
  modelBackend: null,
};
