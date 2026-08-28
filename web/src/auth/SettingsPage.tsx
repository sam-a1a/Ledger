import { useRef, useState } from "react";
import * as api from "../api/account";
import type { Account } from "../api/account";

export function SettingsPage({
  token,
  account,
  onUpdated,
  onClose,
  onSignedOut,
}: {
  token: string;
  account: Account;
  onUpdated: (account: Account) => void;
  onClose: () => void;
  onSignedOut: () => void;
}) {
  const [displayName, setDisplayName] = useState(account.display_name);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const run = async (task: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await task();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  const saveProfile = () =>
    run(async () => {
      onUpdated(await api.updateProfile(token, { display_name: displayName }));
      setMessage("Profile saved.");
    });

  const pickAvatar = (file: File | undefined) => {
    if (!file) return;
    void run(async () => {
      onUpdated(await api.uploadAvatar(token, file));
      setMessage("Avatar updated.");
    });
  };

  const savePassword = () =>
    run(async () => {
      await api.changePassword(token, current, next);
      setCurrent("");
      setNext("");
      setMessage("Password changed.");
    });

  const setPreference = (key: string, value: unknown) =>
    run(async () => {
      onUpdated(await api.updateProfile(token, { preferences: { [key]: value } }));
    });

  const removeAccount = () =>
    run(async () => {
      const password = window.prompt(
        "Deleting your account removes your profile and every conversation, permanently.\n\n" +
          "The audit log is not deleted — the record of what was queried belongs to the " +
          "organisation. It is anonymised, so the calls stay auditable without staying " +
          "attributable to you.\n\nEnter your password to confirm:",
      );
      if (!password) return;
      await api.deleteAccount(token, password);
      onSignedOut();
    });

  const preferences = account.preferences ?? {};

  return (
    <div className="settings" data-testid="settings">
      <header className="settings-header">
        <h2>Settings</h2>
        <button type="button" onClick={onClose} data-testid="close-settings">
          Close
        </button>
      </header>

      {error && <div className="auth-error" role="alert">{error}</div>}
      {message && <div className="auth-notice" data-testid="settings-message">{message}</div>}

      <section>
        <h3>Profile</h3>
        <div className="avatar-row">
          {account.avatar_url ? (
            <img src={account.avatar_url} alt="" className="avatar large" />
          ) : (
            <span className="avatar large placeholder" aria-hidden>
              {account.display_name.slice(0, 1).toUpperCase()}
            </span>
          )}
          <div>
            <button type="button" onClick={() => fileInput.current?.click()} disabled={busy}>
              Change picture
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              hidden
              data-testid="avatar-input"
              onChange={(e) => pickAvatar(e.target.files?.[0])}
            />
            <p className="hint">
              PNG, JPEG, or WebP under 4 MB. Re-encoded on upload, which strips location data
              and anything else the file was carrying.
            </p>
          </div>
        </div>

        <label>
          Display name
          <input
            type="text"
            value={displayName}
            data-testid="display-name"
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </label>
        <label>
          Email
          <input type="email" value={account.email} disabled />
        </label>
        <label>
          Role
          <input type="text" value={account.role} disabled />
          <span className="hint">Granted, not chosen — so it is not editable here.</span>
        </label>
        <button type="button" className="primary" onClick={saveProfile} disabled={busy}>
          Save profile
        </button>
      </section>

      <section>
        <h3>Preferences</h3>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={Boolean(preferences.show_thinking)}
            onChange={(e) => setPreference("show_thinking", e.target.checked)}
          />
          Show the model's reasoning while it works
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={Boolean(preferences.expand_trace)}
            onChange={(e) => setPreference("expand_trace", e.target.checked)}
          />
          Expand the tool trace by default
        </label>
      </section>

      {account.has_password && (
        <section>
          <h3>Password</h3>
          <label>
            Current password
            <input
              type="password"
              autoComplete="current-password"
              value={current}
              data-testid="current-password"
              onChange={(e) => setCurrent(e.target.value)}
            />
          </label>
          <label>
            New password
            <input
              type="password"
              minLength={10}
              autoComplete="new-password"
              value={next}
              data-testid="new-password"
              onChange={(e) => setNext(e.target.value)}
            />
          </label>
          <button type="button" onClick={savePassword} disabled={busy || !current || !next}>
            Change password
          </button>
        </section>
      )}

      <section className="danger-zone">
        <h3>Delete account</h3>
        <p>
          Removes your profile and every conversation, permanently. The audit log is kept and
          anonymised: the record of what was queried belongs to the organisation, so the calls
          stay auditable without staying attributable to you.
        </p>
        <button type="button" className="danger" onClick={removeAccount} disabled={busy}>
          Delete my account
        </button>
      </section>
    </div>
  );
}
