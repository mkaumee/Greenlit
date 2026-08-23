/**
 * The instrument panel.
 *
 * Read-only and undesigned on purpose. Its job is to make the loop visible —
 * the system has been ticking every minute in production with no way to watch
 * a negotiation move except the Firestore console. Phase 6 turns this into the
 * Timeline; until then it is the debugging surface for everything already
 * built.
 */

import { useEffect, useState } from "react";

import { auth, signIn, signOutOfEverything, USE_EMULATOR } from "./firebase";
import type { User } from "./firebase";

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    // Fires once with the restored session before any interaction, which is
    // why "signed out" must not be rendered until it has.
    return auth.onAuthStateChanged((next) => {
      setUser(next);
      setReady(true);
    });
  }, []);

  const attempt = (action: () => Promise<void>) => () => {
    setError("");
    void action().catch((cause: unknown) => {
      setError(cause instanceof Error ? cause.message : String(cause));
    });
  };

  if (!ready) {
    return <main style={S.page}>Checking sign-in…</main>;
  }

  return (
    <main style={S.page}>
      <header style={S.header}>
        <h1 style={S.title}>Agentic Cinema</h1>
        {user === null ? (
          <button style={S.button} onClick={attempt(signIn)} type="button">
            Sign in with Google
          </button>
        ) : (
          <span style={S.who}>
            {user.email ?? user.uid}
            <button
              style={S.button}
              onClick={attempt(signOutOfEverything)}
              type="button"
            >
              Sign out
            </button>
          </span>
        )}
      </header>

      {USE_EMULATOR && <p style={S.badge}>emulator</p>}
      {error !== "" && <p style={S.error}>{error}</p>}

      {user === null ? (
        <p>
          Every read is gated behind <code>isSignedIn()</code> in{" "}
          <code>firestore.rules</code>. There is no anonymous view.
        </p>
      ) : (
        <p>Signed in.</p>
      )}
    </main>
  );
}

const S = {
  page: {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 13,
    padding: "1.5rem",
  },
  header: {
    alignItems: "baseline",
    display: "flex",
    gap: "1rem",
    justifyContent: "space-between",
  },
  title: { fontSize: 16, fontWeight: 600, margin: 0 },
  who: { alignItems: "center", display: "flex", gap: "0.75rem" },
  button: { cursor: "pointer", font: "inherit", padding: "0.25rem 0.75rem" },
  badge: { color: "#a60", margin: "0.5rem 0" },
  error: { color: "#c00", margin: "0.5rem 0" },
} as const;
