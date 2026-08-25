# Role B build plan

The remaining work, in dependency order. Read `CLAUDE.md` first — it says what
the system is and which rules are load-bearing. This says what to build next.

## How to use this

Each phase has a **Done when** that is checkable by running something, not by
looking at code. If a phase cannot be closed that way, the phase is wrong.

Phases are ordered by dependency and by calendar, not by interest. Do not start
a later phase because an earlier one is boring; the ordering below exists
because of the constraint in the next section.

Update this file when reality disagrees with it. A plan nobody edits is a plan
nobody is following.

---

## The constraint that sets the order

**The loop is real, so calendar time is a dependency.**

A negotiation takes days because people take days to answer email. That is the
product working correctly, not a delay to engineer around. It also means a
negotiation started late simply does not finish.

Everything follows from that:

- Real Gmail and a deployed ticker come **early** (Phases 1 and 3), before the
  UI, because until they exist no simulated day is passing anywhere.
- The front half (Phase 2) sits between them, because a deployed ticker with no
  negotiations in the database has nothing to do.
- The UI comes after, because it renders state that must already be moving. A
  beautiful screen showing three hand-seeded rows proves nothing.

The one-line version: **get real negotiations in flight as early as possible,
then build the screens that watch them.**

---

## Where we are

Verified, not assumed — every claim below is covered by a test in the repo.

**Done**

| Area | State |
| --- | --- |
| Repo, uv workspace, ruff + basedpyright, `make check` | Green |
| `contracts/` — 4 signatures, 5 data shapes, `ScriptedBrain` | Done, needs Role A sign-off |
| `orchestrator/clock.py` — sim clock, live/demo/frozen | Done |
| `scripts/check_no_wallclock.py` — AST guard + its own tests | Done |
| `orchestrator/state_machine.py` | Done, `ORDERED` proven unreachable by agent |
| `orchestrator/records.py`, `repository.py` | Done, emulator-tested |
| `firestore.rules`, `firestore.orders.rules`, indexes | Done, both files executed by `make rules-test` |
| GCP setup script, two-database split | Run against the real project; both databases live |
| `orchestrator/mail.py` — transport seam + in-memory impl | Done |
| `orchestrator/tick.py` — the loop | Done, kill-mid-run tested |
| `scripts/run_e2e.py` / `make e2e` | Green, ends with 0 purchase orders |
| `orchestrator/auth.py`, `approvals.py`, `scripts/grant_producer.py` | Done, emulator-tested |
| `orchestrator/logs.py` — JSON for Cloud Logging | Done |
| Row claiming — overlapping ticks cannot double-email | Done, proven by a concurrent test |
| `Dockerfile` | Built by CI on every push, and by Cloud Build for the live deploy |
| `scripts/deploy.sh` | **Run.** Two Cloud Run services + Scheduler live on `encoded-phalanx-505503-v8` |
| `scripts/verify_deploy.sh` | **12 passed, 0 failed, 0 unknown** on the live project |
| `docs/deploy-runbook.md` | The order to do it in, and what each failure means |
| **Phase 1** — settings, Gmail transport, HTTP service, OAuth bootstrap, runbook, CI | Done |
| **Phase 2** — script upload, prop confirmation, research, negotiation creation | Done |
| **Phase 4** — auth, approval service, rules tests | Done |
| **Phase 3** — deploy | **Done and verified against real infrastructure** |
| **Phase 5** — instrument panel | **Done.** Live at encoded-phalanx-505503-v8.web.app, Google sign-in working |

229 Python tests, plus 27 rules tests in `web/`. CI fails the build if any
Python test skips, since a green run that skipped the guardrail tests is worse
than a red one — `make check` runs the same `test-all` target for that reason,
so a green laptop and a green CI mean the same thing.

**Not started**

`web/` has the read-only panel but none of Phase 6's screens · `supplier-sim/`,
scaffolding only (later) · the live Gmail round-trip, which needs the OAuth
bootstrap and two mailboxes.

