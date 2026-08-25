#!/usr/bin/env bash
#
# Check a deployment the way you would check someone else's claim.
#
#   PROJECT_ID=your-project-id ./scripts/verify_deploy.sh
#
# Read-only. Changes nothing, so run it as often as you like — after a deploy,
# after a config change, or on the morning of the demo.
#
# ---------------------------------------------------------------------------
# Why this is a separate script from deploy.sh
# ---------------------------------------------------------------------------
#
# deploy.sh exiting 0 means its gcloud calls succeeded. That is a much weaker
# statement than "the deployment is correct", and the gap between the two is
# exactly where Hard Rule 5 lives: the guardrail is an *absence* — no IAM
# binding on the orders database — and nothing about a successful deploy tests
# an absence.
#
# ---------------------------------------------------------------------------
# The trap this script exists to avoid
# ---------------------------------------------------------------------------
#
# The obvious check is "impersonate the tick account, try to read the orders
# database, expect PERMISSION_DENIED". Written that way it proves nothing,
# because impersonation *itself* fails with PERMISSION_DENIED when the caller
# lacks roles/iam.serviceAccountTokenCreator on the target account. Both
# failures print the same thing, and the wrong one reads as a pass.
#
# So the guardrail check is a pair:
#
#   positive  impersonate the tick account, read (default)  -> must SUCCEED
#   negative  impersonate the tick account, read orders     -> must FAIL
#
# The positive is what proves impersonation works at all. Without it a denial on
# the negative is uninformative, and this script says INCONCLUSIVE rather than
# PASS. An honest "I could not tell" is worth more than a green tick that means
# nothing.

set -uo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
AGENT_SA="${AGENT_SA:-cinema-agent}"
APPROVALS_SA="${APPROVALS_SA:-cinema-approvals}"
ORDERS_DB="${ORDERS_DB:-orders}"
TICK_SERVICE="${TICK_SERVICE:-cinema-tick}"
APPROVALS_SERVICE="${APPROVALS_SERVICE:-cinema-approvals}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID is not set." >&2
  echo "  PROJECT_ID=your-project-id $0" >&2
  exit 2
fi
command -v gcloud >/dev/null || { echo "gcloud is not installed." >&2; exit 2; }

AGENT_EMAIL="${AGENT_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
APPROVALS_EMAIL="${APPROVALS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

PASSED=0
FAILED=0
UNKNOWN=0

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; PASSED=$((PASSED + 1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILED=$((FAILED + 1)); }
huh()  { printf '  \033[33m????\033[0m  %s\n' "$*"; UNKNOWN=$((UNKNOWN + 1)); }
note() { printf '        \033[90m%s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null 2>&1

# ---------------------------------------------------------------------------
say "1. The services are up"
# ---------------------------------------------------------------------------

TICK_URL=$(gcloud run services describe "$TICK_SERVICE" --region="$REGION" \
  --format='value(status.url)' 2>/dev/null || true)
APPROVALS_URL=$(gcloud run services describe "$APPROVALS_SERVICE" \
  --region="$REGION" --format='value(status.url)' 2>/dev/null || true)

if [[ -n "$TICK_URL" ]]; then pass "tick: $TICK_URL"
else fail "tick service not found"; fi

if [[ -n "$APPROVALS_URL" ]]; then pass "approvals: $APPROVALS_URL"
else fail "approvals service not found"; fi

# ---------------------------------------------------------------------------
say "2. The tick is private, and answers an authorised caller"
# ---------------------------------------------------------------------------
#
# Public would mean anyone on the internet can drive the agent's mailbox.
#
# The body matters as much as the code. A 404 from Cloud Run's front end ("The
# requested URL was not found on this server") and a 404 from FastAPI
# ({"detail":"Not Found"}) mean completely different things — wrong hostname
# versus wrong route — and the status code alone cannot tell them apart.

http() {
  # Prints "CODE<TAB>first 160 bytes of body, newlines squashed".
  local out code body
  if ! out=$(curl -sS -w $'\n%{http_code}' "$@" 2>&1); then
    printf '000\tcurl failed: %s\n' "$(head -c 120 <<<"$out" | tr '\n' ' ')"
    return
  fi
  code=$(tail -n1 <<<"$out")
  body=$(sed '$d' <<<"$out" | tr -d '\r' | tr '\n' ' ' | head -c 160)
  printf '%s\t%s\n' "$code" "$body"
}

whose_error() {
  # Google's front end and our app both emit 404s and they mean opposite
  # things. The front end answers in HTML with a robot picture and "That's all
  # we know"; FastAPI answers {"detail":"Not Found"}. Telling them apart is the
  # difference between "the request never reached the container" and "the route
  # is wrong", and no status code carries that.
  case "$1" in
    *"That’s all we know"*|*"That's all we know"*|*"Error 404 (Not Found)"*|*"<!DOCTYPE html>"*)
      echo "google" ;;
    *'"detail"'*)
      echo "app" ;;
    *)
      echo "unclear" ;;
  esac
}

