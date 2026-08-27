import type { Role } from "../state/types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export interface Account {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  tenant_id: number | null;
  avatar_url: string | null;
  preferences: Record<string, unknown>;
  has_password: boolean;
}

export interface Session {
  token: string;
  account: Account;
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  message_count: number;
}

export interface TranscriptMessage {
  role: string;
  text: string;
  trace: Record<string, unknown>[];
  created_at: string;
}

/** The API's error shape, unwrapped so callers show the server's words. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // FastAPI puts the message in `detail`; a validation error puts a list
    // there instead, so unwrap rather than rendering "[object Object]".
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? (detail[0]?.msg ?? "That did not work.")
          : "That did not work.";
    throw new ApiError(message, response.status);
  }
  return body as T;
}

interface TokenResponse {
  access_token: string;
  account: Account;
}

const toSession = (r: TokenResponse): Session => ({ token: r.access_token, account: r.account });

export const signUp = (email: string, password: string, displayName?: string) =>
  request<TokenResponse>("/api/accounts/signup", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name: displayName || null }),
  }).then(toSession);

export const signIn = (email: string, password: string) =>
  request<TokenResponse>("/api/accounts/signin", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  }).then(toSession);

export const forgotPassword = (email: string) =>
  request<{ message: string; reset_token: string | null }>("/api/accounts/forgot-password", {
    method: "POST",
    body: JSON.stringify({ email }),
  });

export const resetPassword = (token: string, password: string) =>
  request<TokenResponse>("/api/accounts/reset-password", {
    method: "POST",
    body: JSON.stringify({ token, password }),
  }).then(toSession);

export const me = (token: string) => request<Account>("/api/accounts/me", {}, token);

export const changePassword = (token: string, current: string, next: string) =>
  request<{ message: string }>(
    "/api/accounts/me/password",
    { method: "POST", body: JSON.stringify({ current_password: current, new_password: next }) },
    token,
  );

export const updateProfile = (token: string, patch: Record<string, unknown>) =>
  request<Account>("/api/accounts/me", { method: "PATCH", body: JSON.stringify(patch) }, token);

export const uploadAvatar = (token: string, file: File) => {
  const form = new FormData();
  form.append("file", file);
  // No Content-Type: the browser has to set the multipart boundary itself.
  return request<Account>("/api/accounts/me/avatar", { method: "POST", body: form }, token);
};

export const deleteAccount = (token: string, password: string) =>
  request<void>(
    "/api/accounts/me",
    { method: "DELETE", body: JSON.stringify({ password }) },
    token,
  );

export const listConversations = (token: string, archived = false) =>
  request<{ conversations: ConversationSummary[] }>(
    `/api/conversations?archived=${archived}`,
    {},
    token,
  ).then((r) => r.conversations);

export const getTranscript = (token: string, id: string) =>
  request<{ conversation: ConversationSummary; messages: TranscriptMessage[] }>(
    `/api/conversations/${id}`,
    {},
    token,
  );

export const updateConversation = (
  token: string,
  id: string,
  patch: { title?: string; archived?: boolean },
) =>
  request<ConversationSummary>(
    `/api/conversations/${id}`,
    { method: "PATCH", body: JSON.stringify(patch) },
    token,
  );

export const deleteConversation = (token: string, id: string) =>
  request<void>(`/api/conversations/${id}`, { method: "DELETE" }, token);
