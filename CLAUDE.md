# CLAUDE.md

Operating rules for this repository. Read before writing code, and re-read
generated code against the Hard Rules before merging it. That is the review
that matters.

## What this is

Before a film shoots, someone sits down with the screenplay and reads it for
things. Not for story — for objects. A scene says *"he grabbed the cup and
threw it at the mirror"*, and the reader writes down: cup, mirror. The mirror
breaks, so make it several mirrors. That pass over the script is a real job on
a real production, and it is slow.

This system does that job, and then keeps going.

1. **Read the script.** A screenplay goes in. The agent finds every physical
   thing a scene needs, and records the line it found it in — so a producer can
   check the work rather than trust it.
2. **Research each item.** What does it cost, and who has one? The agent
   searches, and keeps the URLs it got its numbers from.
3. **Get it.** For anything that needs a person, the agent emails the seller
   and negotiates over real email, across real days.
4. **Stop.** It never buys. A human approves every purchase.

### The loop is real

This is the part that shapes every technical decision below. The agent is not
running a scripted scenario in a sandbox. It sends a real email to a real
seller, and then it *waits* — hours, days — because that is how long people take
to answer. The database is its memory across that gap. There is no process
sitting in RAM holding the conversation; there is a document in Firestore and a
tick that picks it back up.

Cloud Scheduler calls `/tick` every minute. Each tick reads the mailbox, files
any replies against their negotiation by Gmail thread ID, and acts on whatever
is due. Writing a reply to Firestore is what pushes it live to the UI, so the
screens update on their own.

It calls again in a minute whether or not the last one finished, so two ticks
overlapping is ordinary rather than exotic. That is why the tick claims each row
before it works on it — see Hard Rule 3.

A compressed replay — five days of negotiation in sixty seconds — is a **test
harness we will build later**, not the product. The clock supports it already
and that costs us nothing, but nothing in the system depends on it, and "it
works in demo mode" is never evidence that it works.

## How the pieces fit

```
screenplay ──> extract_props ──> research_item ──> negotiate over Gmail
                                                          │
                                                    READY_FOR_HUMAN
                                                          │
                                                   a person approves
                                                          │
                                                       ORDERED
```

## Ownership

| Path             | Owner   | Contents                                                     |
| ---------------- | ------- | ------------------------------------------------------------ |
| `contracts/`     | shared  | The A/B interface. Changes require both sides to agree.       |
| `main-agent/`    | Role A  | The brain. LLM reasoning only, as pure functions.             |
| `orchestrator/`  | Role B  | Clock, Firestore, Gmail, state machine, tick loop, approvals. |
| `supplier-sim/`  | Role B  | Adversary simulator. A later test fixture, not product code.  |
| `web/`           | Role B  | React + Vite + TypeScript front end.                          |

Role A implements the four Protocol signatures in `contracts/`. Role A does not
touch Firestore, Gmail, or the clock. Role B does not make LLM calls except
inside `supplier-sim/`.

## Hard Rules

These are not style preferences. Each one is load-bearing for a claim this
system makes.

- All LLM calls go through `google-genai` or `google-adk`. Never any other provider.
- Never call `datetime.now()` or `time.time()`. Time comes from `clock.now()`.
- No in-memory state between requests. Everything persists to Firestore.
  Any handler must be safe to kill mid-run and resume on the next tick.
- `purchase_orders` is created only via `create()` keyed by `item_id`.
  Never `set()`, never update, never delete.
- `purchase_orders` lives in its own Firestore database. The agent service
  account has no IAM binding on it, and the tick service never builds a client
  for it.

### Why each one exists

**Single LLM provider.** A competition rule, and the Google Cloud story is
stronger when the whole stack is one vendor.

**`clock.now()` only.** In live mode this reads 1:1 with real time, so the rule
can look like ceremony. It is not, for three reasons. Tests would otherwise
depend on how long they take to run, and a negotiation that spans days cannot
be tested by waiting days. The compressed replay harness needs a single place
to change speed, and retrofitting that means touching every file we have
written. And every stored `due_at` has to be measured against the same source
as every comparison, or a supplier looks silent when they are not. A CI guard
fails the build on `datetime.now()` / `time.time()` outside the clock module.

**No in-memory state.** This is the big one now that the loop is real. A
negotiation lives for days; a Cloud Run instance lives for minutes. The process
that sent the opening email is long gone by the time the reply lands, and the
process that reads the reply will be gone before the counter-offer is answered.
Nothing may be held between ticks that is not in Firestore. Kill any handler
halfway and the next tick must resume cleanly — not as a nicety, but because it
*will* happen, repeatedly, over a five-day negotiation.

