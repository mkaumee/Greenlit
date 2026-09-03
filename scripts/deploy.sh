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
API_SA="${API_SA:-cinema-api}"
SCHEDULER_SA="${SCHEDULER_SA:-cinema-scheduler}"
ORDERS_DB="${ORDERS_DB:-orders}"
TOKEN_SECRET="${TOKEN_SECRET:-gmail-agent-refresh-token}"

TICK_SERVICE="${TICK_SERVICE:-cinema-tick}"
APPROVALS_SERVICE="${APPROVALS_SERVICE:-cinema-approvals}"
API_SERVICE="${API_SERVICE:-cinema-api}"

# The role the api service holds over Secret Manager, and the reason it is a
# custom one. It must create a secret per producer and add versions to it, and
# it must never be able to read one back — a service that stores mailbox
# credentials has no business using them. Every predefined role that can write
# can also read, so this is defined rather than chosen.
TOKEN_WRITER_ROLE="${TOKEN_WRITER_ROLE:-cinemaTokenWriter}"

# Which reasoning runs. Defaults to the fake for the same reason mail defaults
# to memory: shipping the wrong one is silent. A regex writing negotiation
# emails looks like a working system until somebody reads one, so switching to
# Role A's brain is an explicit choice:
#
#   BRAIN_BACKEND=main-agent ./scripts/deploy.sh
#
# Do not turn this and MAIL_BACKEND=gmail on in the same deploy. If the first
# live email to a real seller reads badly, you want to know whether it was the
# reasoning or the transport.
BRAIN_BACKEND="${BRAIN_BACKEND:-scripted}"

# Verified against Vertex in the preflight below rather than trusted, because
# a model name is only half of an answer: the same name is served in some
# locations and not others, and a wrong pair 404s every reasoning call while
# looking, from every screen, like a system that is merely quiet.
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.7-flash}"

# Role A's researcher searches the web through Parallel, and that SDK reads
# this name itself. Declared with a default because `set -u` is on: referenced
# without one, an unset key aborts the whole deploy with "unbound variable"
# rather than reaching the preflight that explains what is missing.
PARALLEL_API_KEY="${PARALLEL_API_KEY:-}"

# Gemini through Vertex AI rather than an API key.
#
# We are already on Google Cloud and the service account already exists, so
# Vertex authenticates with it directly: nothing to store, rotate or leak, and
# no secret sitting in an env var that `gcloud run describe` prints back.
# google-genai reads exactly these three names — confirmed against the
# installed SDK, not recalled.
# Not $REGION. That is where the *container* runs; this is where Vertex serves
# a model, and they are unrelated. Tying them together is what turned a
# deliberate GOOGLE_CLOUD_LOCATION=global into us-central1 and 404'd every
# call — the newest models reach the global endpoint before any regional one,
# and google-genai's own default when no location is set is `global`
# (_api_client.py, confirmed against the installed SDK).
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"

# The panel is served from Firebase Hosting and this service from Cloud Run, so
# every approval a producer makes is a cross-origin POST. Without these origins
# the browser refuses at the preflight, before any route runs, and the console
# error says nothing about approvals. Both default hosting domains, because
# Firebase serves the same site on each.
ALLOWED_ORIGINS="${CINEMA_ALLOWED_ORIGINS:-https://${PROJECT_ID}.web.app,https://${PROJECT_ID}.firebaseapp.com}"

# `memory` unless explicitly asked otherwise. See the header.
MAIL_BACKEND="${MAIL_BACKEND:-memory}"
OAUTH_CLIENT_ID="${CINEMA_OAUTH_CLIENT_ID:-}"
OAUTH_CLIENT_SECRET="${CINEMA_OAUTH_CLIENT_SECRET:-}"

# Read out of the downloaded client JSON when they are not set explicitly.
#
# The same reasoning as client_credentials() in gmail.py, which has done this
# on the Python side all along: the file's path is already configuration, so
# asking a person to open it and copy two strings out is friction and a place
# to paste the wrong thing. A client secret ending up truncated by a stray
# newline fails as invalid_client, which names nothing.
#
# Handles both shapes Google produces — `installed` for a Desktop client and
# `web` for a Web-application one. This flow needs the Web one; the other is
# accepted because the same file feeds oauth_bootstrap.py.
OAUTH_CLIENT_SECRETS="${OAUTH_CLIENT_SECRETS:-.secrets/client_secret.json}"

