# firebase-admin ships no type information, so the token payload arrives as a
# plain dict and every call into it is Unknown. Scoped here rather than
# loosened repo-wide, same as repository.py does for the Firestore client.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportExplicitAny=false, reportAny=false, reportMissingTypeStubs=false
"""Proving who is asking, before anything touches money.

Only the approval service uses this. The tick loop has no authenticated
callers — it is woken by Cloud Scheduler and acts on its own behalf — and it
has no route that could spend anything, which is the point of keeping the two
services apart.

## Why firebase-admin rather than checking the JWT by hand

Two reasons, and the second is the one that decided it.

Verifying a Firebase ID token properly means fetching Google's rotating public
certificates, checking the signature, the issuer, the audience and the expiry.
That is a well-known list of things to get subtly wrong.

More practically: the Auth emulator issues **unsigned** tokens. A correct
hand-rolled verifier would reject every one of them, so the whole approval path
would be untestable without a real Firebase project — which is exactly what we
do not have. ``firebase-admin`` honours ``FIREBASE_AUTH_EMULATOR_HOST`` and
skips signature checks against it, so the same code runs in tests and in
production.

## 401 and 403 are different answers

"You are not signed in" and "you are signed in and still may not do this" are
different problems with different fixes, and the second one is worth seeing in
a log. An agent service account presenting its own token gets a 403 — and that
is a line in an audit trail rather than a shrug.
"""

import logging
from dataclasses import dataclass
from typing import Annotated, Any

import firebase_admin
from fastapi import Depends, HTTPException, Request
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials

from orchestrator.settings import Settings

log = logging.getLogger("orchestrator.auth")

PRODUCER_ROLE = "producer"
"""The custom claim that separates a human buyer from everything else.

Set out-of-band by an admin — ``scripts/grant_producer.py`` — never by anything
the agent runs. A claim a caller could mint for itself would not be a claim.
"""


@dataclass(frozen=True, slots=True)
class Producer:
    """A verified human who is allowed to approve purchases."""

    uid: str
    email: str

    @property
    def display(self) -> str:
        return self.email or self.uid


def init_firebase(settings: Settings) -> None:
    """Initialise the Firebase app once, idempotently.

    Safe to call from a lifespan that may run more than once in tests. Uses
    application-default credentials in production and nothing at all against
    the emulator, which needs none.
    """
    if firebase_admin._apps:  # pyright: ignore[reportPrivateUsage]
        return
    try:
        _ = firebase_admin.initialize_app(
            credentials.ApplicationDefault(), {"projectId": settings.gcp_project}
        )
    except Exception:
        # No application-default credentials, which is the normal case on a
        # laptop pointed at the emulator. Token verification still works there
        # because the emulator's tokens are unsigned.
        _ = firebase_admin.initialize_app(options={"projectId": settings.gcp_project})


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="expected an Authorization: Bearer <firebase-id-token> header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


def verify_token(request: Request) -> dict[str, Any]:
    """Decode and verify the caller's Firebase ID token, or refuse."""
    token = _bearer_token(request)
    try:
        return firebase_auth.verify_id_token(token)
    except Exception as exc:
        # Deliberately not echoing the library's message back to the caller:
        # expired, malformed and wrong-audience are all "your token is no
        # good", and the detail belongs in our logs rather than in a response
        # to someone who may be probing.
        log.warning("rejected an ID token", extra={"reason": type(exc).__name__})
        raise HTTPException(
            status_code=401,
            detail="invalid or expired ID token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


Claims = Annotated[dict[str, Any], Depends(verify_token)]
"""The verified token payload, injected by FastAPI."""


def require_producer(claims: Claims) -> Producer:
    """The dependency every money-touching route hangs off.

    A valid token is not enough. The ``producer`` claim is what distinguishes a
    human who may buy from any other identity that happens to hold a token —
    including the agent's own service account, which gets a 403 here and is
    separately denied by IAM at the database.
    """
    if claims.get("role") != PRODUCER_ROLE:
        uid = str(claims.get("uid") or claims.get("sub") or "unknown")
        log.warning("refused a non-producer", extra={"uid": uid})
        raise HTTPException(
            status_code=403,
            detail=(
                "this identity is signed in but is not a producer. "
                "Only a human with the producer claim can approve a purchase."
            ),
        )
    return Producer(
        uid=str(claims.get("uid") or claims.get("sub") or ""),
        email=str(claims.get("email") or ""),
    )
