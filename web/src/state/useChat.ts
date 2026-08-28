import { useCallback, useEffect, useRef, useState } from "react";
import * as accountApi from "../api/account";
import type { ConversationSummary } from "../api/account";
import { askQuestion } from "../api/client";
import { readSse } from "../api/sse";
import { reducer } from "./reducer";
import { initialState, type ChatState } from "./types";

/**
 * Token deltas are accumulated and flushed on a timer rather than dispatched
 * per token. A `setState` per token, with a markdown re-parse behind it, janks
 * visibly past a few hundred tokens; batching costs nothing a reader can
 * perceive, because the frame rate is the ceiling on what they could see.
 */
const FLUSH_INTERVAL_MS = 40;

export function useChat(token: string | null) {
  const [state, setState] = useState<ChatState>(initialState);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const controller = useRef<AbortController | null>(null);

  const dispatch = useCallback(
    (action: Parameters<typeof reducer>[1]) => setState((current) => reducer(current, action)),
    [],
  );

  const refresh = useCallback(
    async (archived = showArchived) => {
      if (!token) return;
      try {
        setConversations(await accountApi.listConversations(token, archived));
      } catch {
        // A failed refresh should not take the chat down; the list simply
        // stays as it was until the next successful call.
      }
    },
    [token, showArchived],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const newChat = useCallback(() => {
    controller.current?.abort();
    setState(initialState);
  }, []);

  const openConversation = useCallback(
    async (id: string) => {
      if (!token) return;
      controller.current?.abort();
      setLoadingTranscript(true);
      try {
        const { conversation, messages } = await accountApi.getTranscript(token, id);
        setState({
          ...initialState,
          conversationId: conversation.id,
          turns: messages.map((message, index) => ({
            id: `${conversation.id}-${index}`,
            kind: message.role === "user" ? "user" : "assistant",
            text: message.text,
            calls: (message.trace ?? []).map((entry, position) => ({
              callId: String(entry.call_id ?? `${index}-${position}`),
              tool: String(entry.tool ?? "?"),
              args: (entry.args ?? {}) as Record<string, unknown>,
              turn: 0,
              status: entry.ok === false ? "error" : "ok",
              rowCount: entry.row_count as number | undefined,
              durationMs: entry.duration_ms as number | undefined,
              errorCode: entry.error_code as string | undefined,
            })),
            // Charts are not replayed: they are derived from a cached result
            // the server no longer holds, and inventing one would show numbers
            // nobody computed.
            charts: [],
            status: "done",
          })) as ChatState["turns"],
        });
      } finally {
        setLoadingTranscript(false);
      }
    },
    [token],
  );

  const stop = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    dispatch({ type: "finish", status: "cancelled" });
  }, [dispatch]);

  const ask = useCallback(
    async (text: string) => {
      if (!token || state.streaming) return;

      const id = crypto.randomUUID();
      dispatch({ type: "ask", id, text, assistantId: `${id}-a` });

      const abort = new AbortController();
      controller.current = abort;

      let pending = "";
      let timer: number | null = null;
      const flush = () => {
        if (!pending) return;
        dispatch({ type: "sse", event: { event: "token", data: { seq: 0, text: pending } } });
        pending = "";
      };

      try {
        const response = await askQuestion(token, text, state.conversationId, abort.signal);
        if (!response.ok) throw new Error(`request failed: ${response.status}`);

        for await (const event of readSse(response, abort.signal)) {
          if (event.event === "token") {
            pending += event.data.text;
            timer ??= window.setTimeout(() => {
              timer = null;
              flush();
            }, FLUSH_INTERVAL_MS);
            continue;
          }
          flush();
          if (timer !== null) {
            clearTimeout(timer);
            timer = null;
          }
          dispatch({ type: "sse", event });
        }
        flush();
      } catch (error) {
        if ((error as Error).name === "AbortError") return;
        dispatch({ type: "finish", status: "error", message: String(error) });
      } finally {
        if (timer !== null) clearTimeout(timer);
        flush();
        controller.current = null;
        dispatch({ type: "finish", status: "done" });
        // The title is derived server-side from the opening question, so the
        // list only becomes correct after the first turn completes.
        void refresh();
      }
    },
    [token, state.streaming, state.conversationId, dispatch, refresh],
  );

  const rename = useCallback(
    async (id: string, title: string) => {
      if (!token) return;
      await accountApi.updateConversation(token, id, { title });
      await refresh();
    },
    [token, refresh],
  );

  const archive = useCallback(
    async (id: string, archived: boolean) => {
      if (!token) return;
      await accountApi.updateConversation(token, id, { archived });
      if (id === state.conversationId) setState(initialState);
      await refresh();
    },
    [token, refresh, state.conversationId],
  );

  const remove = useCallback(
    async (id: string) => {
      if (!token) return;
      await accountApi.deleteConversation(token, id);
      if (id === state.conversationId) setState(initialState);
      await refresh();
    },
    [token, refresh, state.conversationId],
  );

  const toggleArchived = useCallback(() => {
    setShowArchived((current) => {
      void refresh(!current);
      return !current;
    });
  }, [refresh]);

  useEffect(() => () => controller.current?.abort(), []);

  return {
    state,
    conversations,
    showArchived,
    loadingTranscript,
    ask,
    stop,
    newChat,
    openConversation,
    rename,
    archive,
    remove,
    toggleArchived,
  };
}