if [[ -z "$OAUTH_CLIENT_ID" && -f "$OAUTH_CLIENT_SECRETS" ]]; then
  read -r OAUTH_CLIENT_ID OAUTH_CLIENT_SECRET < <(
    python3 - "$OAUTH_CLIENT_SECRETS" <<'PYEOF'
import json, sys

try:
    blob = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print(" ")
    raise SystemExit(0)

for shape in ("web", "installed"):
    section = blob.get(shape)
    if isinstance(section, dict):
        cid = section.get("client_id") or ""
        secret = section.get("client_secret") or ""
        if cid and secret:
            print(f"{cid} {secret}")
            break
else:
    print(" ")
PYEOF
  ) || true
  if [[ -n "$OAUTH_CLIENT_ID" ]]; then
    ok_later_oauth="read the OAuth client from $OAUTH_CLIENT_SECRETS"
  fi
fi
ok_later_oauth="${ok_later_oauth:-}"

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

# Deferred until ok() exists — the client JSON is read before the helpers are
# defined, because every preflight below wants the values.
if [[ -n "$ok_later_oauth" ]]; then
  ok "$ok_later_oauth"
fi

AGENT_EMAIL="${AGENT_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
APPROVALS_EMAIL="${APPROVALS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
API_EMAIL="${API_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
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
say "Preflight — the brain"
# ---------------------------------------------------------------------------
#
# Same shape as the mail check above, and for a worse failure. Role A's
# researcher searches the web through Parallel; with no PARALLEL_API_KEY the
# call raises, researcher.py catches it, and the model is told "web search
# failed" — so it answers anyway, from memory. Out come reference price bands
# and supplier URLs with nothing behind them, in a system whose whole claim is
# that it keeps the URLs it got its numbers from.
#
# Nothing errors, nothing logs, and the negotiation emails look fine. Caught
# here, and again at container startup, because it is not detectable later.

# The Vertex endpoint for a location. Three shapes, copied from the SDK's own
# logic (google/genai/_api_client.py) rather than guessed, because two of them
# are not `{location}-aiplatform`: `global` has its own hostname, and `us`/`eu`
# are multi-regional. Getting it wrong is a DNS failure, which reads like a
# network problem rather than a configuration one.
vertex_host() {
  case "$VERTEX_LOCATION" in
    global) echo "aiplatform.googleapis.com" ;;
    us | eu) echo "aiplatform.${VERTEX_LOCATION}.rep.googleapis.com" ;;
    *) echo "${VERTEX_LOCATION}-aiplatform.googleapis.com" ;;
  esac
}

# 200 means the configured model answers. 404 means it does not exist here and
# the deploy is refused. Anything else warns and continues: a preflight that
# blocks a deploy on a flaky curl or a quota blip is worse than one that does
# not, and the only unambiguous, deterministic failure is the 404.
probe_model() {
  local host status
  host=$(vertex_host)
  status=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    "https://${host}/v1beta1/projects/${PROJECT_ID}/locations/${VERTEX_LOCATION}/publishers/google/models/${GEMINI_MODEL}:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"ping"}]}],"generationConfig":{"maxOutputTokens":1}}' \
    2>/dev/null || echo 000)

  case "$status" in
    200) return 0 ;;
    404)
      cat >&2 <<EOF
  ✗ '$GEMINI_MODEL' is not reachable in '$VERTEX_LOCATION' for this project.

  That is not the same as the model not existing. Vertex returns one 404 for
  both "no such model" and "not served here, or not enabled for you", so the
  location is the first thing to suspect and the name is the second.

  Every reasoning call would fail: no props read from a screenplay, no
  research, no negotiation email, and a chat that answers from stored records
  while looking like a system that is merely quiet. Refusing rather than
  deploying that.

  Try the other endpoint first — the newest models reach it before any
  regional one:

    VERTEX_LOCATION=global BRAIN_BACKEND=main-agent PROJECT_ID=$PROJECT_ID $0

  What this project can serve in '$VERTEX_LOCATION':

EOF
      list_models >&2
      cat >&2 <<EOF

    GEMINI_MODEL=<one of those> BRAIN_BACKEND=main-agent PROJECT_ID=$PROJECT_ID $0

  Nothing has been changed.
