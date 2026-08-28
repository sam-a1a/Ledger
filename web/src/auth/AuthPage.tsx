import { useState } from "react";
import * as api from "../api/account";
import type { Session } from "../api/account";

type Mode = "signin" | "signup" | "forgot" | "reset";

export function AuthPage({ onSignedIn }: { onSignedIn: (session: Session) => void }) {
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [token, setToken] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const go = (next: Mode) => {
    setMode(next);
    setError(null);
    setNotice(null);
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "signin") onSignedIn(await api.signIn(email, password));
      else if (mode === "signup") onSignedIn(await api.signUp(email, password, displayName));
      else if (mode === "reset") onSignedIn(await api.resetPassword(token, password));
      else {
        const result = await api.forgotPassword(email);
        setNotice(result.message);
        // Only present in development, where there is no mail provider. The
        // server decides that; the client just shows what it was given.
        if (result.reset_token) {
          setToken(result.reset_token);
          setMode("reset");
          setNotice("Development mode — the reset token has been filled in for you.");
        }
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth">
      <div className="auth-card">
        <h1>Ledger</h1>
        <p className="auth-tagline">Streaming chat over governed data.</p>

        <form onSubmit={submit} data-testid="auth-form">
          {mode !== "reset" && (
            <label>
              Email
              <input
                type="email"
                required
                autoComplete="email"
                value={email}
                data-testid="auth-email"
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
          )}

          {mode === "signup" && (
            <label>
              Display name <span className="hint">optional</span>
              <input
                type="text"
                value={displayName}
                data-testid="auth-name"
                onChange={(e) => setDisplayName(e.target.value)}
              />
            </label>
          )}

          {mode === "reset" && (
            <label>
              Reset token
              <input
                type="text"
                required
                value={token}
                data-testid="auth-token"
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
          )}

          {mode !== "forgot" && (
            <label>
              {mode === "reset" ? "New password" : "Password"}
              <input
                type="password"
                required
                minLength={mode === "signin" ? undefined : 10}
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
                value={password}
                data-testid="auth-password"
                onChange={(e) => setPassword(e.target.value)}
              />
              {mode !== "signin" && <span className="hint">At least 10 characters.</span>}
            </label>
          )}

          {error && (
            <div className="auth-error" role="alert" data-testid="auth-error">
              {error}
            </div>
          )}
          {notice && (
            <div className="auth-notice" data-testid="auth-notice">
              {notice}
            </div>
          )}

          <button type="submit" className="primary" disabled={busy} data-testid="auth-submit">
            {busy
              ? "…"
              : mode === "signin"
                ? "Sign in"
                : mode === "signup"
                  ? "Create account"
                  : mode === "forgot"
                    ? "Send reset link"
                    : "Set new password"}
          </button>
        </form>

        <div className="auth-switch">
          {mode === "signin" && (
            <>
              <button type="button" onClick={() => go("signup")} data-testid="go-signup">
                Create an account
              </button>
              <button type="button" onClick={() => go("forgot")}>
                Forgot password?
              </button>
            </>
          )}
          {mode !== "signin" && (
            <button type="button" onClick={() => go("signin")} data-testid="go-signin">
              Back to sign in
            </button>
          )}
        </div>

        <p className="auth-footnote">
          The first account created becomes an analyst and can see every column. Later accounts
          start as viewers, which is the restricted role the access-control tests exercise.
        </p>
      </div>
    </div>
  );
}
