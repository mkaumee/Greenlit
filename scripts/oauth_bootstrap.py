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
# pyright: reportUnknownVariableType=false
import argparse
import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from orchestrator.gmail import token_store_for
from orchestrator.settings import GMAIL_SCOPES, Settings, TokenBackend


def _headless_hint(project: str) -> str:
    """How to finish this without a browser on this machine.

    Cloud Shell can serve a port publicly, so a *registered* redirect URI works
    where localhost cannot. Nothing has to be listening on it — the code lands
    in the browser's address bar either way, which is what the old
    out-of-band flow did before Google withdrew it in 2023.
    """
    host = os.environ.get("WEB_HOST", "")
    suggested = (
        f"https://8080-{host}/" if host else "https://8080-<your-cloud-shell-host>/"
    )
    return (
        "\n--- or complete it here, by hand ---\n\n"
        "1. Use a **Web application** OAuth client, and register this exact\n"
        "   redirect URI on it (APIs & Services > Credentials > your client >\n"
        "   Authorised redirect URIs):\n\n"
        f"       {suggested}\n\n"
        "   In Cloud Shell that URL is the Web Preview address for port 8080 —\n"
        "   the Web Preview button, 'Preview on port 8080'. Copy it exactly,\n"
        "   including the trailing slash.\n\n"
        "2. Then run this, as ONE line — a pasted backslash-continuation\n"
        "   breaks if your terminal inserts a blank line after it:\n\n"
        f"       CINEMA_TOKEN_BACKEND=secret-manager CINEMA_GCP_PROJECT={project} "
        f"uv run python scripts/oauth_bootstrap.py --redirect-uri '{suggested}'\n\n"
        "   CINEMA_TOKEN_BACKEND matters: without it the token is written to a\n"
        "   local file, which in Cloud Shell is wiped and is not where Cloud Run\n"
        "   looks anyway.\n\n"
        "   It prints a link. Open it, consent, and the browser lands on a page\n"
        "   that will probably fail to load — that does not matter. Copy the\n"
        "   `code=` value out of the address bar and paste it back here."
    )


def _consent_by_hand(client_secrets: Path, redirect_uri: str) -> object | None:
    """Consent without a listener: the human carries the code across.

    Used when the browser is not on this machine. Google redirects to a URI we
    registered rather than to localhost; whether anything answers there is
    irrelevant, because the authorisation code is in the query string and the
    person reading the address bar can move it.
    """
    flow = Flow.from_client_secrets_file(
        str(client_secrets), scopes=list(GMAIL_SCOPES), redirect_uri=redirect_uri
    )
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print("Open this, and consent as the agent's mailbox:\n")
    print(f"    {auth_url}\n")
    print("You will land on a page that may not load. That is fine — the code")
    print("is in the address bar, after `code=` and before any `&`.\n")

    try:
        code = input("Paste the code: ").strip()
    except EOFError:
        print("\nNo code entered.")
        return None
    if not code:
        print("No code entered.")
        return None

    # Google percent-encodes the code in the address bar; paste is usually
    # already decoded, but %2F for the leading slash is common enough to fix.
    code = code.replace("%2F", "/").replace("%2f", "/")

    try:
        _ = flow.fetch_token(code=code)
    except Exception as cause:
        print(f"\nGoogle refused the code: {cause}")
        print("Codes are single-use and expire in minutes — start again.")
        return None
    return flow.credentials


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
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--redirect-uri",
        default="",
        help=(
            "Complete consent by pasting the code instead of listening on "
            "localhost. Needs this exact URI registered on a Web-application "
            "OAuth client. This is how to do it from Cloud Shell."
        ),
    )
    args = parser.parse_args()
    redirect_uri: str = str(args.redirect_uri).strip()

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

    # Both of these are about the localhost listener, so they do not apply
    # when consent is being completed by hand against a registered redirect.
    if not redirect_uri and (problem := _cannot_work_here(client_secrets)):
        print(problem)
        print(_headless_hint(settings.gcp_project))
        return 2

    print(f"Scopes: {', '.join(s.rsplit('/', 1)[-1] for s in GMAIL_SCOPES)}")
    print("Sign in as the mailbox you are setting up — not your personal")
    print("account, unless that is genuinely the agent's.\n")

    # access_type=offline and prompt=consent together are what actually produce
    # a refresh token. Without prompt=consent, a mailbox that has already been
    # authorised returns an access token only, and the run looks like it worked
    # right up until the first token expiry.
    if redirect_uri:
        credentials = _consent_by_hand(client_secrets, redirect_uri)
        if credentials is None:
            return 1
    else:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secrets), scopes=list(GMAIL_SCOPES)
        )
        credentials = flow.run_local_server(
            port=0, access_type="offline", prompt="consent"
        )

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
        if os.environ.get("CLOUD_SHELL") or os.environ.get("GOOGLE_CLOUD_SHELL"):
            # Easy to miss: the run succeeds, the token exists, and the
            # deployment still cannot see it. Cloud Shell's home directory is
            # also not permanent, so this would evaporate.
            print(
                "\n  WARNING: that is a file in Cloud Shell, which Cloud Run\n"
                "  cannot read and which this machine does not keep. Re-run with\n"
                "  CINEMA_TOKEN_BACKEND=secret-manager to put it where the\n"
                "  deployed service actually looks."
            )

    print("\nIf the consent screen is still in testing mode, this token dies in")
    print("seven days — shorter than a negotiation. See docs/oauth-runbook.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