EOF
      return 1
      ;;
    *)
      printf '  \033[33m!\033[0m could not check %s (HTTP %s) — continuing.\n' \
        "$GEMINI_MODEL" "$status"
      echo "    Not proof the model is wrong; the probe itself did not get an"
      echo "    answer. If reasoning fails after this, that is where to look."
      return 0
      ;;
  esac
}

# Best effort, and only ever printed beside a refusal. The publisher listing is
# long and its shape has changed before, so the grep is deliberately loose and
# an empty result is survivable — the message above still says what to do.
list_models() {
  local host
  host=$(vertex_host)
  curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    "https://${host}/v1beta1/publishers/google/models?pageSize=200" 2>/dev/null \
    | grep -o 'publishers/google/models/gemini[A-Za-z0-9.-]*' \
    | sed 's|publishers/google/models/|    |' \
    | sort -u \
    || echo "    (could not list them — see the Model Garden in the console)"
}

case "$BRAIN_BACKEND" in
  scripted)
    printf '  \033[33m!\033[0m brain is the SCRIPTED fake — a regex and a word list.\n'
    echo "    Fine for proving the loop runs. Not something to show anyone."
    echo "    Turn on the real one:  BRAIN_BACKEND=main-agent"
    ;;
  main-agent)
    # The placeholder check, and it is not pedantry. `-z` cannot tell a key
    # from the word people paste when a command says PARALLEL_API_KEY=your-key
    # — and an invalid key is worse than a missing one. Missing refuses at
    # startup and is loud; invalid gets past every check here, then the search
    # call raises, researcher.py catches it, and the model is told "web search
    # failed" and answers from memory anyway. Out come reference price bands
    # and supplier URLs with nothing behind them, in a system whose whole claim
    # is that it keeps the URLs it got its numbers from.
    case "${PARALLEL_API_KEY,,}" in
      your-key | your-api-key | your_key | api-key | changeme | xxx | todo | "<key>")
        cat >&2 <<EOF
  ✗ PARALLEL_API_KEY is '$PARALLEL_API_KEY', which is the placeholder from the
    command, not a key.

  Worth refusing rather than shrugging at: an invalid key does not fail loudly
  the way a missing one does. Research answers anyway, from memory, and its
  price bands and supplier URLs are then invented rather than sourced — with
  nothing on any screen to say so.

    PARALLEL_API_KEY=<the real one> BRAIN_BACKEND=main-agent PROJECT_ID=$PROJECT_ID $0

  Nothing has been changed.
EOF
        exit 7
        ;;
    esac

    if [[ -z "$PARALLEL_API_KEY" ]]; then
      cat >&2 <<EOF
  ✗ BRAIN_BACKEND=main-agent, but PARALLEL_API_KEY is not set.

  Research would still answer — with no web search behind it — so its price
  bands and supplier URLs would be invented rather than sourced. The service
  refuses to start on this, so deploying it would produce a revision that
  never goes ready.

    PARALLEL_API_KEY=... BRAIN_BACKEND=main-agent PROJECT_ID=$PROJECT_ID $0

  Nothing has been changed.
EOF
      exit 5
    fi
    # Does that model actually exist, here, for this project?
    #
    # This line used to print a green tick beside whatever string was in
    # GEMINI_MODEL and check nothing at all — and a tick beside an unverified
    # string is indistinguishable from a tick beside a working one. Every
    # reasoning call was 404ing and nothing surfaced until somebody asked the
    # chat a question and got the deterministic fallback.
    #
    # Both halves are checked here, because the failure was the *pair*: a real
    # model name sent to a location that does not serve it.
    #
    # Probed with the real call rather than a metadata lookup: generateContent
    # on v1beta1 is exactly what google-genai does, so a 200 here means the
    # thing that will run works. One token, a fraction of a cent. The API
    # version matters — the SDK pins v1beta1, and a probe on v1 could refuse a
    # deploy over a model that is perfectly fine.
    if ! probe_model; then
      exit 6
    fi
    ok "brain is main-agent ($GEMINI_MODEL in $VERTEX_LOCATION), reachable"
    ;;
  *)
    echo "  ✗ BRAIN_BACKEND must be 'scripted' or 'main-agent', got '$BRAIN_BACKEND'" >&2
    exit 2
    ;;