**The deployed system is empty.** Everything runs; nothing has been uploaded.
The next milestone is a screenplay in Firestore so the Scheduler has work —
after that the loop advances on its own, into an in-memory mailbox until the
Gmail cutover.

**Known debts, carried deliberately**

- firebase-tools 15 does not load rules from the multi-database array form in
  `firebase.json` **for the emulator**, so the emulator runs open. Harmless: the
  Python tests use the admin SDK and bypass rules regardless, and the rules tests
  load each file explicitly by path rather than through `firebase.json`.
  Settled for real deploys — `firebase deploy --only firestore` against
  `encoded-phalanx-505503-v8` on 18 Aug compiled and released *both* files, so
  the array form is honoured there. The gap is emulator-only.
- ~~Nothing is deployed.~~ Both services, the Scheduler job and both databases
  are live on `encoded-phalanx-505503-v8`, and `make verify-deploy` reports
  12/0/0 — including the guardrail pair: the tick account reads `(default)`
  and is refused on `orders`.
- `/tick` is unauthenticated in code; the protection is deployment-side, and it
  is now real — an anonymous `POST /tick` against the live service returns 403,
  and only the Scheduler service account holds `run.invoker`.
- The live email round-trip is unproven — it needs two mailboxes and the OAuth
  bootstrap. The transport is covered offline; `docs/oauth-runbook.md` is the
  checklist.
- **The consent screen stays in Testing, permanently.** Publishing looks like
  the fix for seven-day refresh tokens and is not available to us:
  `gmail.modify` is a restricted scope, so publishing forces Google's
  verification plus a CASA security assessment, and "Internal" needs a Workspace
  org that a `@gmail.com` account does not have. Re-auth weekly is the strategy.
  The old runbook advised the opposite and cost an afternoon; it has been
  corrected.
- Contention on a hot item can abort one of two concurrent writes
  (`409 Transaction lock timeout` from Firestore). The work is not lost: the
  row is claimed, the error is reported, and the lease brings it back. Both
  halves have tests. If it ever becomes common rather than occasional, the
  answer is fewer overlapping ticks, not a retry loop.

---

## Invariants

These hold at the end of every phase. If a phase would break one, the phase is
wrong, not the invariant.

1. `make check` and `make e2e` both pass before any merge.
2. No wall-clock reads outside `orchestrator/clock.py`.
3. Every handler is safe to kill mid-run.
4. `purchase_orders` is only ever written by `create()` keyed by `item_id`,
   from a human-authenticated request.
5. The agent's service account cannot write `purchase_orders` at all.
6. The brain composes all email text; the orchestrator only addresses and sends.

---

## Phase 1 — Make it reachable, and make the mail real — DONE

**Goal.** A deployable HTTP service that sends and receives real Gmail.

**Shipped.** `settings.py`, `gmail.py` (transport + file/Secret-Manager token
stores), `app.py` (`GET /health`, `POST /tick`), `scripts/oauth_bootstrap.py`,
`docs/oauth-runbook.md`, and CI running the full gate on push.

Also fixed a latent bug found while building it: threading was keyed on Gmail's
API message id rather than the RFC-822 `Message-ID` header. Those are different
strings, and only the header threads — so every reply would have forked a new
thread in the supplier's inbox, invisibly, because our own routing uses Gmail's
thread id and would have kept working.

**Outstanding.** The live round-trip, which needs the mailboxes.

**Why here.** Nothing can run on a schedule until there is something to call,
and no simulated day passes until real email moves.

**Build**

- `orchestrator/settings.py` — `pydantic-settings`: GCP project, mailbox
  addresses, Secret Manager names, tick limit, clock mode.
- `orchestrator/app.py` — FastAPI. `GET /healthz`, `POST /tick` (returns the
  `TickReport`). Wire `FirestoreRepository`, `SimClock`, `GmailTransport` and
  the brain in one composition root; no globals.
