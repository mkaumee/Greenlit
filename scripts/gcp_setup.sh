#!/usr/bin/env bash
#
# Stand up the Google Cloud side. Run once, by a human, from a machine with
# gcloud installed and authenticated.
#
#   PROJECT_ID=your-project-id ./scripts/gcp_setup.sh
#
# Idempotent: every step checks before it creates, so re-running after a
# failure is safe and prints what already existed.
#
# It creates nothing that costs money while idle. Firestore, Secret Manager and
# the service account are all free at rest; only Cloud Run and Scheduler bill,
# and those come in Phase 3.
#
# ---------------------------------------------------------------------------
# One thing here is permanent
# ---------------------------------------------------------------------------
#
# A Firestore database's location cannot be changed. Moving it later means
# exporting, deleting and re-importing by hand. REGION below is baked in the
# moment the first database is created.
#
# ---------------------------------------------------------------------------
# Why there are two databases
# ---------------------------------------------------------------------------
#
# Security rules do not apply to server SDKs. The orchestrator reaches
# Firestore through a service account, which bypasses firestore.rules
# completely — so a rule denying purchase_orders writes constrains a browser
# and constrains nothing about the agent.
#
# Firestore IAM has no collection-level granularity either: roles/datastore.user
# is all-or-nothing across a database. So the only way to make "the agent
# service account cannot write purchase_orders" an IAM fact rather than a
# hopeful sentence is to put orders in their own database and never grant the
# agent access to it.
#
#   (default)  projects, items, suppliers, negotiations, messages
#              agent SA: roles/datastore.user, scoped by IAM condition
#   orders     purchase_orders, nothing else
#              agent SA: no binding at all
#
# Both are created here because database creation is the irreversible step and
# doing it now keeps the option open. Pointing the code at the second one is a
# separate change.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
AGENT_SA="${AGENT_SA:-cinema-agent}"
ORDERS_DB="${ORDERS_DB:-orders}"
TOKEN_SECRET="${TOKEN_SECRET:-gmail-agent-refresh-token}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID is not set." >&2
  echo "  PROJECT_ID=your-project-id $0" >&2
  exit 2
fi

if ! command -v gcloud >/dev/null; then
  echo "gcloud is not installed. https://cloud.google.com/sdk/docs/install" >&2
  exit 2
fi

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
skip() { printf '  \033[90m·\033[0m %s (already there)\n' "$*"; }

AGENT_EMAIL="${AGENT_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

say "Project"
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1
ok "$PROJECT_ID  (as $(gcloud config get-value account 2>/dev/null))"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
#
# Ask Google what this account may actually do, before changing anything.
#
# Worth the extra call: without it the script fails partway through and leaves
# a project in a state nobody can describe — some APIs on, one database
# created, no IAM binding. Being told the whole list up front means one access
# request instead of five.

declare -a NEEDED=(
  "serviceusage.services.enable|enable the APIs|roles/serviceusage.serviceUsageAdmin"
  "datastore.databases.create|create the two Firestore databases|roles/datastore.owner"
  "iam.serviceAccounts.create|create the agent service account|roles/iam.serviceAccountAdmin"
  "resourcemanager.projects.setIamPolicy|grant the agent its scoped role — THE GUARDRAIL|roles/resourcemanager.projectIamAdmin"
  "secretmanager.secrets.create|create the refresh-token secret|roles/secretmanager.admin"
)

say "Preflight — what this account can do"

# Straight to the REST API. testIamPermissions exists on Cloud Resource
# Manager, but gcloud never exposed it as a `gcloud projects` subcommand — only
# on some other resource types — so calling it by hand is the only way.
#
# It answers "which of these may *I* do", needs no special permission itself,
# and returns only the granted subset. Anything absent from the reply is denied.
json_perms=$(printf '%s\n' "${NEEDED[@]}" | cut -d'|' -f1 \
  | sed 's/.*/"&"/' | paste -sd,)

probe=$(curl -sS -w $'\n%{http_code}' -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -d "{\"permissions\":[${json_perms}]}" \
  "https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}:testIamPermissions" \
  2>&1) || true

http_code=$(tail -n1 <<<"$probe")
granted=$(sed '$d' <<<"$probe")

if [[ "$http_code" != "200" ]]; then
  # "I could not even ask" is a different problem from "I may not do these
  # things", and confusing the two sends you off requesting the wrong roles.
  echo "  Could not query permissions on $PROJECT_ID (HTTP $http_code):" >&2
  printf '    %s\n' "$granted" >&2
  echo >&2
  echo "  Usually the project id is wrong, or gcloud is signed in as an" >&2
  echo "  account with no access to it at all. Current account:" >&2
  echo "    $(gcloud config get-value account 2>/dev/null)" >&2
  exit 3
fi

