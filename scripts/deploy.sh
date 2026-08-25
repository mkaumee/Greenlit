#!/usr/bin/env bash
#
# Deploy the two Cloud Run services and the Scheduler job. Run by a human,
# after scripts/gcp_setup.sh. Needs gcloud and git; deliberately not Docker.
#
#   PROJECT_ID=your-project-id ./scripts/deploy.sh
#
# Cloud Shell is the intended home: gcloud is already there and authenticated,
# and the image is built by Cloud Build rather than a local daemon.
#
# Idempotent, in the same style as gcp_setup.sh: every step checks before it
# creates, and re-running after a failure is safe.
#
# ---------------------------------------------------------------------------
# Run against encoded-phalanx-505503-v8 on 21 Aug 2026, verified 12/0/0
# ---------------------------------------------------------------------------
#
# Written on a machine with neither gcloud nor a Docker daemon, so the first
# real run found things. Kept here because they are the failures a second
# project would hit too:
#
#   · Artifact Registry provisioning timed out on first use. Transient; the
#     re-run picked up where it stopped, which is what idempotency is for.
#   · Once the project policy held the agent's conditioned binding, every
#     unconditioned add-iam-policy-binding became interactive. Piped to
#     /dev/null that prompt is invisible and the script looks hung. Hence
#     --condition=None on every one of them.
#   · /healthz never reached the container — Google's front end answers it
#     with its own 404 on Cloud Run. The health route is /health for that
#     reason. See the note on the route in app.py.
#
# A clean run is still the beginning of the check, not the end of it. What
# decides whether the deploy is correct is scripts/verify_deploy.sh — in
# particular whether the tick account can still reach the orders database,
# which no amount of green output here can tell you.
#
# ---------------------------------------------------------------------------
# Mail is off unless you ask for it
# ---------------------------------------------------------------------------
#
# MAIL_BACKEND defaults to `memory`. That is not timidity: build_services()
# constructs the mail transport at startup, so deploying with `gmail` before a
# refresh token exists in Secret Manager means the container raises during
# startup and the Cloud Run health check never passes. A deploy with no mail
# configured is a perfectly useful deploy — the loop ticks, researches and opens
# negotiations, it just posts into an in-memory mailbox.
#
# Turn it on afterwards, once oauth_bootstrap.py has written a token:
#
#   MAIL_BACKEND=gmail PROJECT_ID=... ./scripts/deploy.sh
#
# which preflights the secret and the OAuth client before touching anything.
#
# ---------------------------------------------------------------------------
# The thing this script must not get wrong
# ---------------------------------------------------------------------------
#
# Two services, one image, two service accounts:
#
#   tick       orchestrator.app:app        cinema-agent
#              roles/datastore.user on (default), CONDITIONED
#              no binding whatsoever on `orders`
#
#   approvals  orchestrator.approvals:app  cinema-approvals
#              roles/datastore.user on (default) AND on `orders`
#
# The approvals account needs both because approving writes the purchase order
# in one database and the negotiation transition in the other. The tick account
# needs exactly one, and the absence of the second *is* Hard Rule 5 — it is what
# makes "the agent cannot spend money" a fact about IAM rather than a promise
# about our code.
#
# Swapping those two accounts is the single deployment mistake that silently
# undoes all of Phase 4: every test still passes, the demo still works, and the
# guardrail is gone. That is why this script ends by printing the real IAM
# policy for both accounts instead of telling you it succeeded.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-cinema}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

AGENT_SA="${AGENT_SA:-cinema-agent}"
APPROVALS_SA="${APPROVALS_SA:-cinema-approvals}"
SCHEDULER_SA="${SCHEDULER_SA:-cinema-scheduler}"
ORDERS_DB="${ORDERS_DB:-orders}"
TOKEN_SECRET="${TOKEN_SECRET:-gmail-agent-refresh-token}"

TICK_SERVICE="${TICK_SERVICE:-cinema-tick}"
APPROVALS_SERVICE="${APPROVALS_SERVICE:-cinema-approvals}"