- `orchestrator/gmail.py` — `GmailTransport` satisfying `MailTransport`:
  - `send()` builds RFC-2822, base64url-encodes it, sets `In-Reply-To` and
    `References` from `last_msg_id`, passes `threadId` when continuing.
  - `poll()` lists unread, reads `threadId` and `id`, detects attachments from
    the payload parts, then removes the `UNREAD` label so the next poll does
    not re-read it.
  - Returns `RawInbound` with **no timestamp** — the tick stamps it with
    `clock.now()`. Do not let the transport read a clock.
- `scripts/oauth_bootstrap.py` — run locally by a human; performs the consent
  flow for both mailboxes and writes refresh tokens to Secret Manager.
- `docs/oauth-runbook.md` — the file `CLAUDE.md` already points at.
- `.github/workflows/check.yml` — `make check` plus `make e2e` on every push.

**Done when.** A real email leaves the agent mailbox, a human replies from the
supplier mailbox, and a `POST /tick` files that reply against the right
negotiation by thread ID. `make e2e` still green.

**Watch out for**

- **Seven-day refresh tokens.** A consent screen in testing mode issues tokens
  that expire inside a negotiation's lifetime. Publish the consent screen, or
  put a calendar reminder on the re-auth. This is the single most likely way a
  live negotiation dies silently.
- Route inbound by `threadId` only. Suppliers rewrite subject lines.
- Poll, do not use Pub/Sub push — push needs a verified domain and a watch
  subscription renewed weekly, and buys nothing at one tick per minute.
- Unread-query polling is simple but re-reads anything a human opens in the
  mailbox. If that bites, move to `historyId` — but not before it bites.

**Blocked on.** Nothing from Role A. Needs a human to run the consent flow.

---

## Phase 2 — Close the front half: script to negotiations — DONE

**Goal.** An uploaded screenplay becomes live negotiations with no hand-seeding.

**Shipped.** `POST /projects`, `POST /projects/{id}/script`,
`POST /projects/{id}/items/confirm`, and `sourcing.py` running off a second
due-queue on items — the same killable pattern negotiations use, rather than a
background job with its own recovery story.

`run_e2e.py` now starts from a screenplay. It used to create items, suppliers
and negotiations itself, which is exactly why this gap stayed invisible: the
test worked around the missing piece and the pipeline looked complete.

Ids are derived rather than generated throughout — items from the prop name,
suppliers from the email, negotiations from the item-supplier pair. That makes
re-uploading a revised draft update rather than duplicate, and makes a tick
killed midway through opening negotiations collide with its own earlier writes
instead of emailing those sellers twice.

**Why here.** This is the biggest hole in the system right now. `run_e2e.py`
seeds items, suppliers and negotiations by hand because **nothing turns a
script into them.** Until this exists, the deployed ticker has no work.

**Build**

- `POST /projects` — create a project with an initial clock.
- `POST /projects/{pid}/script` — accept a screenplay, call
  `brain.extract_props()`, persist each `PropDraft` as an `ItemRecord` with
  `status=DRAFT`, carrying `mentions` and `consumable`.
- `POST /projects/{pid}/items/confirm` — the producer confirms the list and
  sets quantities. **Consumable props need a human here**: only they know how
  many takes the schedule allows. Nothing is researched or negotiated before
  this call.
- Research step — for each confirmed item, call `brain.research_item()`, store
  the `reference_band` and write a `SupplierRecord` per candidate.
- Negotiation creation — for each supplier with a usable address, write a
  `NegotiationRecord` in `DRAFTED`, due now. The existing tick loop takes it
  from there with no changes.

**Design decision to make here.** Research and negotiation-creation are
long-running and LLM-backed, so they must be killable like everything else. Give
`items` its own `next_action_due_at` and a second collection-group query in the
tick, mirroring negotiations exactly. One pattern, one index shape, one
recovery story — rather than a bespoke background job with its own failure mode.

**Done when.** Upload a script, confirm the props, run one tick, and opening
emails go out to researched suppliers. No hand-seeded documents anywhere.

