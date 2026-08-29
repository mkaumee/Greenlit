#!/usr/bin/env bash
#
# Print the redirect URI to register on the OAuth client, and where to put it.
#
#   PROJECT_ID=your-project-id ./scripts/oauth_redirect_uri.sh
#
# Read-only. Changes nothing, so it is safe to run whenever you need the value
# again — which you will, because it is needed before the connect flow works
# and there is nowhere in the product that displays it.
#
# ---------------------------------------------------------------------------
# Why this is a separate script rather than a line in deploy.sh
# ---------------------------------------------------------------------------
#
# The value depends on a deployed service, and registering it is a manual step
# in the Cloud Console that no amount of scripting can do for you: OAuth 2.0
# client redirect URIs are not exposed by gcloud. `gcloud alpha iap
# oauth-clients` looks like the answer and is not — it manages IAP's own brand
# clients, not the Web client a browser consents against.
#
# So the sequence is: deploy, run this, paste, then deploy again once the
# /mailbox routes exist. The URL itself does not move between deploys — Cloud
# Run derives it from the service name, region and project — so registering it
# before the routes ship is not premature, it is the right order.
#
# ---------------------------------------------------------------------------
# What NOT to add
# ---------------------------------------------------------------------------
#
# Authorised JavaScript origins. The reflex is to fill in both boxes, and here
# it is wrong: the browser never talks to Google's token endpoint. It opens a
# consent URL in a popup, Google redirects to our backend, and the backend does
# the code exchange with the client secret. A JS origin would be inert — which
# is worse than an error, because it looks like you configured something.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
APPROVALS_SERVICE="${APPROVALS_SERVICE:-cinema-approvals}"
LOCAL_PORT="${LOCAL_PORT:-8000}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "PROJECT_ID is not set." >&2
  echo "  PROJECT_ID=your-project-id $0" >&2
  exit 2
fi

command -v gcloud >/dev/null || {
  echo "gcloud is not installed." >&2
  echo "  Cloud Shell has it already — see docs/deploy-runbook.md." >&2
  exit 2
}

URL=$(gcloud run services describe "$APPROVALS_SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" \
  --format='value(status.url)' 2>/dev/null || true)

if [[ -z "$URL" ]]; then
  cat >&2 <<EOF
  ✗ no $APPROVALS_SERVICE service in $PROJECT_ID ($REGION).

  The redirect URI is derived from that service's URL, so it cannot be known
  until it exists. Deploy first:

    PROJECT_ID=$PROJECT_ID ./scripts/deploy.sh

  then run this again.
EOF
  exit 3
fi

printf '\n\033[1mRegister this redirect URI\033[0m\n\n'
printf '    %s/mailbox/callback\n' "$URL"
printf '\n  and, if you want to run the flow locally as well:\n\n'
printf '    http://localhost:%s/mailbox/callback\n' "$LOCAL_PORT"

cat <<EOF

$(printf '\033[1mWhere\033[0m')

  https://console.cloud.google.com/apis/credentials?project=$PROJECT_ID

  Open your OAuth 2.0 Client ID of type **Web application** — the one you
  already created. Under "Authorised redirect URIs", Add URI, paste, Save.

  Leave "Authorised JavaScript origins" empty. The browser never calls
  Google's token endpoint; the backend does the exchange. An origin there
  would do nothing, which is worse than an error because it looks configured.

$(printf '\033[1mWhile you are on that screen\033[0m')

  Check the consent screen's Test users list contains every Google account
  that will connect a mailbox, including the one you hand to a judge. An
  account that is not on it gets "Access blocked: … has not completed the
  Google verification process", which reads like a broken app rather than a
  missing list entry.

  Audience stays **Testing**, deliberately. gmail.modify is a restricted
  scope, so publishing would force Google verification plus a CASA security
  assessment. The cost is that refresh tokens expire after seven days — which
  is why reconnecting is a button in the panel rather than a script.

EOF