esac

# ---------------------------------------------------------------------------
say "APIs"
# ---------------------------------------------------------------------------

enabled=$(gcloud services list --enabled --format='value(config.name)')
for api in run.googleapis.com cloudscheduler.googleapis.com \
           artifactregistry.googleapis.com cloudbuild.googleapis.com \
           secretmanager.googleapis.com iamcredentials.googleapis.com \
           aiplatform.googleapis.com; do
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
ensure_sa "$API_SA" "$API_EMAIL" \
  "Serves the producer's browser. Cannot write purchase orders."
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

# The api service reaches (default) and nothing else. Same conditioned shape as
# the agent's, and for the same reason: Firestore IAM cannot name a collection,
# so "may not write a purchase order" has to mean "has no binding on the orders
# database". This is what makes the third service worth its cost.
if has_role "$API_EMAIL" "roles/datastore.user"; then
  skip "datastore.user for $API_SA"
else
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${API_EMAIL}" \
    --role="roles/datastore.user" \
    --condition="expression=resource.name.startsWith('projects/${PROJECT_ID}/databases/(default)'),title=default_database_only,description=No access to the orders database." \
    >/dev/null
  ok "datastore.user for $API_SA — (default) only, CONDITIONED"
fi

# Writing a producer's refresh token, without being able to read one.
#
# Every predefined Secret Manager role that can write can also read, so the
# smallest correct grant does not exist and has to be defined. secretAccessor
# is deliberately absent: the service that stores a mailbox credential has no
# business using it, and the tick service — which does — cannot create one.
if gcloud iam roles describe "$TOKEN_WRITER_ROLE" --project="$PROJECT_ID" \
     >/dev/null 2>&1; then
  skip "role $TOKEN_WRITER_ROLE"
else
  gcloud iam roles create "$TOKEN_WRITER_ROLE" --project="$PROJECT_ID" \
    --title="Greenlit token writer" \
    --description="Create and add versions to producer refresh-token secrets. Cannot read them." \
    --permissions="secretmanager.secrets.create,secretmanager.secrets.get,secretmanager.versions.add" \
    --stage=GA >/dev/null
  ok "role $TOKEN_WRITER_ROLE — write a token, never read one"
fi

if has_role "$API_EMAIL" "projects/${PROJECT_ID}/roles/${TOKEN_WRITER_ROLE}"; then
  skip "$TOKEN_WRITER_ROLE for $API_SA"
else
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${API_EMAIL}" \
    --role="projects/${PROJECT_ID}/roles/${TOKEN_WRITER_ROLE}" \
    --condition=None >/dev/null
  ok "$TOKEN_WRITER_ROLE for $API_SA"
fi

# The tick service reads them. Project-level rather than per-secret because a
# producer's secret does not exist until they connect, and you cannot grant
# accessor on a secret that is not there yet.
if has_role "$AGENT_EMAIL" "roles/secretmanager.secretAccessor"; then
  skip "secretAccessor for $AGENT_SA"