**Blocked on.** Role A's real `extract_props` and `research_item`.
`ScriptedBrain` covers both until they land, so this phase is not gated.

---

## Phase 3 — Deploy, and start the clock for real — DONE

**Goal.** Cloud Scheduler ticking every minute against a real project, with a
real negotiation in flight.

**Why here.** See the constraint at the top. The day this ships is the day
elapsed time starts counting for us. Every day it slips is a day of negotiation
we do not get back.

**Shipped.** Row claiming in `repository.py`, `logs.py`, the `Dockerfile`, and
`scripts/deploy.sh`. Everything that can be proven without billing is proven;
the deploy is the only thing left, and it is waiting on an account link rather
than on code.

**The bug that turned out to be the point of this phase.** `/tick` was about to
be called every 60 seconds by something that does not wait for the previous
call to finish. Two overlapping ticks would both read the same due negotiation,
both ask the brain, and **both email the supplier** — indistinguishable, from
the seller's inbox, from the pestering bug already fixed in `tick.py`, and with
an entirely different cause.

Everything else in the loop was already safe by accident of design: filing a
reply, opening a negotiation and writing an order are all `create()` against a
derived key. Sending an email was the one uncovered path, and researching an
item was the one expensive one.

The fix is a compare-and-swap on Firestore's own `update_time`: both ticks hold
the same read, both attempt the conditional write, and the storage engine
admits exactly one. Same argument as the purchase-order guardrail — refused by
the database, not by our code — and it holds at any clock speed, so `DEMO` mode
needs no special case. The lease that comes with it is not what makes it safe;
it only bounds how long a row waits if the winner is then killed mid-work.

Proven by two ticks racing through an `asyncio.Barrier` so the race is certain
rather than probable, and by mutation: removing the precondition fails exactly
four tests.

**What the concurrency test taught us.** Its first version asserted that every
researched item is also *counted* as researched, and it failed one run in five
with `409 Transaction lock timeout` — Firestore aborting one of two genuinely
concurrent writes. That is not a test artifact and not something to retry
around. What the system owes under contention is not that every item succeeds,
but that none is silently dropped: each either completes or is reported, and a
reported one comes back when its lease expires. Both are now asserted.

**Done when — met.** `make verify-deploy` against
`encoded-phalanx-505503-v8`: **12 passed, 0 failed, 0 could not be determined**,
including the guardrail pair — the tick account reads `(default)` and is refused
on `orders`, so the denial is Firestore's rather than an artefact of missing
`serviceAccountTokenCreator`.

