import type { Role } from "../state/types";

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

export interface Session {
  token: string;
  role: Role;
  tenantId: number | null;
  demo: boolean;
}

export async function login(role: Role): Promise<Session> {
  const response = await fetch(`${BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (!response.ok) throw new Error(`login failed: ${response.status}`);
  const body = await response.json();
  return {
    token: body.access_token,
    role: body.role,
    tenantId: body.tenant_id,
    demo: body.demo,
  };
}

export function askQuestion(
  session: Session,
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
      Authorization: `Bearer ${session.token}`,
    },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  });
}