else
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${AGENT_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None >/dev/null
  ok "secretAccessor for $AGENT_SA — reads producer tokens, creates none"
fi

# Vertex AI, for both services that reason.
#
# This is what replaces a Gemini API key: the account authenticates as itself,
# so there is no key to store or rotate and nothing secret in the deployed
# environment. roles/aiplatform.user is the smallest role that can call a
# published model; it does not permit training, tuning or model management.
#
# The tick negotiates and researches. The api service reads screenplays —
# extract_props is a Gemini call, and it is the service a producer's browser
# can reach — so it needs the same grant. Without it a script upload fails at
# request time with a permission error from deep inside the SDK, which is a
# long way from "the producer dropped a PDF on the page".
#
# Granted whether or not BRAIN_BACKEND is main-agent, so that switching the
# brain on later is one variable rather than a variable and an IAM change
# nobody remembers.
for reasoner in "$AGENT_SA:$AGENT_EMAIL" "$API_SA:$API_EMAIL"; do
  reasoner_sa="${reasoner%%:*}"
  reasoner_email="${reasoner#*:}"
  if has_role "$reasoner_email" "roles/aiplatform.user"; then
    skip "aiplatform.user for $reasoner_sa"
  else
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${reasoner_email}" \
      --role="roles/aiplatform.user" \
      --condition=None >/dev/null
    ok "aiplatform.user for $reasoner_sa — Gemini through Vertex, no API key"
  fi
done

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
    --description="Greenlit images" >/dev/null
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
TICK_ENV="${TICK_ENV}@CINEMA_BRAIN_BACKEND=${BRAIN_BACKEND}"
TICK_ENV="${TICK_ENV}@CINEMA_GEMINI_MODEL=${GEMINI_MODEL}"
TICK_ENV="${TICK_ENV}@CINEMA_TOKEN_BACKEND=secret-manager"
TICK_ENV="${TICK_ENV}@CINEMA_REFRESH_TOKEN_SECRET=${TOKEN_SECRET}"
TICK_ENV="${TICK_ENV}@CINEMA_LOG_FORMAT=json"
# Not prefixed CINEMA_: google-genai reads these names itself. Set on every
# deploy, so turning the real brain on later does not also mean remembering
# these three.
TICK_ENV="${TICK_ENV}@GOOGLE_GENAI_USE_VERTEXAI=true"
TICK_ENV="${TICK_ENV}@GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
TICK_ENV="${TICK_ENV}@GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}"
if [[ -n "$PARALLEL_API_KEY" ]]; then
  # Not prefixed CINEMA_: the Parallel SDK reads this name itself.
  TICK_ENV="${TICK_ENV}@PARALLEL_API_KEY=${PARALLEL_API_KEY}"
fi
# The api service reads screenplays, so it needs the same brain configuration
# the tick has — but not PARALLEL_API_KEY. Only research_item searches the web,
# and that runs on the tick; a key here would be a credential in an environment
# that has no use for it.
API_BRAIN_ENV="@CINEMA_BRAIN_BACKEND=${BRAIN_BACKEND}"
API_BRAIN_ENV="${API_BRAIN_ENV}@CINEMA_GEMINI_MODEL=${GEMINI_MODEL}"
API_BRAIN_ENV="${API_BRAIN_ENV}@GOOGLE_GENAI_USE_VERTEXAI=true"
API_BRAIN_ENV="${API_BRAIN_ENV}@GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
API_BRAIN_ENV="${API_BRAIN_ENV}@GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}"

API_OAUTH_ENV=""
if [[ -n "$OAUTH_CLIENT_ID" ]]; then
  TICK_ENV="${TICK_ENV}@CINEMA_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}"
  TICK_ENV="${TICK_ENV}@CINEMA_OAUTH_CLIENT_SECRET=${OAUTH_CLIENT_SECRET}"
  # The api service exchanges authorization codes, so it needs the same client.
  API_OAUTH_ENV="@CINEMA_OAUTH_CLIENT_ID=${OAUTH_CLIENT_ID}"
  API_OAUTH_ENV="${API_OAUTH_ENV}@CINEMA_OAUTH_CLIENT_SECRET=${OAUTH_CLIENT_SECRET}"
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
  --set-env-vars="^@^CINEMA_GCP_PROJECT=${PROJECT_ID}@CINEMA_ORDERS_DATABASE=${ORDERS_DB}@CINEMA_LOG_FORMAT=json@CINEMA_ALLOWED_ORIGINS=${ALLOWED_ORIGINS}" \
  --quiet >/dev/null
ok "$APPROVALS_SERVICE  (orchestrator.approvals:app, as $APPROVALS_SA)"

# The producer's browser talks to this one. Same image, a third command, and an
# account that cannot reach the orders database.
gcloud run deploy "$API_SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --service-account="$API_EMAIL" \
  --allow-unauthenticated \
  --command=uvicorn \
  --args="orchestrator.api:app,--host,0.0.0.0,--port,8080" \
  --timeout=60s \
  --max-instances=2 \
  --memory=512Mi \
  --set-env-vars="^@^CINEMA_GCP_PROJECT=${PROJECT_ID}@CINEMA_LOG_FORMAT=json@CINEMA_ALLOWED_ORIGINS=${ALLOWED_ORIGINS}@CINEMA_TOKEN_BACKEND=secret-manager@CINEMA_REFRESH_TOKEN_SECRET=${TOKEN_SECRET}${API_BRAIN_ENV}${API_OAUTH_ENV}" \
  --quiet >/dev/null
ok "$API_SERVICE  (orchestrator.api:app, as $API_SA)"

API_URL=$(gcloud run services describe "$API_SERVICE" \
  --region="$REGION" --format='value(status.url)')

# The redirect URI is this service's own URL, which does not exist until the
# service does — so it is set on a second pass rather than guessed. Configured
# rather than derived per request from the Host header: that header is
# attacker-controlled, and a redirect_uri built from it is a way to have Google
# hand an authorization code somewhere else.
REDIRECT_URI="${API_URL}/mailbox/callback"
gcloud run services update "$API_SERVICE" --region="$REGION" \
  --update-env-vars="CINEMA_OAUTH_REDIRECT_URI=${REDIRECT_URI}" \
  --quiet >/dev/null
ok "redirect URI configured — register it, see below"

# Which of your OAuth clients is the right one?
#
# A project typically has several Web clients: Firebase creates its own for
# Google sign-in, and there is the one somebody made by hand for this flow.
# They are indistinguishable by name and interchangeable-looking in the
# console, and picking the wrong one fails as redirect_uri_mismatch — after a
# producer has already been through the consent screen.
#
# The downloaded JSON lists that client's registered redirect_uris, so the
# question is answerable here rather than guessed at. It is a snapshot from
# download time, which is why this is a warning and not a refusal: registering
# the URI after downloading is the normal case on a first deploy.
REDIRECT_REGISTERED=""
if [[ -f "$OAUTH_CLIENT_SECRETS" ]]; then
  REDIRECT_REGISTERED=$(
    python3 - "$OAUTH_CLIENT_SECRETS" "$REDIRECT_URI" <<'PYEOF'
import json, sys

try:
    blob = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    raise SystemExit(0)

for shape in ("web", "installed"):
    section = blob.get(shape)
    if isinstance(section, dict):
        if sys.argv[2] in (section.get("redirect_uris") or []):
            print("yes")
        break
PYEOF
  ) || true
fi

if [[ "$REDIRECT_REGISTERED" == "yes" ]]; then
  ok "that URI is already registered on the client in $OAUTH_CLIENT_SECRETS"
elif [[ -f "$OAUTH_CLIENT_SECRETS" ]]; then
  printf '  \033[33m!\033[0m the client in %s does not list that redirect URI.\n' \
    "$OAUTH_CLIENT_SECRETS"
  echo "    Expected on a first deploy — the URI did not exist until just now."
  echo "    If you have several Web clients, the right one is whichever you"
  echo "    register it on; re-download its JSON afterwards and this line will"
  echo "    confirm you used the same one."
fi

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
say "What the three accounts can actually do"
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
echo "  $API_SA (browser-facing) — must also show a CONDITION naming (default)."
echo "  It serves chat and script upload, so an unconditioned binding here"
echo "  would put the identity behind every producer action next to the money:"
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${API_EMAIL}" \
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
  api        $API_URL

$(printf '\033[1mNow verify it\033[0m')

  This script exiting 0 means the gcloud calls succeeded. It does not mean the
  deploy is correct — in particular it says nothing about whether the tick
  account can still reach the orders database, which is the one thing that
  would quietly undo Phase 4.

    PROJECT_ID=$PROJECT_ID ./scripts/verify_deploy.sh

$(printf '\033[1mThen\033[0m')

$NEXT_MAIL
$(printf '\033[1mBefore a producer can connect their Gmail\033[0m')

  The service is already configured with this. What is left is registering it
  on the Web OAuth client, which cannot be scripted — gcloud does not expose
  OAuth client redirect URIs at all:

    ${REDIRECT_URI}

  Cloud Run also answers to a second hostname for the same service, and OAuth
  compares redirect_uri as an exact string. Register both. The script below
  prints both, along with the two things people get wrong on that screen:

    PROJECT_ID=$PROJECT_ID ./scripts/oauth_redirect_uri.sh

  Leave it alone overnight. Come back to a negotiation that advanced with
  nobody touching it, and one JSON line per minute:

    gcloud logging read \\
      'resource.labels.service_name="$TICK_SERVICE" jsonPayload.message="tick"' \\
      --limit=20 --format='value(jsonPayload)'

EOF
