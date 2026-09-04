/**
 * The instrument panel.
 *
 * Read-only and undesigned on purpose. Its job is to make the loop visible —
 * the system has been ticking every minute in production with no way to watch
 * a negotiation move except the Firestore console. Phase 6 turns this into the
 * Timeline; until then it is the debugging surface for everything already
 * built.
 */

import { useState } from "react";

import { Spinner } from "@/components/ui/spinner";

import { useNegotiations, useProjects } from "../useNegotiations";
import type { Money, Negotiation } from "../useNegotiations";

export function DebugPanel() {
  const { ids, error: projectError } = useProjects();
  const [chosen, setChosen] = useState<string>("");

  // Whichever project exists, until someone picks another. An empty list is
  // the correct state for a deployment with no screenplay uploaded yet, not a
  // bug — so it says which of the two it is.
  const project = chosen !== "" && ids.includes(chosen) ? chosen : (ids[0] ?? "");
  const { rows, error, loading } = useNegotiations(project);

  if (projectError !== "") {
    return <p style={S.error}>{projectError}</p>;
  }

  if (ids.length === 0) {
    return <p>No projects. Upload a screenplay and this fills in.</p>;
  }

  return (
    <>
      <p>
        <label>
          Project{" "}
          <select
            onChange={(event) => setChosen(event.target.value)}
            style={S.button}
            value={project}
          >
            {ids.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>{" "}
        <span style={S.note}>
          {rows.length} negotiation{rows.length === 1 ? "" : "s"} · times are
          simulated, from the project clock
        </span>
      </p>

      {error !== "" && <p style={S.error}>{error}</p>}
      {loading && (
        <p style={{ display: "flex", alignItems: "center", gap: "0.5em" }}>
          <Spinner /> Subscribing…
        </p>
      )}

      <table style={S.table}>
        <thead>
          <tr>
            {[
              "Item",
              "Supplier",
              "State",
              "Latest quote",
              "Rounds",
              "Msgs",
              "Next due (sim)",
              "Last heard (sim)",
              "Escalation",
            ].map((head) => (
              <th key={head} style={S.th}>
                {head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Row key={row.id} row={row} />
          ))}
        </tbody>
      </table>
    </>
  );
}

function Row({ row }: { row: Negotiation }) {
  return (
    <tr>
      <td style={S.td}>{row.item_id ?? "—"}</td>
      <td style={S.td}>{row.supplier_id ?? "—"}</td>
      <td style={S.td}>{row.state ?? "—"}</td>
      <td style={S.td}>{money(row.latest_quote?.unit_price)}</td>
      <td style={S.td}>
        {row.rounds_used ?? 0}/{row.max_rounds ?? "?"}
      </td>
      <td style={S.td}>{row.messages ?? "…"}</td>
      <td style={S.td}>{simTime(row.next_action_due_at)}</td>
      <td style={S.td}>{simTime(row.last_inbound_at)}</td>
      <td style={S.td}>{row.escalation_reason ?? ""}</td>
    </tr>
  );
}

/**
 * Never a formatted string from the database — money is stored as an amount
 * and a currency, and the formatting happens here so a mixed-currency total
 * cannot be faked by string concatenation somewhere upstream.
 */
const money = (value: Money | undefined): string =>
  value === undefined ? "—" : `${value.currency} ${value.amount}`;

/**
 * Simulated time, and labelled as such in the column head.
 *
 * Rendered in UTC rather than the browser's zone deliberately: every timestamp
 * written to Firestore comes from `clock.now()`, and quietly shifting it into
 * local time would make a five-day negotiation look like it happened at hours
 * nobody worked.
 */
const simTime = (value: { toDate: () => Date } | undefined): string => {
  if (value === undefined) {
    return "—";
  }
  return value.toDate().toISOString().slice(0, 16).replace("T", " ");
};

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
  badge: { color: "#a60", fontWeight: 400 },
  note: { color: "#666" },
  error: { color: "#c00", margin: "0.5rem 0" },
  table: { borderCollapse: "collapse", width: "100%" } as const,
  th: {
    borderBottom: "1px solid #999",
    padding: "0.35rem 0.6rem",
    textAlign: "left",
    whiteSpace: "nowrap",
  } as const,
  td: {
    borderBottom: "1px solid #eee",
    padding: "0.35rem 0.6rem",
    whiteSpace: "nowrap",
  } as const,
  button: { cursor: "pointer", font: "inherit", padding: "0.25rem 0.75rem" },
} as const;