missing=0
declare -a ASK_FOR=()
for entry in "${NEEDED[@]}"; do
  perm="${entry%%|*}"
  rest="${entry#*|}"
  what="${rest%%|*}"
  role="${rest##*|}"
  # Each permission comes back as its own quoted string, so a substring match
  # on the raw JSON is exact enough and saves depending on jq being installed.
  if grep -q "\"${perm}\"" <<<"$granted"; then
    ok "$what"
  else
    printf '  \033[31m✗\033[0m %s\n' "$what"
    ASK_FOR+=("$role")
    missing=$((missing + 1))
  fi
done

if (( missing > 0 )); then
  # shellcheck disable=SC2207
  unique_roles=($(printf '%s\n' "${ASK_FOR[@]}" | sort -u))
  cat <<EOF

$(printf '\033[1m%s permission(s) missing.\033[0m' "$missing") Nothing has been changed.

Ask whoever owns the project to grant these on $PROJECT_ID:

$(printf '  %s\n' "${unique_roles[@]}")

  gcloud projects add-iam-policy-binding $PROJECT_ID \\
    --member="user:\$YOUR_EMAIL" --condition=None \\
$(printf '    --role=%s \\\n' "${unique_roles[@]}" | sed '$ s/ \\$//')

If roles/resourcemanager.projectIamAdmin is in that list, read this before
asking for a substitute: it is the one that lets the agent's service account be
granted access to (default) and denied access to orders. Without it the split
between the two databases is a convention in our code rather than something
Google enforces, and the strongest claim this project makes stops being true.
roles/editor does NOT include it.

Re-run this script once the roles land. It is idempotent.
Set FORCE=1 to attempt anyway and see the raw errors.

EOF
  [[ "${FORCE:-}" == "1" ]] || exit 3
  printf '\033[33mFORCE=1 — continuing regardless.\033[0m\n'
fi

# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
#
# Checked separately from permissions because it is a different kind of "no".
# You can hold every IAM role on a project and still be unable to enable
# Secret Manager, Cloud Run or Scheduler, because those refuse to activate
# without a billing account attached. Firestore enables happily either way,
# which is what makes this fail halfway rather than up front.

say "Preflight — billing"

billing_state=$(gcloud billing projects describe "$PROJECT_ID" \
  --format='value(billingEnabled)' 2>/dev/null || echo "unknown")

case "$billing_state" in
  True|true)
    ok "a billing account is linked"
    ;;
  unknown)
    printf '  \033[33m?\033[0m could not read billing status — continuing anyway\n'
    printf '    (needs billing.resourceAssociations.list; if the API enables\n'
    printf '     below fail with UREQ_PROJECT_BILLING_NOT_FOUND, this is why)\n'
    ;;
  *)
    printf '  \033[31m✗\033[0m no billing account linked\n'
    cat <<EOF

$(printf '\033[1mBilling is not enabled on %s.\033[0m' "$PROJECT_ID")

Firestore will enable without it. Secret Manager, Cloud Run and Cloud
Scheduler will not — so the setup would stop partway through.

If you already have credits, the billing account exists and simply is not
attached to this project:

  gcloud billing accounts list
  gcloud billing projects link $PROJECT_ID --billing-account=ACCOUNT_ID

If that returns nothing, or linking is refused, the billing account belongs
to someone else. Linking needs Billing Account Administrator on the account
itself — being an admin on the *project* is not enough. Either get that role,
or ask whoever owns the billing account to link it from
Billing > My Projects > Link a billing account.

$(printf '\033[1mMeanwhile, most of this does not need billing:\033[0m')

  WITHOUT_BILLING=1 PROJECT_ID=$PROJECT_ID $0

That does the Firestore databases, the agent service account and the scoped
IAM binding, and skips only Secret Manager. Refresh tokens stay in the
gitignored file backend, which is where they already are.

It leaves nothing to redo — re-run the script normally once billing lands and
it adds the missing pieces.

Nothing further has been changed.

EOF
    [[ "${WITHOUT_BILLING:-}" == "1" || "${FORCE:-}" == "1" ]] || exit 4
    ;;
esac

if [[ "${WITHOUT_BILLING:-}" == "1" ]]; then
  printf '\n\033[33mWITHOUT_BILLING=1 — skipping everything that needs a billing account.\033[0m\n'
fi

# ---------------------------------------------------------------------------

say "APIs"

# Split by whether the API refuses to activate without a billing account.
# Firestore and Gmail do not care, which is what makes the whole email loop
# reachable on an unbilled project.
FREE_APIS=(firestore.googleapis.com gmail.googleapis.com)
BILLED_APIS=(
  secretmanager.googleapis.com
  run.googleapis.com
  cloudscheduler.googleapis.com
  artifactregistry.googleapis.com
  cloudbuild.googleapis.com
)

apis=("${FREE_APIS[@]}")
if [[ "${WITHOUT_BILLING:-}" != "1" ]]; then
  apis+=("${BILLED_APIS[@]}")
fi

