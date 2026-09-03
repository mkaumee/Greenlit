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
API_SA="${API_SA:-cinema-api}"
ORDERS_DB="${ORDERS_DB:-orders}"
TICK_SERVICE="${TICK_SERVICE:-cinema-tick}"
APPROVALS_SERVICE="${APPROVALS_SERVICE:-cinema-approvals}"
API_SERVICE="${API_SERVICE:-cinema-api}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID is not set." >&2
  echo "  PROJECT_ID=your-project-id $0" >&2
  exit 2
fi
command -v gcloud >/dev/null || { echo "gcloud is not installed." >&2; exit 2; }

AGENT_EMAIL="${AGENT_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
APPROVALS_EMAIL="${APPROVALS_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
API_EMAIL="${API_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

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
API_URL=$(gcloud run services describe "$API_SERVICE" \
  --region="$REGION" --format='value(status.url)' 2>/dev/null || true)

if [[ -n "$TICK_URL" ]]; then pass "tick: $TICK_URL"
else fail "tick service not found"; fi

if [[ -n "$APPROVALS_URL" ]]; then pass "approvals: $APPROVALS_URL"
else fail "approvals service not found"; fi

# API_SERVICE was declared at the top of this script and used nowhere. The one
# service a producer's browser actually talks to — chat, script upload, mailbox
# connect — was absent from the verification entirely.
if [[ -n "$API_URL" ]]; then pass "api: $API_URL"
else fail "api service not found"; fi

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

body_of() {
  # The whole response body, or empty. `http` truncates to 160 bytes for its
  # one-line reports, which is right there and useless for JSON.
  curl -sS --max-time 20 "$@" 2>/dev/null || true
}

# One field out of a /health body. python3 rather than jq, which Cloud Shell
# does not guarantee, and rather than grep, which is how the last reading of a
# deployed value came back cut in half.
field() {
  python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except Exception:
    print("")
' "$1" 2>/dev/null
}

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

if [[ -n "$API_URL" ]]; then
  # Public on purpose, same as approvals: Cloud Run IAM cannot validate a
  # Firebase ID token, so auth.py is the gate. What must be true is that an
  # anonymous question about somebody's production is refused.
  check_http "api /health" 200 "$API_URL/health"

  check_http "an unauthenticated chat is refused" 401 -XPOST \
    -H 'Content-Type: application/json' \
    -d '{"project_id":"none","question":"what needs me?"}' \
    "$API_URL/chat"
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

    # The api service serves the producer's browser and is the newest way this
    # could quietly stop being true: one wrong role on it and the identity
    # behind every chat message and script upload can write purchase orders.
    if gcloud iam service-accounts describe "$API_EMAIL" >/dev/null 2>&1; then
      if try_read "$API_EMAIL" "$ORDERS_DB"; then
        fail "the api account CAN read the '$ORDERS_DB' database"
        note "That account serves the browser. It must not reach money."
        note "Look for an unconditioned roles/datastore.user on $API_SA."
      else
        pass "the api account cannot touch '$ORDERS_DB' either"
      fi
    else
      huh "no $API_SA account yet — run scripts/deploy.sh"
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
say "6. A human can actually sign in"
# ---------------------------------------------------------------------------
#
# Added after this script reported 12 passed, 0 failed on a project where
# signing in was impossible. Every check above concerns the agent; none of them
# noticed that Firebase Authentication had never been switched on, so the panel
# deployed, served, and refused every visitor with
# `auth/configuration-not-found`.
#
# Two separate facts, and they fail differently:
#   - the Identity Toolkit config exists at all  (the console's "Get started")
#   - google.com is present and enabled          (the provider toggle)
#
# Neither has a gcloud subcommand. Straight to the REST API, as with check 3 —
# and for the same reason, the token is captured with stderr going to a file
# rather than merged with 2>&1. Folding a warning into the token yields an
# Authorization header with a newline in it, which curl rejects with error 43
# in a way that looks nothing like the auth problem it actually is.

auth_errfile=$(mktemp)
if ! id_token=$(gcloud auth print-access-token 2>"$auth_errfile"); then
  huh "could not mint an access token to query Identity Toolkit"
  note "$(tr '\n' ' ' <"$auth_errfile" | head -c 200)"