check_http() {
  # check_http <label> <expected-code> <curl args...>
  local label="$1" want="$2"; shift 2
  local result code body
  result=$(http "$@")
  code=${result%%$'\t'*}
  body=${result#*$'\t'}
  if [[ "$code" == "$want" ]]; then
    pass "$label ($code)"
    return
  fi
  fail "$label returned $code, expected $want"
  case "$(whose_error "$body")" in
    google)
      note "This is Google's front end, not our app — the request never"
      note "reached the container. Usual causes, in order of likelihood:"
      note "  · ingress is internal-only (Cloud Run 404s rather than 403s so"
      note "    it does not leak that the service exists). Check with:"
      note "      gcloud run services describe <svc> --region=$REGION \\"
      note "        --format='value(spec.template.metadata.annotations)'"
      note "  · the service or revision was deleted"
      note "  · the hostname is stale — compare status.url against the URL"
      note "    'gcloud run deploy' printed" ;;
    app)
      note "This came from our app, so the container is running and the route"
      note "is the problem. body: $body" ;;
    *)
      note "body: $body" ;;
  esac
}

if [[ -n "$TICK_URL" ]]; then
  anon=$(http -XPOST "$TICK_URL/tick")
  case "${anon%%$'\t'*}" in
    401|403) pass "an anonymous POST /tick is refused (${anon%%$'\t'*})" ;;
    000)     huh "could not reach $TICK_URL at all" ;;
    *)       fail "anonymous POST /tick returned ${anon%%$'\t'*} — the tick is PUBLIC"
             note "body: ${anon#*$'\t'}" ;;
  esac

  token=$(gcloud auth print-identity-token 2>/dev/null || true)
  if [[ -z "$token" ]]; then
    huh "could not mint an identity token, so the authorised path is untested"
    note "  gcloud auth login"
  else
    check_http "an authorised GET /health" 200 \
      -H "Authorization: Bearer $token" "$TICK_URL/health"
  fi
fi

if [[ -n "$APPROVALS_URL" ]]; then
  # Public on purpose: Cloud Run IAM cannot validate a Firebase ID token, so
  # the gate is auth.py. What must be true is that it refuses an anonymous
  # approval — a 401, not a 200.
  check_http "approvals /health" 200 "$APPROVALS_URL/health"

  check_http "an unauthenticated approval is refused" 401 -XPOST \
    -H 'Content-Type: application/json' \
    -d '{"project_id":"none","negotiation_id":"none"}' \
    "$APPROVALS_URL/items/none/approve"
fi

# ---------------------------------------------------------------------------
say "3. THE GUARDRAIL — can the tick account reach the orders database?"
# ---------------------------------------------------------------------------
#
# The pair described at the top of this file. Read both results together or
# neither of them means anything.
#
# One classifier, used for both accounts. An earlier version had two, written
# differently, and they disagreed about the same underlying cause: the agent
# check reported "unknown" while the approvals check reported "failed", from
# what was almost certainly one missing permission. A verifier that describes
# one fact two ways is worse than useless.

LAST_ERR=""

