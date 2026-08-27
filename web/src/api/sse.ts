import type { SseEvent, SseEventName } from "./types";

/**
 * Read a server-sent event stream from a POST response.
 *
 * `EventSource` is GET-only, so using it would mean putting the question into a
 * query string and losing the Authorization header. `fetch` plus a
 * ReadableStream also gives us an AbortController, which is what lets the Stop
 * button close the connection and trigger cancellation on the server.
 */
export async function* readSse(
  response: Response,
  signal?: AbortSignal,
): AsyncGenerator<SseEvent> {
  const body = response.body;
  if (!body) throw new Error("response had no body to stream");

  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;

      // Frames are separated by a blank line. Anything after the last separator
      // is a partial frame and stays in the buffer.
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        const raw = buffer.slice(0, separator);
        buffer = buffer.slice(separator + 2);
        const parsed = parseFrame(raw);
        if (parsed) yield parsed;
        separator = buffer.indexOf("\n\n");
      }
    }
  } finally {
    // An aborted fetch leaves the reader open otherwise.
    if (signal?.aborted) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function parseFrame(raw: string): SseEvent | null {
  let event: string | null = null;
  let data: string | null = null;

  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) return null; // keepalive comment
    if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data = line.slice(6);
  }
  if (!event || data === null) return null;

  try {
    return { event: event as SseEventName, data: JSON.parse(data) } as SseEvent;
  } catch {
    // A malformed frame is a bug worth seeing, not worth crashing the stream.
    console.error("could not parse SSE frame", raw);
    return null;
  }
}
