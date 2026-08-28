import { useCallback, useEffect, useState } from "react";
import * as api from "../api/account";
import type { Account, Session } from "../api/account";

const STORAGE_KEY = "ledger.session";

/**
 * The token lives in localStorage, which is reachable by any script that gets
 * onto the page. That is a real trade-off and the right one here: this is a
 * single-page app with no server-rendered session, and the alternative — an
 * httpOnly cookie — needs CSRF protection that a token in a header does not.
 * A production deployment handling other people's data should revisit it.
 */
function load(): Session | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function useSession() {
  const [session, setSession] = useState<Session | null>(load);
  const [checking, setChecking] = useState(true);

  // A stored token can be expired, or name an account that has since been
  // deleted. Verify once on load rather than discovering it on first use.
  useEffect(() => {
    const stored = load();
    if (!stored) {
      setChecking(false);
      return;
    }
    api
      .me(stored.token)
      .then((account) => persist({ ...stored, account }))
      .catch(() => persist(null))
      .finally(() => setChecking(false));
  }, []);

  const persist = useCallback((next: Session | null) => {
    setSession(next);
    try {
      if (next) localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      else localStorage.removeItem(STORAGE_KEY);
    } catch {
      // Private browsing, or storage disabled. The session still works for
      // this tab; it just will not survive a reload.
    }
  }, []);

  const updateAccount = useCallback(
    (account: Account) => {
      setSession((current) => {
        if (!current) return current;
        const next = { ...current, account };
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
        } catch {
          /* see above */
        }
        return next;
      });
    },
    [],
  );

  const signOut = useCallback(() => persist(null), [persist]);

  return { session, checking, setSession: persist, updateAccount, signOut };
}