try_read() {
  # Can this service account read this database?
  #
  #   0 = yes
  #   1 = impersonation worked, Firestore said no
  #   2 = could not impersonate — tells us nothing about Firestore
  #   3 = the database does not exist
  #
  # Straight to the Firestore REST API with an impersonated access token.
  #
  # The obvious-looking `gcloud firestore documents list` DOES NOT EXIST. It
  # was in an earlier version of this script and made the whole guardrail check
  # meaningless: gcloud answers an unknown subcommand with "Invalid choice" and
  # a non-zero exit, which reads exactly like a permission denial. Two runs
  # reported FAIL on a check that had never once contacted Firestore.
  #
  # If you are tempted to swap this back to a gcloud subcommand, confirm the
  # subcommand exists first:  gcloud firestore --help
  local who="$1" database="$2" token code body encoded

  # stderr to a file, NOT merged into the token. gcloud prints a WARNING about
  # impersonation on every successful call, and folding that into $token gives
  # an Authorization header containing a newline — which curl rejects with
  # error 43, indistinguishable at a glance from a permissions problem.
  local errfile
  errfile=$(mktemp)
  if ! token=$(gcloud auth print-access-token \
        --impersonate-service-account="$who" 2>"$errfile"); then
    LAST_ERR=$(tr '\n' ' ' <"$errfile" | head -c 240)
    rm -f "$errfile"
    return 2
  fi
  rm -f "$errfile"
  # Belt and braces: a token is one opaque word, so anything else is contamination.
  token=$(tr -d '[:space:]' <<<"$token")

  # Parentheses are legal in a URL path, but encoding (default) settles it.
  encoded=${database//\(/%28}
  encoded=${encoded//\)/%29}

  if ! body=$(curl -sS -w $'\n%{http_code}' \
      -H "Authorization: Bearer $token" \
      "https://firestore.googleapis.com/v1/projects/${PROJECT_ID}/databases/${encoded}/documents/purchase_orders?pageSize=1" \
      2>&1); then
    LAST_ERR="curl failed: $(tr '\n' ' ' <<<"$body" | head -c 200)"
    return 2
  fi

  code=$(tail -n1 <<<"$body")
  LAST_ERR=$(sed '$d' <<<"$body" | tr '\n' ' ' | head -c 240)

  case "$code" in
    200) LAST_ERR=""; return 0 ;;
    403) return 1 ;;
    404) return 3 ;;
    *)   return 1 ;;
  esac
}

# Every branch that reports a problem prints what the tool actually said.
# Twice in this project a check reported a confident, wrong diagnosis because
# the underlying error was discarded: a gcloud subcommand that did not exist
# read as a permission denial, and an impersonation failure gave no clue which
# of several causes applied. The error text costs one line and is usually the
# whole answer.
grant_hint() {
  note "Grant yourself the right to impersonate, then re-run:"
  note "  gcloud iam service-accounts add-iam-policy-binding $1 \\"
  note "    --member=\"user:\$(gcloud config get-value account)\" \\"
  note "    --role=roles/iam.serviceAccountTokenCreator"
}

try_read "$AGENT_EMAIL" "(default)"
case $? in
  3)
    fail "the '(default)' database does not exist"
    note "Run scripts/gcp_setup.sh." ;;&
  0)
    note "impersonation works — a denial below is Firestore's, not IAM's"
    if try_read "$AGENT_EMAIL" "$ORDERS_DB"; then
      fail "the tick account CAN read the '$ORDERS_DB' database"
      note "Hard Rule 5 is not true in this deployment. Look for an"
      note "unconditioned roles/datastore.user on $AGENT_SA."
    else
      pass "the tick account cannot touch '$ORDERS_DB' — Hard Rule 5 holds"
    fi
    ;;
  1)
    huh "$AGENT_SA cannot read '(default)' either, so the guardrail is untested"
    note "It should be able to. Either scripts/gcp_setup.sh has not run, or the"
    note "conditioned binding has not propagated yet — IAM can take a few"
    note "minutes. Check what it actually holds:"
    note "  gcloud projects get-iam-policy $PROJECT_ID \\"
    note "    --flatten='bindings[].members' \\"
    note "    --filter='bindings.members:serviceAccount:$AGENT_EMAIL' \\"
    note "    --format='table(bindings.role, bindings.condition.expression)'"
    note "firestore said: $LAST_ERR"
    ;;
  2)
    huh "cannot impersonate $AGENT_SA, so the guardrail is untested"
    note "This is NOT a pass — it says nothing about what the agent can reach."
    note "gcloud said: $LAST_ERR"
    grant_hint "$AGENT_EMAIL"
    ;;