else
  id_token=$(tr -d '[:space:]' <<<"$id_token")

  # x-goog-user-project is not optional here, and its absence is invisible
  # until it bites. `gcloud auth print-access-token` yields a bare user token
  # carrying no project; Firestore and Cloud Run infer one, Identity Toolkit
  # refuses to. Without the header the 403 comes back blaming SERVICE_DISABLED
  # on a project number you have never seen, and enabling the API on the right
  # project cannot fix it. firebase-tools sets this header on every call.
  cfg=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $id_token" \
    -H "x-goog-user-project: ${PROJECT_ID}" \
    "https://identitytoolkit.googleapis.com/v2/projects/${PROJECT_ID}/config" \
    2>/dev/null || echo "000")

  case "$cfg" in
    200)
      pass "Firebase Authentication is initialised"

      # The single provider, not the list. The list response nests the id
      # inside a resource name and guessing at that shape is how this script
      # previously grew a check that never tested anything. A direct GET
      # answers 404 when the provider is absent, which needs no parsing.
      idp=$(curl -sS -w $'\n%{http_code}' \
        -H "Authorization: Bearer $id_token" \
        -H "x-goog-user-project: ${PROJECT_ID}" \
        "https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/defaultSupportedIdpConfigs/google.com" \
        2>/dev/null || printf '\n000')
      idp_code=$(tail -n1 <<<"$idp")
      idp_body=$(sed '$d' <<<"$idp")

      if [[ "$idp_code" == "200" ]] && grep -q '"enabled":[[:space:]]*true' <<<"$idp_body"; then
        pass "Google sign-in is enabled"
      elif [[ "$idp_code" == "200" || "$idp_code" == "404" ]]; then
        fail "Google sign-in is not enabled — the panel will refuse every visitor"
        note "curl -X POST -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \\"
        note "  -H 'x-goog-user-project: ${PROJECT_ID}' \\"
        note "  -H 'Content-Type: application/json' -d '{\"enabled\": true}' \\"
        note "  'https://identitytoolkit.googleapis.com/admin/v2/projects/${PROJECT_ID}/defaultSupportedIdpConfigs?idpId=google.com'"
      else
        huh "could not read the Google provider config (HTTP $idp_code)"
      fi
      ;;
    404)
      fail "Firebase Authentication was never switched on (auth/configuration-not-found)"
      note "This is what the browser reports when nobody can sign in. Fix:"
      note "  console: Firebase → Authentication → Get started"
      note "  or:      curl -X POST -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \\"
      note "             -H 'x-goog-user-project: ${PROJECT_ID}' \\"
      note "             -H 'Content-Type: application/json' -d '{}' \\"
      note "             'https://identitytoolkit.googleapis.com/v2/projects/${PROJECT_ID}/identityPlatform:initializeAuth'"
      ;;
    *)
      # Never pass on a shrug. 403 here usually means identitytoolkit is not
      # enabled, which is a different problem from Auth being absent.
      huh "could not read the Identity Toolkit config (HTTP $cfg)"
      note "gcloud services enable identitytoolkit.googleapis.com --project $PROJECT_ID"
      ;;
  esac
fi
rm -f "$auth_errfile"

# ---------------------------------------------------------------------------
say "7. What is actually reasoning"
# ---------------------------------------------------------------------------
#
# Every question in this section used to be answered by composing a gcloud
# expression on the spot, and one of those came back cut in half because the
# env is a list of dicts and it was split on commas. The services already
# report all of it on /health; this reads the body instead of the status code.
#
# Nothing here can FAIL. A scripted brain and an off mailbox are legitimate
# configurations — they are the defaults — and the point is that neither is
# visible from any screen in the product. Reporting them is the whole job.

if [[ -n "$TICK_URL" ]]; then
  token=$(gcloud auth print-identity-token 2>/dev/null || true)
  tick_health=$(body_of -H "Authorization: Bearer $token" "$TICK_URL/health")

  brain=$(field brain_backend <<<"$tick_health")
  case "$brain" in
    main-agent)
      pass "tick brain: main-agent ($(field gemini_model <<<"$tick_health") via $(field gemini_credentials <<<"$tick_health"))"
      ;;
    scripted)
      huh "tick brain: SCRIPTED — a regex and a word list write the emails"
      note "every negotiation message is template text; it looks fine until read"
      ;;
    *) huh "tick brain: could not read /health" ;;
  esac

  mail=$(field mail_backend <<<"$tick_health")
  case "$mail" in
    gmail)  pass "mail: gmail — this deployment sends real email" ;;
    memory) huh "mail: memory — nothing is sent to anyone" ;;
    *)      huh "mail: could not read /health" ;;
  esac

  case "$(field research_key_present <<<"$tick_health")" in
    True|true) pass "research key: configured" ;;
    *)         huh "research key: absent — price bands would be unsourced" ;;
  esac
fi

if [[ -n "$API_URL" ]]; then
  api_health=$(body_of "$API_URL/health")
  api_brain=$(field brain_backend <<<"$api_health")
  case "$api_brain" in
    main-agent)
      pass "api brain: main-agent ($(field gemini_model <<<"$api_health") via $(field gemini_credentials <<<"$api_health"))"
      ;;
    scripted)
      huh "api brain: SCRIPTED — the chat cannot reason and PDFs are refused"
      ;;
    *) huh "api brain: could not read /health" ;;
  esac

  # The two services reason independently and are configured separately. One
  # on the real brain and one on the fake is a deployment where the chat is
  # thoughtful and the negotiations are templates, or the reverse.
  if [[ -n "$brain" && -n "$api_brain" && "$brain" != "$api_brain" ]]; then
    huh "tick and api disagree about the brain ($brain vs $api_brain)"
  fi
fi

# The research key itself: never printed, because a terminal may be on a
# screen share and a key belongs in Secret Manager. Length and last four are
# enough to tell a real one from the word somebody pasted, which is the only
# question anyone actually has about it.
research_key=$(gcloud run services describe "$TICK_SERVICE" --region="$REGION" \
  --format=json 2>/dev/null | python3 -c '
import json, sys
try:
    spec = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
except Exception:
    sys.exit(0)
for entry in spec.get("env", []):
    if entry.get("name") == "PARALLEL_API_KEY":
        print(entry.get("value", ""))
        break
' 2>/dev/null || true)

placeholders="your-key your-api-key your_key api-key changeme xxx todo"
if [[ -z "$research_key" ]]; then
  note "research key: not set on $TICK_SERVICE"
elif [[ " $placeholders " == *" ${research_key,,} "* ]]; then
  fail "research key is the placeholder '$research_key', not a key"
  note "search fails on every call; the model then answers from memory, and"
  note "its price bands and supplier URLs are invented rather than sourced"
else
  note "research key: ${#research_key} chars, ending ...${research_key: -4}"
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
