#!/usr/bin/env python3
"""Make someone a producer. Run by a human, deliberately.

    uv run python scripts/grant_producer.py producer@example.com
    uv run python scripts/grant_producer.py --revoke producer@example.com
    uv run python scripts/grant_producer.py --list

The ``producer`` custom claim is the only thing separating a human who may
approve a purchase from any other identity holding a valid token — including
the agent's own service account. Nothing the agent runs can set it: custom
claims are writable only through the Firebase Admin SDK with administrative
credentials, which is what makes the claim mean anything at all. A permission
a caller could grant itself is not a permission.

## Against the emulator

Set ``FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099`` and this talks to the
emulator instead, creating the user if they do not exist yet. That is the local
setup path, and it is the same code that runs against the real project.

## Against a real project

Needs application-default credentials with permission to administer Firebase
Auth::

    gcloud auth application-default login
    CINEMA_GCP_PROJECT=your-project uv run python scripts/grant_producer.py you@example.com

The user must already exist there — this grants a claim, it does not create
accounts in a live project, and silently signing someone up because their email
was mistyped is not a thing this should do.

## The claim reaches the token on next sign-in

Custom claims are baked into an ID token when it is issued, so whoever you just
granted has to sign out and back in before the approval endpoint sees it. Their
existing token is unchanged and will keep getting a 403 until it expires.
"""

# firebase-admin ships no type information.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportMissingTypeStubs=false, reportAny=false
# pyright: reportUnknownArgumentType=false
import argparse
import os
import sys

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from orchestrator.auth import PRODUCER_ROLE
from orchestrator.settings import Settings


def _connect(settings: Settings) -> bool:
    """Initialise firebase-admin. Returns True when pointed at the emulator."""
    emulated = bool(os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"))
    if not firebase_admin._apps:  # pyright: ignore[reportPrivateUsage]
        if emulated:
            _ = firebase_admin.initialize_app(
                options={"projectId": settings.gcp_project}
            )
        else:
            _ = firebase_admin.initialize_app(
                credentials.ApplicationDefault(), {"projectId": settings.gcp_project}
            )
    return emulated


def _find_or_create(email: str, *, emulated: bool) -> firebase_auth.UserRecord:
    try:
        return firebase_auth.get_user_by_email(email)
    except firebase_auth.UserNotFoundError:
        if not emulated:
            raise
        print(f"No such user in the emulator; creating {email}.")
        return firebase_auth.create_user(email=email)


def _show(user: firebase_auth.UserRecord) -> str:
    role = (user.custom_claims or {}).get("role", "")
    return f"  {user.email or user.uid:40s} {role or '-'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("email", nargs="?", help="who to grant or revoke")
    _ = parser.add_argument(
        "--revoke", action="store_true", help="remove the claim instead"
    )
    _ = parser.add_argument(
        "--list", action="store_true", help="show every user and their role"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    emulated = _connect(settings)
    where = (
        f"the Auth emulator at {os.environ['FIREBASE_AUTH_EMULATOR_HOST']}"
        if emulated
        else f"project {settings.gcp_project}"
    )

    if args.list:
        print(f"Users in {where}:")
        for user in firebase_auth.list_users().iterate_all():
            print(_show(user))
        return 0

    if not args.email:
        parser.print_usage()
        print("\nGive an email address, or --list.")
        return 2

    email: str = args.email
    try:
        user = _find_or_create(email, emulated=emulated)
    except firebase_auth.UserNotFoundError:
        print(f"No user {email} in {where}.")
        print("They have to sign in to the app once before they can be granted.")
        return 1

    if args.revoke:
        firebase_auth.set_custom_user_claims(user.uid, None)
        # Revoking the claim does not invalidate tokens already issued, so the
        # sessions carrying it are killed too. Leaving them alive would mean a
        # revoked producer could still approve a purchase for up to an hour.
        firebase_auth.revoke_refresh_tokens(user.uid)
        print(f"Revoked producer from {email} ({user.uid}) in {where}.")
        print("Their existing sessions have been invalidated.")
        return 0

    firebase_auth.set_custom_user_claims(user.uid, {"role": PRODUCER_ROLE})
    print(f"Granted producer to {email} ({user.uid}) in {where}.")
    print("They must sign out and back in — the claim is baked into a new token.")
    if not emulated:
        print("\nThis identity can now approve purchases. There is no second gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
