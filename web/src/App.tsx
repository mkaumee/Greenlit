/**
 * The shell: sign in, pick a project, and route.
 *
 * The Inbox is the home screen rather than the item list, deliberately. The
 * agent does the reading, the researching and the days of negotiating on its
 * own; the only thing it cannot do is spend money. Making the decision queue
 * the front door puts the stop condition at the centre of the product instead
 * of leaving it as a status label on a table row.
 */

import { useEffect, useState } from "react";
import { NavLink, Route, HashRouter as Router, Routes } from "react-router-dom";

import { explain } from "@/authErrors";
import { Button } from "@/components/ui/button";
import { auth, signIn, signOutOfEverything, USE_EMULATOR } from "@/firebase";
import type { User } from "@/firebase";
import { useItems, useNegotiations, useProjects, useSuppliers } from "@/hooks/useProject";
import { Breakdown } from "@/screens/Breakdown";
import { DebugPanel } from "@/screens/DebugPanel";
import { Inbox } from "@/screens/Inbox";
import { Savings } from "@/screens/Savings";

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");

  useEffect(
    // Fires once with the restored session before any interaction, which is
    // why "signed out" must not render until it has.
    () =>
      auth.onAuthStateChanged((next) => {
        setUser(next);
        setReady(true);
      }),
    [],
  );

  if (!ready) {
    return <Centred>Checking sign-in…</Centred>;
  }

  if (user === null) {
    return (
      <Centred>
        <h1 className="text-2xl font-semibold">Greenlit</h1>
        <p className="max-w-md text-sm text-muted-foreground">
          An agent reads your screenplay, finds every prop it needs, and
          negotiates with sellers over days. It never buys anything — you do.
        </p>
        <Button
          onClick={() => {
            setError("");
            void signIn().catch((cause: unknown) => setError(explain(cause)));
          }}
        >
          Sign in with Google
        </Button>
        {error !== "" && <p className="max-w-md text-sm text-destructive">{error}</p>}
      </Centred>
    );
  }

  return (
    <Router>
      <SignedIn user={user} />
    </Router>
  );
}

function SignedIn({ user }: { user: User }) {
  const { ids } = useProjects();
  const [chosen, setChosen] = useState("");
  const projectId = chosen !== "" && ids.includes(chosen) ? chosen : (ids[0] ?? "");

  const { rows: items } = useItems(projectId);
  const { rows: negotiations } = useNegotiations(projectId);
  const { rows: suppliers } = useSuppliers(projectId);

  const supplierName = (id: string | undefined): string =>
    suppliers.find((s) => s.id === id)?.name ?? id ?? "unknown seller";

  const waiting = new Set(
    negotiations.filter((n) => n.state === "READY_FOR_HUMAN").map((n) => n.item_id),
  ).size;

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <header className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-semibold">Greenlit</h1>
          {USE_EMULATOR && (
            <span className="rounded-sm bg-muted px-2 py-0.5 text-xs">emulator</span>
          )}
        </div>
        <div className="flex items-center gap-3 text-sm">
          {ids.length > 1 && (
            <select
              className="rounded-md border bg-background px-2 py-1"
              onChange={(e) => setChosen(e.target.value)}
              value={projectId}
            >
              {ids.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          )}
          <span className="text-muted-foreground">{user.email ?? user.uid}</span>
          <Button variant="outline" size="sm" onClick={() => void signOutOfEverything()}>
            Sign out
          </Button>
        </div>
      </header>

      <nav className="mb-8 flex gap-1 border-b">
        <Tab to="/" label="Needs you" count={waiting} />
        <Tab to="/breakdown" label="Breakdown" />
        <Tab to="/savings" label="Savings" />
        <Tab to="/debug" label="Debug" />
      </nav>

      <Routes>
        <Route
          path="/"
          element={
            <Inbox
              projectId={projectId}
              items={items}
              negotiations={negotiations}
              supplierName={supplierName}
            />
          }
        />
        <Route
          path="/breakdown"
          element={<Breakdown items={items} negotiations={negotiations} />}
        />
        <Route
          path="/savings"
          element={<Savings items={items} negotiations={negotiations} />}
        />
        <Route path="/debug" element={<DebugPanel />} />
      </Routes>
    </div>
  );
}

function Tab({ to, label, count }: { to: string; label: string; count?: number }) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        `-mb-px border-b-2 px-4 py-2 text-sm ${
          isActive
            ? "border-foreground font-medium"
            : "border-transparent text-muted-foreground hover:text-foreground"
        }`
      }
    >
      {label}
      {count !== undefined && count > 0 && (
        <span className="ml-2 rounded-full bg-foreground px-1.5 py-0.5 text-xs text-background">
          {count}
        </span>
      )}
    </NavLink>
  );
}

function Centred({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 px-6 text-center">
      {children}
    </div>
  );
}
