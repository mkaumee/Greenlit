# Agentic Cinema

An agentic procurement system for film production.

Before a shoot, someone reads the screenplay looking for objects. A scene says
*"he grabbed the cup and threw it at the mirror"*, and they write down: cup,
mirror — and several mirrors, because it breaks. This system does that pass,
then researches what each item costs, finds who sells it, and negotiates over
real email across real days.

It stops before spending money. Every purchase order is created by a human, and
that limit is enforced by database rules rather than by prompt design.

## Layout

```
contracts/       The Role A / Role B interface. Both sides import it.
main-agent/      Role A. The brain: four LLM-backed functions, no side effects.
orchestrator/    Role B. Clock, Firestore, Gmail, state machine, tick loop.
supplier-sim/    Role B. Adversary simulator. Own mailbox, email only.
web/             Role B. React + Vite + TypeScript, live on Firestore snapshots.
scripts/         OAuth flow, seeding, the wall-clock guard.
docs/            Runbooks.
```

## Getting started

```bash
uv sync                      # Python 3.14 workspace, all members
uv run pytest                # unit tests, no cloud access needed
uv run ruff check .          # lint
uv run basedpyright          # types
```

Everything above runs offline. Nothing in the test suite touches a live GCP
project — Firestore tests run against the emulator.

## How the pieces fit

Cloud Scheduler calls `/tick` every minute. Each tick reads the mailbox, files
replies against their negotiation by Gmail thread ID, and acts on whatever is
due. Writing a reply to Firestore is what pushes it live to the UI, so the
screens update on their own.

The loop is real. A negotiation runs for days because that is how long people
take to answer email, and Firestore is the agent's memory across that gap — no
process stays alive between an outbound message and the reply. `clock.now()` is
the only source of time, which is what makes a multi-day negotiation testable in
milliseconds. See `CLAUDE.md` for why that is a hard rule rather than a
preference.

## The rules that shape the code

`CLAUDE.md` holds five constraints that are load-bearing for what this system
claims. The short version:

- One LLM provider, `google-genai` / `google-adk`.
- No wall-clock time, anywhere. `clock.now()` only.
- No in-memory state between requests. Any handler is safe to kill mid-run.
- `purchase_orders` is created with `create()` keyed by `item_id`, never
  updated, never deleted — so a duplicate order is refused by the storage
  engine before application code runs.
- The agent's service account cannot write to `purchase_orders` at all.

## What happens next

`docs/PLAN.md` holds the phased build plan: what is done, what is not, and
the order the rest goes in.

## Licence

MIT. See `LICENSE`.