enabled_now=$(gcloud services list --enabled --format='value(config.name)')
for api in "${apis[@]}"; do
  if grep -qx "$api" <<<"$enabled_now"; then
    skip "$api"
  else
    gcloud services enable "$api" >/dev/null
    ok "$api"
  fi
done

if [[ "${WITHOUT_BILLING:-}" == "1" ]]; then
  printf '  \033[90m·\033[0m skipped (need billing): %s\n' "${BILLED_APIS[*]}"
fi

# ---------------------------------------------------------------------------

say "Firestore  (location is permanent: $REGION)"
existing_dbs="$(gcloud firestore databases list --format='value(name)' 2>/dev/null || true)"

create_db() {
  local db="$1" label="$2"
  if grep -q "/databases/${db}$" <<<"$existing_dbs"; then
    skip "$label"
    return
  fi
  gcloud firestore databases create \
    --database="$db" \
    --location="$REGION" \
    --type=firestore-native >/dev/null
  ok "$label"
}

create_db "(default)" "(default) — negotiations, items, suppliers, messages"
create_db "$ORDERS_DB" "$ORDERS_DB — purchase orders only"

# ---------------------------------------------------------------------------

say "Agent service account"
if gcloud iam service-accounts describe "$AGENT_EMAIL" >/dev/null 2>&1; then
  skip "$AGENT_EMAIL"
else
  gcloud iam service-accounts create "$AGENT_SA" \
    --display-name="Agentic Cinema orchestrator" \
    --description="Runs the tick loop. Deliberately cannot write purchase orders." \
    >/dev/null
  ok "$AGENT_EMAIL"
fi

say "IAM"
# Scoped to the default database only. The condition is the whole point: a
# plain roles/datastore.user binding would cover every database in the project,
# orders included, and quietly undo the split above.
if gcloud projects get-iam-policy "$PROJECT_ID" \
     --flatten='bindings[].members' \
     --filter="bindings.members:serviceAccount:${AGENT_EMAIL} AND bindings.role:roles/datastore.user" \
     --format='value(bindings.role)' | grep -q datastore.user; then
  skip "datastore.user on (default)"
else
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${AGENT_EMAIL}" \
    --role="roles/datastore.user" \
    --condition="title=default-database-only,description=No access to the orders database,expression=resource.name.startsWith('projects/${PROJECT_ID}/databases/(default)')" \
    >/dev/null
  ok "datastore.user, conditioned to (default)"
fi

if [[ "${WITHOUT_BILLING:-}" == "1" ]]; then
  printf '  \033[90m·\033[0m secret %s skipped — Secret Manager needs billing.\n' \
    "$TOKEN_SECRET"
  printf '    Keep CINEMA_TOKEN_BACKEND=file until it is on.\n'
else
  if gcloud secrets describe "$TOKEN_SECRET" >/dev/null 2>&1; then
    skip "secret $TOKEN_SECRET"
  else
    gcloud secrets create "$TOKEN_SECRET" --replication-policy=automatic >/dev/null
    ok "secret $TOKEN_SECRET (no version yet — the bootstrap script adds one)"
  fi

  if gcloud secrets get-iam-policy "$TOKEN_SECRET" --format=json \
       | grep -q "$AGENT_EMAIL"; then
    skip "secretAccessor on $TOKEN_SECRET"
  else
    gcloud secrets add-iam-policy-binding "$TOKEN_SECRET" \
      --member="serviceAccount:${AGENT_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
    ok "secretAccessor on $TOKEN_SECRET"
  fi
fi

# ---------------------------------------------------------------------------

say "What the agent account can actually do"
echo "  Read this rather than trusting the ticks above — it is the guardrail."
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${AGENT_EMAIL}" \
  --format='table(bindings.role, bindings.condition.expression)'

if [[ "${WITHOUT_BILLING:-}" == "1" ]]; then
  TOKEN_LINE="CINEMA_TOKEN_BACKEND=file            # secret-manager once billing is on"
else
  TOKEN_LINE="CINEMA_TOKEN_BACKEND=secret-manager"
fi

cat <<EOF

$(printf '\033[1mNext\033[0m')

  1. Put these in your .env (see .env.example):

       CINEMA_GCP_PROJECT=$PROJECT_ID
       $TOKEN_LINE
       CINEMA_REFRESH_TOKEN_SECRET=$TOKEN_SECRET

  2. Deploy the rules and indexes:

       make deploy-rules PROJECT_ID=$PROJECT_ID

  3. Mint a Gmail refresh token — see docs/oauth-runbook.md:

       uv run python scripts/oauth_bootstrap.py

     Gmail needs no billing. The live email round-trip — the last unproven
     piece of Phase 1 — can be done today regardless of the billing situation.

  Not done here: Cloud Run and Cloud Scheduler. They cost money to leave
  running and there is nothing for a deployed ticker to tick until the script
  upload flow lands, so they belong to Phase 3.

EOF