esac

# The other half of the split: the approvals account must reach both.
try_read "$APPROVALS_EMAIL" "$ORDERS_DB"
case $? in
  3)
    fail "the '$ORDERS_DB' database does not exist"
    note "Run scripts/gcp_setup.sh." ;;&
  0) pass "the approvals account can reach '$ORDERS_DB' — approval will work" ;;
  1)
    fail "the approvals account CANNOT reach '$ORDERS_DB'"
    note "Approving will fail at the point of writing the order. It needs an"
    note "unconditioned roles/datastore.user. If gcp_setup.sh only just ran,"
    note "give IAM a few minutes and try again before changing anything."
    note "firestore said: $LAST_ERR"
    ;;
  2)
    huh "cannot impersonate $APPROVALS_SA — untested"
    note "gcloud said: $LAST_ERR"
    grant_hint "$APPROVALS_EMAIL"
    ;;
esac

# ---------------------------------------------------------------------------
say "4. Rules and indexes reached the real project"
# ---------------------------------------------------------------------------
#
# firebase.json uses the multi-database array form, and firebase-tools does not
# read that form for the *emulator* — which is why the emulator runs open. That
# makes "does `firebase deploy` honour it for a real project" an open question,
# and an unanswered one means the browser-side rules may not be deployed at all.

for db in "(default)" "$ORDERS_DB"; do
  if gcloud firestore databases describe --database="$db" \
       >/dev/null 2>&1; then
    pass "database '$db' exists"
  else
    fail "database '$db' does not exist — run scripts/gcp_setup.sh"
  fi
done

# The collection-group index is what due_negotiations() queries on. Without it
# every tick fails FAILED_PRECONDITION, and the failure is per-project so the
# service still looks healthy.
idx=$(gcloud firestore indexes fields list --database='(default)' \
  --format='value(name)' 2>/dev/null | grep -c "next_action_due_at" || true)
if [[ "${idx:-0}" -ge 1 ]]; then
  pass "next_action_due_at field index present ($idx)"
else
  fail "no next_action_due_at index — every tick will fail FAILED_PRECONDITION"
  note "  make deploy-rules PROJECT_ID=$PROJECT_ID"
fi

note "Rules themselves cannot be read back by gcloud. Check by eye, once:"
note "  console.cloud.google.com/firestore/databases/-default-/rules"
note "  console.cloud.google.com/firestore/databases/$ORDERS_DB/rules"

# ---------------------------------------------------------------------------
say "5. Something has actually ticked"
# ---------------------------------------------------------------------------

ticks=$(gcloud logging read \
  "resource.labels.service_name=\"$TICK_SERVICE\" jsonPayload.message=\"tick\"" \
  --limit=5 --format='value(timestamp)' --freshness=1h 2>/dev/null | wc -l \
  | tr -d ' ')

if [[ "${ticks:-0}" -ge 1 ]]; then
  pass "$ticks tick(s) logged in the last hour — Scheduler is driving it"
else
  huh "no tick logged in the last hour"
  note "Fine if you deployed less than a minute ago. If it persists:"
  note "  gcloud scheduler jobs describe cinema-tick --location=$REGION"
  note "  gcloud logging read 'resource.labels.service_name=\"$TICK_SERVICE\"' --limit=20"
fi

# ---------------------------------------------------------------------------
printf '\n\033[1m%s\033[0m\n' "Result"
printf '  %d passed, %d failed, %d could not be determined\n' \
  "$PASSED" "$FAILED" "$UNKNOWN"

if (( FAILED > 0 )); then
  printf '\n\033[31mThe deployment is not correct.\033[0m Fix the FAILs above.\n\n'
  exit 1
fi
if (( UNKNOWN > 0 )); then
  printf '\n\033[33mInconclusive.\033[0m Nothing failed, but something could not be\n'
  printf 'checked — and an unchecked guardrail is not a working guardrail.\n\n'
  exit 3
fi
printf '\n\033[32mVerified.\033[0m\n\n'
