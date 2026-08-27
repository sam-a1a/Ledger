import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { askQuestion, login, type Session } from "../api/client";
import { readSse } from "../api/sse";
import { reducer } from "./reducer";
import { initialState, type Role } from "./types";

/**
 * Token deltas are accumulated in a ref and flushed once per animation frame.
 *
 * A `setState` per token, with a markdown re-parse behind it, visibly janks
 * past a few hundred tokens. Batching costs nothing a reader can perceive --
 * the frame rate is the ceiling on what they could see anyway.
 */
const FLUSH_INTERVAL_MS = 40;

export function useChat() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const [session, setSession] = useState<Session | null>(null);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    login("analyst").then(setSession).catch(console.error);
  }, []);

  const switchRole = useCallback(async (role: Role) => {
    const next = await login(role);
    setSession(next);
    dispatch({ type: "reset" });
  }, []);

  const stop = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    dispatch({ type: "finish", status: "cancelled" });
  }, []);

  const ask = useCallback(
    async (text: string) => {
      if (!session || state.streaming) return;

      const id = crypto.randomUUID();
      dispatch({ type: "ask", id, text, assistantId: `${id}-a` });

      const abort = new AbortController();
      controller.current = abort;

      // Coalesce token frames; everything else applies immediately so tool
      // status rows appear the moment work starts.
      let pending = "";
      let timer: number | null = null;
      const flush = () => {
        if (!pending) return;
        dispatch({
          type: "sse",
          event: { event: "token", data: { seq: 0, text: pending } },
        });
        pending = "";
      };

      try {
        const response = await askQuestion(session, text, state.conversationId, abort.signal);
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
      }
    },
    [session, state.streaming, state.conversationId],
  );

  // Abort in flight work if the component unmounts, so the server sees the
  // disconnect and cancels rather than finishing a query nobody will read.
  useEffect(() => () => controller.current?.abort(), []);

  return { state, session, ask, stop, switchRole };
}
