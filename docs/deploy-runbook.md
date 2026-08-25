# Deploy runbook

Getting the loop onto Cloud Run and ticking on its own. Run by a human, in
**Cloud Shell** — gcloud is already there and authenticated, and the image is
built by Cloud Build so no Docker daemon is needed.

Budget half an hour for the first run, most of it waiting for the first build.

## Before you start

- Billing is linked. Cloud Run and Scheduler simply refuse to exist without it,
  unlike Firestore — which is what lets an unbilled project look half-working.
- `scripts/gcp_setup.sh` has been run **to completion**, and there are two
  Firestore databases:

  ```bash
  gcloud firestore databases list --format='table(name,locationId,type)'
  ```

  Check this rather than assume it. `firebase deploy` will create `(default)`
  on its own if it is missing, which makes a half-set-up project look set up —
  and the guardrail is that `orders` is a *separate* database the agent has no
  binding on. One database means no guardrail, and nothing about a successful
  rules deploy would tell you.

- **The project is a Firebase project**, not only a Google Cloud one:

  ```bash
  firebase projects:list | grep your-project-id
  ```

  These are two different things, and the difference stays invisible for a
  long time. Every step in this runbook works on a project that was never
  added to Firebase: `make deploy-rules` goes through
  `firebaserules.googleapis.com` and the Firestore admin API, both plain GCP
  APIs. So the project deploys, ticks, negotiates and passes
  `verify_deploy.sh` — and then registering the web app for the panel fails,
  days later, with

  ```
  404 Firebase project 678371873554 not found.
  ```

  about a project that plainly exists. Enabling `firebase.googleapis.com`
  does not fix it; the API being on and the Firebase resource existing are
  different things.

  `scripts/gcp_setup.sh` now does this, so a project set up from scratch is
  fine. For one that predates that, `firebase projects:addfirebase
  your-project-id` adds it — and if it refuses, **read the message it prints
  rather than guessing**. The script relays Google's own error for the same
  reason.

  The CLI cannot always finish this job. Observed on this project:
  `:addFirebase` returned **403 PERMISSION_DENIED** to an account holding
  `roles/owner`, authorising as the right user, with a freshly minted token
  carrying the `firebase` scope — every IAM explanation ruled out by the log
  itself. Accepting Firebase's terms of service is a per-account step that no
  command-line tool can present, and the API refuses until it has happened.
  When you see that 403, stop fighting it: [console.firebase.google.com](https://console.firebase.google.com)
  → **Add project** → pick the **existing** project from the dropdown rather
  than creating a new one, accepting the terms if prompted. Two clicks, and it
  handles the cases the CLI cannot.

## The order matters

Two steps are easy to do too late, and both fail in ways that point somewhere
else.

### 1. Rules and indexes go first

```bash
make deploy-rules PROJECT_ID=your-project-id
```

`firestore.indexes.json` carries the collection-group entries for
`next_action_due_at` as **field overrides**. Firestore auto-creates ordinary
single-field indexes but not collection-group ones, and `due_negotiations()` is
a collection-group query.

Skip this and every tick fails with `FAILED_PRECONDITION`. The failure is caught
per project, so `/tick` returns 200 with an error inside the body and the
service looks perfectly healthy. You would be debugging the wrong thing.

### 2. Deploy with mail off

```bash
make deploy PROJECT_ID=your-project-id
```

`MAIL_BACKEND` defaults to `memory` on purpose. `build_services()` constructs
the mail transport at startup, so deploying with `gmail` before a refresh token
exists in Secret Manager gives you a container that raises during startup, a
revision that never goes ready, and a deploy that fails at the last and most
expensive step.

A deploy with mail off is not a half-deploy. The loop ticks, researches, opens
negotiations and drives the state machine — it just posts into an in-memory
mailbox. Everything except the sending is exercised.

### 3. Verify — this is what "deployed" means

```bash
make verify-deploy PROJECT_ID=your-project-id
```

`make deploy` exiting 0 means its gcloud calls succeeded. It says nothing about
whether the tick account can still reach the orders database, because the
guardrail is an **absence** and nothing about a successful deploy tests an
absence.

The verifier reports `PASS`, `FAIL` or `????`, and exits non-zero on either of
the last two. `????` is deliberate: the guardrail check needs to impersonate the
tick service account, and impersonation *itself* fails with `PERMISSION_DENIED`
when you lack `roles/iam.serviceAccountTokenCreator`. That looks identical to
the guardrail working. If you see `????` on the guardrail, grant yourself the
right and run it again:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  cinema-agent@your-project-id.iam.gserviceaccount.com \
  --member="user:$(gcloud config get-value account)" \
  --role=roles/iam.serviceAccountTokenCreator
```

Do not read `????` as a pass. An unchecked guardrail is not a working guardrail.

### 4. Switch on Authentication, or nobody can open the panel

The web panel reads Firestore from the browser, and `firestore.rules` gates
every read behind `isSignedIn()`. Until Authentication exists and Google
sign-in is enabled, the deployed page loads and then refuses every visitor.
`make verify-deploy` checks both, because the first time this was missed the
verifier reported 12 passed, 0 failed on a project where signing in was
impossible.

There is **no `firebase` command for this** — the CLI does not expose
Authentication setup at all. Two routes, and the order matters because the
second is impossible before the first:

1. **Initialise Authentication.** Firebase console → Authentication → **Get
   started**. That button is the whole step, and until it is pressed there is
   no Sign-in method tab — which makes "enable the Google provider" look like
   an instruction to a screen that does not exist. By API:

   ```bash
   P=your-project-id
   curl -sS -X POST -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "x-goog-user-project: $P" -H "Content-Type: application/json" -d '{}' \
        "https://identitytoolkit.googleapis.com/v2/projects/$P/identityPlatform:initializeAuth"
   ```

   An empty `{}` back means it worked.

2. **Enable Google.** Console → Authentication → Sign-in method → Google →
   Enable → support email → Save. **Do this in the console rather than by
   API.** The API route refuses with `INVALID_CONFIG : client_id cannot be
   empty` because it will not provision an OAuth client, while the console
   creates one as part of enabling the provider.

Two traps, both of which cost real time here:

- **`x-goog-user-project` is mandatory on every Identity Toolkit call.**
  `gcloud auth print-access-token` returns a bare user token with no project
  attached. Firestore and Cloud Run infer one; Identity Toolkit does not.
  Without the header the 403 reads `SERVICE_DISABLED` against a project number
  you have never seen, and enabling the API on the right project changes
  nothing.
- **`auth/configuration-not-found` and `auth/operation-not-allowed` are
  different errors.** The first means step 1 has not been done and the provider
  screen does not exist yet; the second means it does and Google is switched
  off. The panel spells both out on screen rather than showing the bare code.

### 5. Seed a project, or the panel is an empty table

A correct deployment with nothing in it looks broken to everyone who opens it.
`make e2e` fills the *emulator*; this fills a deployed project:

```bash
make seed PROJECT_ID=your-project-id ARGS=--dry-run   # see the plan first
make seed PROJECT_ID=your-project-id
```

It goes through the deployed service's own `POST /projects`,
`/script` and `/items/confirm` rather than writing to Firestore, so a
successful seed is also evidence the front half of the service works.

Nothing needs ticking afterwards. Cloud Scheduler is already calling `/tick`
every minute, so the loop researches the items and opens negotiations on its
own, and the panel fills in with nobody touching the browser.

**It refuses to run when `mail_backend` is not `memory`.** Seeding a
deployment configured for real Gmail would set the agent emailing addresses
that research invented, from a screenplay, with nobody expecting it.
`--allow-live-mail` overrides that, and should be used only when a real
round-trip is the point.

### 5b. The panel is built against one approvals service

`make deploy-web` looks up the `cinema-approvals` URL and inlines it into the
bundle, because Vite substitutes `import.meta.env` at build time rather than
reading it in the browser. Two consequences worth knowing:

- **Deploy the services before the panel.** `make deploy-web` fails rather than
  publishing if it cannot find the service — a panel whose only irreversible
  action is inert is worse than no panel.
- **Re-deploy the panel if the approvals URL ever changes.** It will not pick
  up a new one on its own.

Approving also needs the signed-in account to carry the `producer` claim, or
every attempt is a 403 by design:

```bash
uv run python scripts/grant_producer.py you@example.com
```

### 6. Turn real email on, once the token exists

Only after `docs/oauth-runbook.md` is done and the token is in Secret Manager:

```bash
CINEMA_TOKEN_BACKEND=secret-manager CINEMA_GCP_PROJECT=your-project-id \
  uv run python scripts/oauth_bootstrap.py
```

That backend variable is not optional. Without it the token lands in a local
gitignored file that the deployed service cannot read, the bootstrap reports
success, and the flip below fails with a confusing `NotFound`.

Then:

```bash
CINEMA_OAUTH_CLIENT_ID=...apps.googleusercontent.com \
CINEMA_OAUTH_CLIENT_SECRET=... \
CINEMA_AGENT_EMAIL=producer-agent@example.com \
MAIL_BACKEND=gmail make deploy PROJECT_ID=your-project-id
```

The client id and secret matter as much as the token. Without them
`build_credentials()` builds a credential with an empty client, the service
starts happily, and the first send fails with `invalid_client` — a failure that
looks like a Gmail problem and is not.

`deploy.sh` preflights all three before changing anything, so a missing piece is
one line of English rather than a failed revision.

## What it costs

The loop itself is not what will spend the credit.

| | |
| --- | --- |
| Cloud Run | `--min-instances=0`, so nothing runs between ticks. 43k invocations a month against a 2M free tier. |
| Scheduler | One job. Three are free. |
| Firestore | A few reads per tick, ~7k/day against a 50k/day free tier. |
| Artifact Registry | One image, ~200MB, against 0.5GB free. |

What *will* spend it is Role A's model calls once the real brain is wired. Keep
`--max-instances=2` — it is there so a wedged tick cannot fan out.

## When it is working

```bash
gcloud logging read \
  'resource.labels.service_name="cinema-tick" jsonPayload.message="tick"' \
  --limit=20 --format='value(jsonPayload)'
```

One JSON object per minute, each with the tick's counters as fields. That is the
thing to look at the morning after — `messages_sent`, `claims_lost`,
`error_count` — rather than whether the service is up.

## When something is wrong

| Symptom | Cause |
| --- | --- |
| Revision never goes ready | Almost always mail. Check `MAIL_BACKEND` and whether the secret has a version. |
| `/tick` returns 200 but every project has an error | Indexes. Run `make deploy-rules`. |
| `FAILED_PRECONDITION` with an index-creation link | Same, and the link builds it by hand if you are in a hurry. |
| Cloud Build fails on push permissions | The build account needs `artifactregistry.writer`. `deploy.sh` grants it; if you built by hand, it did not. |
| A script hangs with no output during an IAM step | gcloud is prompting for a condition. Once the project policy holds any conditional binding — ours does, the agent's scoped `datastore.user` — an unconditioned `add-iam-policy-binding` becomes interactive. Every call in our scripts passes `--condition=None`; if you are running one by hand, add it. |
| `provisioning for project is taking too long` creating the Artifact Registry repo | First-use provisioning, and transient. Re-run `make deploy` — it is idempotent and picks up where it stopped. Seen on the very first deploy to a fresh project. |
| `cinema-agent` shows no roles at the end of a deploy | `gcp_setup.sh` has not run. Every tick will fail `PERMISSION_DENIED`, and the guardrail will look like it holds because the agent cannot reach anything. Run `make gcp-setup`. |
| `invalid_grant` after about a week | The testing-mode token expired. Expected. Re-run the bootstrap. |
| `invalid_client` on the first send | `CINEMA_OAUTH_CLIENT_ID` / `_SECRET` are not set on the service. |
| Anonymous `POST /tick` returns 200 | The tick is public. It should be `--no-allow-unauthenticated`; redeploy. |
| An HTML 404 with a robot picture and "That's all we know" | Google's front end, not our app — the request never reached the container. Check ingress first: `gcloud run services describe <svc> --region=us-central1 --format='value(spec.template.metadata.annotations)'`. An `internal` ingress 404s rather than 403s, deliberately, so it does not leak that the service exists. |
| A JSON 404 (`{"detail":"Not Found"}`) | Our app answered, so the container is fine and the route is wrong. |
| `/healthz` returns an HTML 404 while everything else works | Not our bug to fix — Google's front end swallows that path on Cloud Run. The health endpoint is `/health` for exactly this reason; do not rename it back. Proven on the deployed service: `/openapi.json` returned 200 listing `/healthz` as a registered route, an unknown path returned our own JSON 404, and `/healthz` alone returned Google's HTML. |
| Verifier says the tick account can read `orders` | The service accounts are the wrong way round. This is the one that silently undoes Phase 4 — fix before anything else. |
| Verifier reports a Firestore denial mentioning `Invalid choice` | Not a denial — a gcloud subcommand that does not exist, exiting non-zero. The guardrail check is testing nothing. Fixed once already (`gcloud firestore documents list` was invented); if it recurs, confirm the command exists before trusting the result. |