# `memory` unless explicitly asked otherwise. See the header.
MAIL_BACKEND="${MAIL_BACKEND:-memory}"
OAUTH_CLIENT_ID="${CINEMA_OAUTH_CLIENT_ID:-}"
OAUTH_CLIENT_SECRET="${CINEMA_OAUTH_CLIENT_SECRET:-}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID is not set." >&2
  echo "  PROJECT_ID=your-project-id $0" >&2
  exit 2
fi

# No docker. The image is built by Cloud Build, which is what Cloud Run does
# internally anyway — and it means this runs in Cloud Shell, where there is
# gcloud and no daemon.
for tool in gcloud git; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed." >&2; exit 2; }
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
skip() { printf '  \033[90m·\033[0m %s (already there)\n' "$*"; }

AGENT_EMAIL="${AGENT_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
APPROVALS_EMAIL="${APPROVALS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_EMAIL="${SCHEDULER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/orchestrator:${IMAGE_TAG}"

gcloud config set project "$PROJECT_ID" >/dev/null 2>&1

# ---------------------------------------------------------------------------
say "Preflight — billing"
# ---------------------------------------------------------------------------
#
# Cloud Run and Scheduler simply refuse to exist without it, unlike Firestore,
# which is why gcp_setup.sh can get most of the way on an unbilled project and
# this script cannot get anywhere at all.

billing=$(gcloud billing projects describe "$PROJECT_ID" \
  --format='value(billingEnabled)' 2>/dev/null || echo unknown)
if [[ "$billing" != "True" && "$billing" != "true" ]]; then
  cat >&2 <<EOF
  ✗ no billing account linked to $PROJECT_ID (read as: $billing)

  Everything below needs it. See the billing section of scripts/gcp_setup.sh:

    gcloud billing accounts list
    gcloud billing projects link $PROJECT_ID --billing-account=ACCOUNT_ID

  Nothing has been changed.
EOF
  exit 4
fi
ok "billing is linked"

# ---------------------------------------------------------------------------
say "Preflight — mail"
# ---------------------------------------------------------------------------
#
# The one failure this script exists to prevent. build_services() constructs the
# mail transport during startup, so `gmail` with nothing behind it is not a
# degraded service — it is a container that raises before it can serve
# /health, a Cloud Run revision that never goes ready, and a deploy that fails
# at the last and most expensive step. Every condition is checkable up front, so
# check it up front.

case "$MAIL_BACKEND" in
  memory)
    ok "mail is off (MAIL_BACKEND=memory) — nothing will email a real seller"
    ;;
  gmail)
    problems=()
    versions=$(gcloud secrets versions list "$TOKEN_SECRET" \
      --filter='state:ENABLED' --format='value(name)' 2>/dev/null | wc -l | tr -d ' ')
    [[ "$versions" -gt 0 ]] \
      || problems+=("secret '$TOKEN_SECRET' has no enabled version — the refresh token has not been bootstrapped into it")
    [[ -n "$OAUTH_CLIENT_ID" ]] \
      || problems+=("CINEMA_OAUTH_CLIENT_ID is not set — token refresh fails with invalid_client")
    [[ -n "$OAUTH_CLIENT_SECRET" ]] \
      || problems+=("CINEMA_OAUTH_CLIENT_SECRET is not set")

    if (( ${#problems[@]} > 0 )); then
      printf '  \033[31m✗\033[0m %s\n' "${problems[@]}" >&2
      cat >&2 <<EOF

  MAIL_BACKEND=gmail cannot work yet, and deploying it would produce a service
  that fails its health check rather than one that runs without email.

  Either deploy without mail first — the loop still ticks and negotiates:

    PROJECT_ID=$PROJECT_ID $0

  or finish the Gmail side first (docs/oauth-runbook.md):

    CINEMA_TOKEN_BACKEND=secret-manager CINEMA_GCP_PROJECT=$PROJECT_ID \\
      uv run python scripts/oauth_bootstrap.py

  Nothing has been changed.
EOF
      exit 5
    fi
    ok "refresh token present ($versions version(s)) and OAuth client configured"
    printf '  \033[33m!\033[0m this deploy WILL email real sellers once it ticks\n'
    ;;
  *)
    echo "  ✗ MAIL_BACKEND must be 'memory' or 'gmail', got '$MAIL_BACKEND'" >&2
    exit 2
    ;;
esac

# ---------------------------------------------------------------------------
say "APIs"
# ---------------------------------------------------------------------------

enabled=$(gcloud services list --enabled --format='value(config.name)')
for api in run.googleapis.com cloudscheduler.googleapis.com \
           artifactregistry.googleapis.com cloudbuild.googleapis.com \
           secretmanager.googleapis.com iamcredentials.googleapis.com; do
  if grep -qx "$api" <<<"$enabled"; then skip "$api"; else
    gcloud services enable "$api" >/dev/null && ok "$api"
  fi
done

# ---------------------------------------------------------------------------
say "Service accounts"
# ---------------------------------------------------------------------------

ensure_sa() {
  local name="$1" email="$2" desc="$3"
  if gcloud iam service-accounts describe "$email" >/dev/null 2>&1; then
    skip "$email"
  else
    gcloud iam service-accounts create "$name" --description="$desc" \
      --display-name="$name" >/dev/null
    ok "$email"
  fi
}

ensure_sa "$AGENT_SA" "$AGENT_EMAIL" \
  "Runs the tick loop. Deliberately cannot write purchase orders."
ensure_sa "$APPROVALS_SA" "$APPROVALS_EMAIL" \
  "Runs the approval service. The only identity that may write an order."
ensure_sa "$SCHEDULER_SA" "$SCHEDULER_EMAIL" \
  "Mints the OIDC token Cloud Scheduler calls /tick with."

# ---------------------------------------------------------------------------
say "IAM"
# ---------------------------------------------------------------------------
#
# The agent's conditioned binding is created by gcp_setup.sh; this only adds
# the approvals account, which is unconditioned because it genuinely needs both
# databases.

has_role() {
  gcloud projects get-iam-policy "$PROJECT_ID" \
    --flatten='bindings[].members' \
    --filter="bindings.members:serviceAccount:$1 AND bindings.role:$2" \
    --format='value(bindings.role)' | grep -q .
}

if has_role "$APPROVALS_EMAIL" "roles/datastore.user"; then
  skip "datastore.user for $APPROVALS_SA"
else
  # --condition=None is not decoration. Once a policy holds any conditional
  # binding — and ours does, the agent's scoped datastore.user — gcloud refuses
  # to add an unconditioned one non-interactively and prompts for a choice.
  # With stdout piped to /dev/null that prompt is invisible and the script
  # simply appears to hang.
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${APPROVALS_EMAIL}" \
    --role="roles/datastore.user" \
    --condition=None >/dev/null
  ok "datastore.user for $APPROVALS_SA — both databases, deliberately"
fi

# Reading the token secret is the tick service's business only. The approvals
# service never sends mail.
if gcloud secrets describe "$TOKEN_SECRET" >/dev/null 2>&1; then
  if gcloud secrets get-iam-policy "$TOKEN_SECRET" --format=json \
       | grep -q "$AGENT_EMAIL"; then
    skip "secretAccessor on $TOKEN_SECRET"
  else
    gcloud secrets add-iam-policy-binding "$TOKEN_SECRET" \
      --member="serviceAccount:${AGENT_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
    ok "secretAccessor on $TOKEN_SECRET"
  fi
else
  printf '  \033[33m?\033[0m secret %s does not exist — run gcp_setup.sh\n' \
    "$TOKEN_SECRET"
fi

# ---------------------------------------------------------------------------
say "Image"
# ---------------------------------------------------------------------------

if gcloud artifacts repositories describe "$REPO" \
     --location="$REGION" >/dev/null 2>&1; then
  skip "artifact registry $REPO"
else
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location="$REGION" \
    --description="Agentic Cinema images" >/dev/null
  ok "artifact registry $REPO"
fi

# Since Google's 2024 Cloud Build service-account change, builds on newer
# projects run as the Compute Engine default service account rather than the
# legacy cloudbuild one, and that account does not get push or logging rights by
# default. The failure is a permission error deep in a build log, which is a
# miserable thing to diagnose, so grant both up front. Idempotent.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for role in roles/artifactregistry.writer roles/logging.logWriter; do
  if has_role "$BUILD_SA" "$role"; then
    skip "$role for the build account"
  else
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${BUILD_SA}" --role="$role" \
      --condition=None >/dev/null
    ok "$role for the build account"
  fi
done

# Context is the repository root: `contracts` is a uv workspace path dependency
# and a context of orchestrator/ cannot see it. See the Dockerfile.
#
# What actually leaves this machine is decided by .gcloudignore, which exists so
# that is an explicit list rather than whatever .gitignore happens to say.
say "Building — this takes a few minutes the first time"
gcloud builds submit "$(git rev-parse --show-toplevel)" \
  --tag="$IMAGE" --quiet
ok "$IMAGE"

# ---------------------------------------------------------------------------
say "Cloud Run"
# ---------------------------------------------------------------------------

# Assembled rather than inlined because the OAuth pair is only present when the
# Gmail side is configured, and an empty client id is worse than an absent one:
# it produces a credential that looks valid and fails at refresh.
#
# The `^@^` prefix switches gcloud's list delimiter from comma to `@`. An OAuth
# client secret can legitimately contain a comma, and with the default delimiter
# that would split one variable into two malformed ones.
TICK_ENV="CINEMA_GCP_PROJECT=${PROJECT_ID}"
TICK_ENV="${TICK_ENV}@CINEMA_ORDERS_DATABASE=${ORDERS_DB}"
TICK_ENV="${TICK_ENV}@CINEMA_MAIL_BACKEND=${MAIL_BACKEND}"
TICK_ENV="${TICK_ENV}@CINEMA_TOKEN_BACKEND=secret-manager"
TICK_ENV="${TICK_ENV}@CINEMA_REFRESH_TOKEN_SECRET=${TOKEN_SECRET}"
TICK_ENV="${TICK_ENV}@CINEMA_LOG_FORMAT=json"
if [[ -n "$OAUTH_CLIENT_ID" ]]; then
  TICK_ENV="${TICK_ENV}@CINEMA_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}"
  TICK_ENV="${TICK_ENV}@CINEMA_OAUTH_CLIENT_SECRET=${OAUTH_CLIENT_SECRET}"
fi
if [[ -n "${CINEMA_AGENT_EMAIL:-}" ]]; then
  TICK_ENV="${TICK_ENV}@CINEMA_AGENT_EMAIL=${CINEMA_AGENT_EMAIL}"
fi

# --timeout is under the one-minute schedule on purpose, so a wedged tick
# cannot still be running when the next one fires. That is only safe because
# the tick claims each row before working on it — a truncated tick leaves
# leased rows for the next pass rather than half-finished ones. Before the
# claiming landed, this setting would have caused the double-email it prevents.
gcloud run deploy "$TICK_SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$AGENT_EMAIL" \
  --no-allow-unauthenticated \
  --timeout=50s \
  --max-instances=2 \
  --min-instances=0 \
  --memory=512Mi \
  --set-env-vars="^@^${TICK_ENV}" \
  --quiet >/dev/null
ok "$TICK_SERVICE  (orchestrator.app:app, as $AGENT_SA, mail=$MAIL_BACKEND)"

# Same image, different command and a different account. The command override
# is the entire difference between the service that cannot spend money and the
# service that can.
#
# --allow-unauthenticated is deliberate and is not a hole. Cloud Run IAM cannot
# validate a Firebase ID token, so the gate has to live in the app, and it does:
# orchestrator/auth.py rejects anything without a verified token and a
# `producer` custom claim, and firestore.orders.rules refuses the write a second
# time for a browser. Putting IAM in front instead would mean a producer's
# browser could not call it at all.
gcloud run deploy "$APPROVALS_SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$APPROVALS_EMAIL" \
  --allow-unauthenticated \
  --command=uvicorn \
  --args="orchestrator.approvals:app,--host,0.0.0.0,--port,8080" \
  --timeout=60s \
  --max-instances=2 \
  --memory=512Mi \
  --set-env-vars="^@^CINEMA_GCP_PROJECT=${PROJECT_ID}@CINEMA_ORDERS_DATABASE=${ORDERS_DB}@CINEMA_LOG_FORMAT=json" \
  --quiet >/dev/null
ok "$APPROVALS_SERVICE  (orchestrator.approvals:app, as $APPROVALS_SA)"

TICK_URL=$(gcloud run services describe "$TICK_SERVICE" --region="$REGION" \
  --format='value(status.url)')
APPROVALS_URL=$(gcloud run services describe "$APPROVALS_SERVICE" \
  --region="$REGION" --format='value(status.url)')

# ---------------------------------------------------------------------------
say "Cloud Scheduler"
# ---------------------------------------------------------------------------

gcloud run services add-iam-policy-binding "$TICK_SERVICE" \
  --region="$REGION" \
  --member="serviceAccount:${SCHEDULER_EMAIL}" \
  --role="roles/run.invoker" --quiet >/dev/null
ok "run.invoker for $SCHEDULER_SA on $TICK_SERVICE"

if gcloud scheduler jobs describe cinema-tick --location="$REGION" \
     >/dev/null 2>&1; then
  gcloud scheduler jobs update http cinema-tick \
    --location="$REGION" --schedule="* * * * *" \
    --uri="${TICK_URL}/tick" --http-method=POST \
    --oidc-service-account-email="$SCHEDULER_EMAIL" \
    --oidc-token-audience="$TICK_URL" \
    --attempt-deadline=50s --quiet >/dev/null
  ok "scheduler job cinema-tick (updated)"
else
  gcloud scheduler jobs create http cinema-tick \
    --location="$REGION" --schedule="* * * * *" \
    --uri="${TICK_URL}/tick" --http-method=POST \
    --oidc-service-account-email="$SCHEDULER_EMAIL" \
    --oidc-token-audience="$TICK_URL" \
    --attempt-deadline=50s --quiet >/dev/null
  ok "scheduler job cinema-tick — every minute"
fi

# Nothing schedules the approval service. It is called by a person, and an
# approval that could be triggered on a timer would not be an approval.

# ---------------------------------------------------------------------------
say "What the two accounts can actually do"
# ---------------------------------------------------------------------------
echo "  Read this. Do not trust the ticks above — this is the guardrail."
echo
echo "  $AGENT_SA (tick) — must show a CONDITION naming (default), and must"
echo "  NOT show an unconditioned datastore.user:"
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${AGENT_EMAIL}" \
  --format='table(bindings.role, bindings.condition.expression)'
echo
echo "  $APPROVALS_SA — datastore.user with no condition is correct here:"
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${APPROVALS_EMAIL}" \
  --format='table(bindings.role, bindings.condition.expression)'

if [[ "$MAIL_BACKEND" == "memory" ]]; then
  NEXT_MAIL="  Mail is off. The loop will tick, research and open negotiations, and
  send nothing. To turn real email on once the token is bootstrapped:

    CINEMA_OAUTH_CLIENT_ID=... CINEMA_OAUTH_CLIENT_SECRET=... \\
      MAIL_BACKEND=gmail PROJECT_ID=$PROJECT_ID ./scripts/deploy.sh
"
else
  NEXT_MAIL="  Mail is ON. The next tick emails whatever suppliers research finds.
"
fi

cat <<EOF

$(printf '\033[1mDeployed\033[0m')

  tick       $TICK_URL      (private; Scheduler only)
  approvals  $APPROVALS_URL

$(printf '\033[1mNow verify it\033[0m')

  This script exiting 0 means the gcloud calls succeeded. It does not mean the
  deploy is correct — in particular it says nothing about whether the tick
  account can still reach the orders database, which is the one thing that
  would quietly undo Phase 4.

    PROJECT_ID=$PROJECT_ID ./scripts/verify_deploy.sh

$(printf '\033[1mThen\033[0m')

$NEXT_MAIL
  Leave it alone overnight. Come back to a negotiation that advanced with
  nobody touching it, and one JSON line per minute:

    gcloud logging read \\
      'resource.labels.service_name="$TICK_SERVICE" jsonPayload.message="tick"' \\
      --limit=20 --format='value(jsonPayload)'

EOF
