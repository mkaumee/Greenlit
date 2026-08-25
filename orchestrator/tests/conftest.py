# firebase-admin ships no type information, and an emulator's JSON response is
# Any by nature. Both are about the libraries rather than this code.
# pyright: reportAny=false, reportMissingTypeStubs=false
# pyright: reportUnknownMemberType=false
"""Fixtures for tests that need a real Firestore.

Real meaning the emulator, never a live project. Start it with
``make emulator`` (or ``firebase emulators:start --only firestore``) and these
run; without it they skip with a message rather than failing, so ``make test``
still works on a machine that has not set the emulator up yet.

``make e2e`` boots the emulator itself, which is where these are guaranteed to
actually execute.
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import ClassVar

import httpx
import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import AsyncClient

EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
AUTH_HOST = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
PROJECT_ID = os.environ.get("FIRESTORE_PROJECT_ID", "demo-cinema")
ORDERS_DATABASE = "orders"


def _wipe(database: str) -> None:
    """Empty one database. Each fixture clears its own.

    Both must be wiped independently — clearing ``(default)`` leaves a stale
    purchase order sitting in ``orders``, which is exactly the kind of leak
    that makes a guardrail test pass for the wrong reason.
    """
    _ = httpx.delete(
        f"http://{EMULATOR_HOST}/emulator/v1/projects/{PROJECT_ID}"
        f"/databases/{database}/documents",
        timeout=10.0,
    )


def _emulator_running() -> bool:
    try:
        response = httpx.get(f"http://{EMULATOR_HOST}/", timeout=1.0)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


@pytest.fixture
async def firestore() -> AsyncIterator[AsyncClient]:
    """A clean database per test.

    Skipping lives here rather than in a marker each test file has to import:
    asking for this fixture is already the statement "this test needs Firestore".

    Wiped before rather than after, so a failing test leaves its data behind for
    inspection in the emulator UI on :4000.
    """
    if not _emulator_running():
        pytest.skip(
            f"Firestore emulator not reachable at {EMULATOR_HOST}. "
            f"Start it with `make emulator`."
        )

    os.environ["FIRESTORE_EMULATOR_HOST"] = EMULATOR_HOST
    _wipe("(default)")

    client = AsyncClient(
        project=PROJECT_ID,
        credentials=AnonymousCredentials(),
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
async def orders_firestore() -> AsyncIterator[AsyncClient]:
    """A client on the separate ``orders`` database.

    Purchase orders live in their own database because Firestore rules do not
    apply to server SDKs and Firestore IAM cannot scope below a database — so
    the only way the agent's service account can be denied order writes is to
    give orders a database it has no binding on.

    A distinct fixture rather than a parameter, so a test that touches orders
    has to say so.
    """
    if not _emulator_running():
        pytest.skip(f"Firestore emulator not reachable at {EMULATOR_HOST}.")

    os.environ["FIRESTORE_EMULATOR_HOST"] = EMULATOR_HOST
    _wipe(ORDERS_DATABASE)

    client = AsyncClient(
        project=PROJECT_ID,
        credentials=AnonymousCredentials(),
        database=ORDERS_DATABASE,
    )
    try:
        yield client
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def _auth_emulator_running() -> bool:
    try:
        response = httpx.get(f"http://{AUTH_HOST}/", timeout=1.0)
    except httpx.HTTPError:
        return False
    return response.status_code < 500


class TokenMinter:
    """Issues real Firebase ID tokens from the Auth emulator.

    Real matters here. The alternative — overriding the FastAPI dependency and
    injecting a ``Producer`` straight into the route — would test the money
    logic while quietly skipping the part that decides who is allowed to reach
    it. These tokens go through ``firebase_admin.verify_id_token`` exactly as a
    browser's would; the emulator leaves them unsigned and firebase-admin skips
    the signature check, but every other claim is checked for real.
    """

    PASSWORD: ClassVar[str] = "correct-horse-battery-staple"

    _base: str
    _key: str

    def __init__(self, api_key: str = "fake-api-key") -> None:
        self._base = f"http://{AUTH_HOST}/identitytoolkit.googleapis.com/v1"
        self._key = api_key

    def create(self, email: str) -> str:
        """Sign a new user up. Returns their uid."""
        response = httpx.post(
            f"{self._base}/accounts:signUp",
            params={"key": self._key},
            json={
                "email": email,
                "password": self.PASSWORD,
                "returnSecureToken": True,
            },
            timeout=10.0,
        )
        _ = response.raise_for_status()
        uid: str = response.json()["localId"]
        return uid

    def sign_in(self, email: str) -> str:
        """Get a fresh ID token for an existing user.

        Separate from ``create`` because whatever claims a user has are baked
        into the token at issue time. Testing that a claim granted after signup
        actually reaches the API means signing in again afterwards, which is the
        same thing a real producer has to do.
        """
        response = httpx.post(
            f"{self._base}/accounts:signInWithPassword",
            params={"key": self._key},
            json={
                "email": email,
                "password": self.PASSWORD,
                "returnSecureToken": True,
            },
            timeout=10.0,
        )
        _ = response.raise_for_status()
        token: str = response.json()["idToken"]
        return token

    def mint(self, email: str, *, role: str = "") -> str:
        """Create a user, optionally give them a role, return their token."""
        import firebase_admin
        from firebase_admin import auth as firebase_auth

        uid = self.create(email)
        if role:
            assert firebase_admin._apps, (  # pyright: ignore[reportPrivateUsage]
                "init_firebase must run before setting custom claims"
            )
            firebase_auth.set_custom_user_claims(uid, {"role": role})
        return self.sign_in(email)


@pytest.fixture
def tokens() -> Iterator[TokenMinter]:
    """A clean Auth emulator, and a way to get tokens out of it.

    Skips rather than fails when the emulator is absent, matching the Firestore
    fixtures — but CI asserts that nothing skipped, because a green run that
    quietly skipped every approval test is worse than a red one.
    """
    if not _auth_emulator_running():
        pytest.skip(
            f"Auth emulator not reachable at {AUTH_HOST}. "
            f"Start it with `make emulator`."
        )

    os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = AUTH_HOST
    _ = httpx.delete(
        f"http://{AUTH_HOST}/emulator/v1/projects/{PROJECT_ID}/accounts", timeout=10.0
    )
    yield TokenMinter()
