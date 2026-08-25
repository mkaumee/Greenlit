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
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from orchestrator.gmail import token_store_for
from orchestrator.settings import GMAIL_SCOPES, Settings, TokenBackend


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
