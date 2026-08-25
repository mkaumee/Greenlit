#!/usr/bin/env python3
"""Mint a Gmail refresh token. Run by a human, on a machine with a browser.

The service never runs this. It opens a consent screen, which needs a person,
and it is the only place in the repository that touches an OAuth client secret.

    uv run python scripts/oauth_bootstrap.py

Do it once per mailbox — the producer agent and, later, the supplier test
account. See ``docs/oauth-runbook.md`` for the Google Cloud side.

The token is written through whichever ``TokenStore`` configuration selects, so
the same script works before and after there is a GCP project: today it lands
in a gitignored file, and once ``CINEMA_TOKEN_BACKEND=secret-manager`` is set it
goes to Secret Manager instead.
"""

# google_auth_oauthlib's flow objects are untyped, so the credential coming
# back is Any no matter how it is annotated here.
# pyright: reportAny=false, reportUnknownMemberType=false, reportMissingTypeStubs=false
import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from orchestrator.gmail import token_store_for
from orchestrator.settings import GMAIL_SCOPES, Settings, TokenBackend


def _cannot_work_here(client_secrets: Path) -> str:
    """Why this run cannot succeed, or "" if it can.

    Both of these produce an error far from their cause, so they are checked
    before a browser is opened rather than after.
    """
    # A Desktop client permits http://localhost:<any port>, which is what
    # run_local_server needs. A Web client requires every redirect URI to be
    # registered, and the port here is random — so Google answers
    # "Error 400: redirect_uri_mismatch", which sounds like a console setting
    # to fix rather than the wrong client type to recreate.
    try:
        kinds = list(json.loads(client_secrets.read_text()))
    except (OSError, ValueError) as cause:
        return f"Could not read {client_secrets}: {cause}"

    if "installed" not in kinds:
        kind = kinds[0] if kinds else "unrecognised"
        return (
            f"{client_secrets} is a '{kind}' OAuth client, not a Desktop app.\n\n"
            "Desktop clients allow http://localhost on any port, which this\n"
            "flow needs. A Web client only allows redirect URIs you register,\n"
            "and the port here is chosen fresh each run — so Google refuses\n"
            "with 'Error 400: redirect_uri_mismatch'.\n\n"
            "Create the right one: APIs & Services > Credentials >\n"
            "Create credentials > OAuth client ID > **Desktop app**, download\n"
            f"it, and replace {client_secrets}."
        )

    # run_local_server binds a port on *this* machine and Google redirects the
    # browser to localhost on that port. In Cloud Shell the browser is on your
    # laptop, where nothing is listening, so consent completes and the script
    # waits forever for a callback that cannot arrive.
    if (
        os.environ.get("CLOUD_SHELL") or os.environ.get("GOOGLE_CLOUD_SHELL")
    ) and not os.environ.get("CINEMA_ALLOW_HEADLESS"):
        return (
            "This cannot complete in Cloud Shell.\n\n"
            "Consent needs a browser on the same machine as this script: the\n"
            "flow listens on localhost and Google redirects the browser there.\n"
            "In Cloud Shell the browser is on your laptop, so the callback\n"
            "never arrives and this would hang.\n\n"
            "Run it on your laptop instead. Nothing needs to come back here —\n"
            "with CINEMA_TOKEN_BACKEND=secret-manager the token goes straight\n"
            "to Secret Manager, which is where Cloud Run reads it.\n\n"
            "Set CINEMA_ALLOW_HEADLESS=1 to override, if you have forwarded\n"
            "the port yourself."
        )
    return ""


def main() -> int:
    settings = Settings()
    client_secrets: Path = settings.oauth_client_secrets

    if not client_secrets.exists():
        print(f"No OAuth client at {client_secrets}.\n")
        print("Create one in Google Cloud console:")
        print("  APIs & Services > Credentials > Create credentials")
        print("  > OAuth client ID > Desktop app, then download the JSON.")
        print(f"\nSave it as {client_secrets}, or point CINEMA_OAUTH_CLIENT_SECRETS")
        print("somewhere else. See docs/oauth-runbook.md.")
        return 2

    # Two failures that both surface as something misleading if left to run.
    if problem := _cannot_work_here(client_secrets):
        print(problem)
        return 2

    print(f"Scopes: {', '.join(s.rsplit('/', 1)[-1] for s in GMAIL_SCOPES)}")
    print("A browser window will open. Sign in as the mailbox you are setting up")
    print("— not your personal account, unless that is genuinely the agent's.\n")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secrets), scopes=list(GMAIL_SCOPES)
    )
    # access_type=offline and prompt=consent together are what actually produce
    # a refresh token. Without prompt=consent, a mailbox that has already been
    # authorised returns an access token only, and the run looks like it worked
    # right up until the first token expiry.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        print("\nNo refresh token came back.")
        print("Revoke the app's access at https://myaccount.google.com/permissions")
        print("and run this again — Google only issues one on first consent.")
        return 1

    store = token_store_for(settings)
    store.write(str(refresh_token))

    where = (
        f"Secret Manager secret {settings.refresh_token_secret!r}"
        if settings.token_backend is TokenBackend.SECRET_MANAGER
        else str(settings.refresh_token_path)
    )
    print(f"\nRefresh token stored in {where}.")

    if settings.token_backend is TokenBackend.FILE:
        print("That path is gitignored. Do not move it somewhere that is not.")

    print("\nIf the consent screen is still in testing mode, this token dies in")
    print("seven days — shorter than a negotiation. See docs/oauth-runbook.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