**What the first real run cost, and why it was worth it.** Four things that no
green test suite could have found: `/healthz` never reaches a Cloud Run
container (Google's front end answers it), a conditional IAM policy turns every
later `add-iam-policy-binding` interactive and a piped script appears to hang,
the guardrail check was calling a gcloud subcommand that does not exist, and a
tick with no projects logged nothing at all — making a live Scheduler
indistinguishable from a dead one. Each is fixed, with the reason recorded where
the next person will hit it.

**Build**

- `Dockerfile` for `orchestrator` — build from repo root so the `contracts`
  path dependency resolves.
- **Two** Cloud Run services from that one image, differing only in entrypoint
  and service account:
  - `orchestrator.app:app` as the tick account — `roles/datastore.user` on
    `(default)` and **nothing at all** on `orders`.
  - `orchestrator.approvals:app` as the approvals account — the only identity
    in the system with a binding on `orders`.
  Same image on purpose: one build, one set of dependencies, and the boundary
  is the IAM grant rather than a fork of the code. Getting the accounts the
  wrong way round is the one deployment mistake that silently undoes Phase 4,
  so verify with `gcloud projects get-iam-policy` after deploying.
- Cloud Scheduler → `POST /tick` every minute with OIDC auth. Nothing schedules
  the approval service; it is called by a person.
- Secret Manager for refresh tokens; service account with **no** `producer`
  claim.
- Deploy both rules files and `firestore.indexes.json`.
- Structured JSON logging of each `TickReport`.

**Watch out for**

- ~~Overlapping ticks.~~ Done — see above. Note the ordering that made it work:
  the claim goes *after* the terminal check, because claiming writes
  `next_action_due_at` and doing that to a finished negotiation would put it
  back into the queue `save_negotiation` deliberately drops it from.
- Tick timeout shorter than the Scheduler interval, so a wedged tick cannot
  pile up. `deploy.sh` sets `--timeout=50s` under a 60s schedule, which is only
  safe *because* of the claiming: a truncated tick leaves leased rows for the
  next pass rather than half-finished ones.
- Cold-start latency is irrelevant here — nothing is waiting on a response.
- **Getting the two service accounts the wrong way round.** The single
  deployment mistake that silently undoes Phase 4 — every test still passes and
  the guardrail is gone. `deploy.sh` ends by printing both IAM policies instead
  of reporting success, for exactly this reason.

**Done when.** You can leave it alone overnight and come back to a negotiation
that advanced without anyone touching it.

---

## Phase 4 — The money path — DONE

**Goal.** A human can approve a purchase, and nothing else can.

**Shipped.** `orchestrator/auth.py` (Firebase ID token + `producer` claim),
`orchestrator/approvals.py` (a second ASGI app with `approve`, `floor` and
`cancel`), `scripts/grant_producer.py`, and `web/tests/rules.test.ts` — the
first thing in the repo that has ever executed the rules files.

**Two things came out differently from the sketch above.**

*A separate app, not a separate account on the same app.* The plan said
approval "cannot live in the tick service" and left the shape open. It is now a
second `FastAPI()` instance with its own composition root, deployed as its own
Cloud Run service under its own account. `test_app.py` asserts the tick app
exposes no route matching `/approve` and holds no client for the orders
database, so the split fails the build rather than eroding quietly.

*Idempotency turned out to be the subtle part.* Approving writes to two
databases with no transaction across them, so the order goes first: a crash
between the writes leaves a real order and a negotiation still reading
`READY_FOR_HUMAN`, which a retry can finish. The other order would leave
something that looks bought and is not.

But the retry and the guardrail both arrive as `DuplicateOrderError` and mean
opposite things — same negotiation is a retry to complete, a different one is
the second-supplier violation to refuse. Conflating them would either break
retries or turn the guardrail into a shrug. Both directions have tests, and
removing the discrimination fails exactly the two guardrail tests and nothing
else.

**Done when — met.** `make rules-test` runs both rules files, and an approval
attempted by a signed-in identity with no `producer` claim is refused by
Firestore. Deliberately mutating three rules (letting a signed-in caller
create an order, reopening `update, delete`, and dropping the `ORDERED` guard
on negotiations) fails five tests, so the suite is load-bearing rather than
decorative.

**Not in this phase.** Deploying the approval service — it belongs with Phase
3's Cloud Run work, and `scripts/deploy.sh` now carries it. Until that runs, the
two-account split is real in code and in tests but there is one process running
both in development.

---

## Phase 5 — Instrument panel (UI pass 1)

**Goal.** See the loop run, without reading the Firestore console.

**Build.** React + Vite + TS, Firebase JS SDK, `onSnapshot` from the first
line. One read-only screen: negotiations with state, next due time, latest
quote, round count, message count, escalation reason. No design work at all.

`web/` already exists with `package.json`, `tsconfig.json` and vitest — Phase 4
put the rules tests there. Extend it; do not start a second npm project.

**Done when.** A tick changes the screen with no refresh and no polling code.

**Why it earns its place.** It is the debugging surface for Phases 2–4, it
proves listeners, auth and hosting early, and it becomes the timeline screen in
Phase 6 rather than being thrown away. Budget three hours; do not gold-plate it.

### Built — DONE against the emulator

`make e2e` fills the emulator, `make web-dev` serves the panel, and writing to
a negotiation document from outside the browser moves the row: state and
escalation reason changed with zero page loads. Driven headlessly to check
that rather than eyeballed.

Three things worth carrying forward:

- **A collection-group query is refused**, by the catch-all at
  `firestore.rules:86` — Firestore matches one against
  `/{path=**}/negotiations/{id}` and our rule is nested under
  `/projects/{projectId}/`. Asserted in `web/tests/rules.test.ts` rather than
  assumed, and the panel subscribes per project as a result. Phase 6 inherits
  this: no screen may query across projects without a rules change.
- **The web config is committed** in `web/src/firebase-config.ts`. A Firebase
  web API key is a public identifier, not a credential; hiding it would only
  break a fresh clone. The file says so.
- **Message counts refresh on the negotiation document's snapshot**, not on new
  messages, because a subcollection write does not fire the parent's listener.
  Fine while the tick writes both together.

### DONE — hosted, and signed into

Live at `https://encoded-phalanx-505503-v8.web.app`. The deployed bundle hash
matched a local rebuild byte for byte, so what is served is the committed
source.

Getting there took most of a day, all of it in Google's console rather than in
code, and every step of it is now written down in `docs/deploy-runbook.md`
sections 4 and 5. The three findings worth keeping:

- **A Google Cloud project is not a Firebase project.** Everything in Phase 3
  passes on one that was never added to Firebase — `deploy-rules` goes through
  plain GCP APIs. The first thing to need the Firebase resource was the web
  app, days later, failing with "404 project not found" about a project that
  plainly exists. `gcp_setup.sh` now does it.
- **`x-goog-user-project` is mandatory on Identity Toolkit calls.** Without it
  the 403 blames `SERVICE_DISABLED` on an unrelated project number, and
  enabling the API on the right project changes nothing.
- **Authentication must be initialised before a provider can be enabled**, and
  the Sign-in method tab does not exist until it is — which makes "enable
  Google" read as an instruction to a screen that is not there. Enabling the
  provider must happen in the console; the API refuses with `client_id cannot
  be empty` rather than provisioning an OAuth client.

`verify_deploy.sh` gained a sixth check for both auth facts, because it
reported 12 passed, 0 failed on a project where signing in was impossible.

### Seeding — done

`make seed PROJECT_ID=...` puts the same screenplay `make e2e` uses into a
deployed project, through the service's own endpoints. It refuses to run when
`mail_backend` is not `memory`.

### Still outstanding

- **Register a Firebase web app and paste its config in.** Until then
  `firebase-config.ts` holds placeholders and the panel runs against the
  emulator only — it throws something legible rather than failing deep in the
  SDK if pointed at production:

  ```bash
  firebase apps:create web "Greenlit" --project $PROJECT_ID
  firebase apps:sdkconfig web --project $PROJECT_ID
  ```

- **Enable the Google sign-in provider** — Firebase console → Authentication →
  Sign-in method → Google.
- Then `make deploy-web PROJECT_ID=...`. The hosted panel will show an empty
  table until a screenplay is uploaded to the deployed project, which is the
  correct result rather than a bug.

---

## Phase 6 — The product (UI pass 2)

**Goal.** The 25% of the score that is not engineering.

| Screen | Content |
| --- | --- |
| Breakdown | Item list with per-item status. Upload lives here. |
| Item detail | Quotes side by side, recommendation, the brain's reasoning, the reference band, **and the script lines the prop came from** |
| Timeline | Every message in and out with simulated timestamps — the proof that days passed |
| Approve | Accept, or set a floor and hand it back |
| Savings | First quote vs final accepted, summed. Measured, not claimed. |

**The design problem worth the most thought.** The floor-price handoff. A
producer is authorising an agent to spend days negotiating on their behalf,
against their money. Make the ceiling visible, the stop condition visible, and
make it obvious the agent comes back before anything is bought. This is the
interaction that proves it is an agent rather than a form, and it is a design
job rather than an engineering one.

**Watch out for.** Pick the stack and a design direction *before* generating
screens. Generated UI converges on one recognisable look, and "looks like every
other submission" is exactly what loses a criterion asking whether this feels
like a finished product.

---

## Phase 7 — Demo surface

- **Guardrail moment.** A control that attempts a duplicate order and is
  refused by the database, with the error visible on screen. Make it a beat,
  not a footnote — it is the most defensible thing the project does.
- **Escalation path.** When `extract_quote` returns an escalation or the brain
  stops, a human resolves it in the UI.
- **Judge mode.** A seeded account with negotiations already mid-flight, plus a
  "run five days in sixty seconds" control that needs no Gmail OAuth from the
  visitor.
- **Cold test.** Open the hosted URL in a browser that has never signed in, on
  a phone, on someone else's machine. This catches the credential that only
  exists on your laptop — the most common way a hackathon project dies on
  submission day.

---

## Later — explicitly parked

Nothing in the product may depend on any of these.

- **`supplier-sim/`.** Separate service, own mailbox, Gemini writing every
  reply, contact only over email. Five personas over `latency_hours`,
  `anchor_multiplier`, `floor_multiplier`, `writing_style`, `ghost_probability`
  — including one who buries the price in a PDF, which is the case
  `extract_quote` must escalate rather than guess at.
- **Compressed replay harness.** The clock already supports it; nothing uses it.
- **Buying direct from online shops.** When it lands, a listing is just another
  quote and it funnels into the same approval gate. Do not add a second path to
  money.

---

## The daily habit

Run `make e2e` and push, every day, however broken. Ten minutes.

With no stubs in the plan, this is the only thing keeping the two halves of the
codebase from diverging. Skip it and the first real integration happens late,
and the remaining days go on finding out whose code was wrong instead of
building.

---

## Open questions

- ~~**Role A sign-off on `contracts/`.**~~ Resolved, and it had already
  diverged: `GeminiAgentBrain` was built against `BreakdownSource`,
  `ItemDraft` and `parse_breakdown` while the contract said `ScriptSource`,
  `PropDraft` and `extract_props`. The other three signatures matched. The
  contract moved to his names — he had already built to them, and *breakdown*
  is the real film term. **BlueGecko should be told the contract moved toward
  him**, and he still owns two things Role B cannot decide: which Gemini model
  to default to, and whether `parallel-web` needs a key in the deployed
  environment.

### Merging `main-agent` — the two steps that need the directory to exist

Everything else is done: the rename, `settings.gemini_model`, `build_brain`
constructing `GeminiAgentBrain(model=…)` (covered by a test using a fake
module), `main-agent` listed as a workspace member, and `deploy.sh` passing
`CINEMA_BRAIN_BACKEND`. These two cannot land before the branches merge,
because both fail while `main-agent/` is absent:

1. **`orchestrator/pyproject.toml`** — declare `main-agent` as an optional
   dependency, or the image will not install it and selecting the real brain
   fails at startup. Adding it now breaks `uv sync` on this branch.
2. **`Dockerfile`** — `COPY main-agent/pyproject.toml main-agent/` in the
   dependency layer and `COPY main-agent/ main-agent/` in the source layer,
   mirroring `contracts`. Adding it now breaks `make image`, which CI runs.

Then: `BRAIN_BACKEND=main-agent ./scripts/deploy.sh`, **with mail still off**.
Read the emails it writes before turning the transport on — a real brain
producing plausible-but-wrong text is the failure the fake cannot show.
- ~~Who publishes the OAuth consent screen.~~ Answered, and the answer is
  nobody: `gmail.modify` is a restricted scope, so publishing forces Google's
  verification plus a CASA security assessment. The app stays in Testing and we
  re-auth weekly. What is still open is **who runs the bootstrap and owns the
  two mailboxes** — that is the last thing standing between us and a real
  negotiation.
- ~~Billing.~~ Linked. $100 of credit, and the per-minute tick sits inside the
  free tier; what will actually spend it is Role A's model calls.
