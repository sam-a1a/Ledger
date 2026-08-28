/**
 * Where the API lives.
 *
 * Empty means same-origin, which is the production path: nginx serves the app
 * and proxies /api with buffering disabled.
 *
 * In development it points straight at the API instead of going through Vite's
 * proxy, which buffers server-sent events -- a request streams perfectly with
 * curl and then hangs forever in the browser. Talking to the API directly keeps
 * the streaming path identical to production and removes a dev-only proxy from
 * between us and the thing under test.
 */
const BASE = import.meta.env.VITE_API_BASE ?? "";

export function askQuestion(
  token: string,
  message: string,
  conversationId: string | null,
  signal: AbortSignal,
): Promise<Response> {
  return fetch(`${BASE}/api/chat`, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
}