Killable is only half of it, though. Cloud Scheduler does not wait for one tick
to finish before firing the next, so **claim a row before acting on it.** Every
due-queue handler pushes `next_action_due_at` forward with a conditional write
on Firestore's `update_time` before it calls the brain or sends anything. Two
ticks holding the same read both try; the storage engine admits exactly one.
Skip that and the second tick emails a supplier who has already been emailed —
which reads, from their inbox, exactly like an agent that pesters.

**`create()` keyed by `item_id`.** This is the guardrail, and it is enforced by
the storage engine rather than by prompt design. `create()` fails if the
document already exists, so a duplicate purchase order is refused before our
code runs. Because the *item* is the key, ordering the same item from two
suppliers is the same violation and is refused identically.

**No agent write access — and why that needs a second database.** "The agent
cannot spend money" has to be an IAM fact, not a claim about how well we wrote
the prompt. Getting there took more than a security rule, because of two things
about Firestore that only bite once deployed:

1. **Rules do not apply to server SDKs.** `firestore.rules` governs the Firebase
   *client* SDKs — a browser holding an Auth token. Anything going through
   `google-cloud-firestore` with a service account bypasses every rule in the
   file. A rule denying order writes constrains a producer's browser and
   constrains nothing whatsoever about the agent.
2. **Firestore IAM cannot see collections.** `roles/datastore.user` is all or
   nothing across an entire database. There is no binding that means "may write
   negotiations, may not write orders".

Together those make the claim unachievable in a single database. The smallest
thing IAM can name is a database, so orders got their own: the agent is granted
`roles/datastore.user` on `(default)` under an IAM condition, and has no binding
at all on `orders`. Verify it, do not trust it:

```
gcloud projects get-iam-policy $PROJECT --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:cinema-agent@$PROJECT.iam.gserviceaccount.com" \
  --format='table(bindings.role, bindings.condition.expression)'
```

In code the same line is drawn twice: `FirestoreRepository` has no method that
writes an order, and `OrdersRepository` is never constructed by the tick
service. Approval therefore lives somewhere else — `orchestrator/approvals.py`,
a second ASGI app with its own composition root, deployed as its own Cloud Run
service under the one account that does have a binding on `orders`. It cannot
be bolted onto the tick service without undoing all of this, and a test asserts
the tick app exposes no route matching `/approve` so that failure is loud.

What the rules files still do is govern the producer's browser: one order per
item, `approved_by` matching the caller, no updates and no deletes ever.
`make rules-test` executes both files against the emulator — the only thing in
the repo that does, since everything else reaches Firestore through the admin
SDK and bypasses them.

## Money and units

Money is always `{"amount": 880, "currency": "MYR"}`. Never a formatted string
like `"RM880"`, never a bare number. `amount` is an integer in the currency's
minor-unit-free major form (ringgit, not sen) unless the field name says
otherwise. Mixing currencies in an arithmetic operation raises.

## Time

Every timestamp written to Firestore is simulation time, from `clock.now()`.
Live mode — the product — runs 1:1. The compressed mode exists for the future
test harness and is not used by anything today. Same code path, one field
different.

Because the loop is real, **OAuth token lifetime is an operational concern, not
a demo-day chore.** A consent screen in testing mode issues refresh tokens that
die after seven days, which is shorter than a negotiation.

Publishing is not the way out, despite being the obvious one: `gmail.modify` is
a restricted scope, so publishing forces Google's verification plus a CASA
security assessment. Re-auth weekly is the strategy, not the fallback. See
`docs/oauth-runbook.md`.

## The stop condition

The agent stops at `READY_FOR_HUMAN`. Always. No exceptions, no config flag, no
"auto-approve under RM500." `ORDERED` is the only state that writes a purchase
order, and it is reachable only through the human-authenticated endpoint.

Confusing or unparseable supplier replies escalate to `READY_FOR_HUMAN` too. An
agent that guesses at an ambiguous quote is worse than one that asks.

## Not in scope yet

**Buying direct from online shops.** The agent will eventually be able to
source an item from a listing — good price, good reviews — instead of
negotiating with a person. It is parked. When it lands, a listing is just
another quote and it funnels into the same approval gate; do not add a second
path to money.

**The supplier simulator and the compressed replay.** Both are test
infrastructure for later. Nothing in the product may depend on either.

## Conventions

- Python 3.14, `uv` workspace. `ruff` and `basedpyright` configured at the root
  and inherited by every member package. Note that 3.14.0rc2 is broken for us —
  `fastapi` and `google-genai` fail to import on it. Use 3.14 final.
- Double quotes, 88 columns, LF endings.
- `pydantic` v2 models at every boundary, so bad data fails loudly and early.
- Tests run against the Firestore emulator, never a live project.

## The daily habit

Run the loop end to end every day, however broken, and push to your branch. Ten
minutes. With no stubs in the plan, this is the only thing keeping the two
halves from diverging. `make e2e` is that check; it must pass before any merge.
