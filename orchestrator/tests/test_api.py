# httpx .json() and Starlette's app.state are untyped by nature, and
# urllib's parse_qs is annotated to return Unknown-keyed dicts.
# The `tokens` fixture is requested for its side effect — standing the Auth
# emulator up before init_firebase runs — so it is deliberately unread.
# pyright: reportAny=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnusedParameter=false
"""The producer's browser-facing service.

Most of this file is about one document. ``/mailbox/callback`` arrives as a
redirect from Google carrying no Authorization header, so the single-use state
value is the *only* thing binding an authorization code to a producer. If it
can be replayed, reused, or outlived, somebody attaches their mailbox to
another producer's account — or has the agent send as them.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cinema_contracts import (
    ClockMode,
    ExtractedQuote,
    Money,
    NegotiationState,
    SceneMention,
)
from conftest import TokenMinter
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.api import ApiServices, app, build_api_services
from orchestrator.auth import init_firebase
from orchestrator.clock import ClockState, SimClock
from orchestrator.records import (
    ItemRecord,
    ItemStatus,
    NegotiationRecord,
    ProjectRecord,
    SupplierRecord,
)
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import Settings

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
UID = "producer-1"

SETTINGS = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    gcp_project="demo-cinema",
    oauth_client_id="client-id.apps.googleusercontent.com",
    oauth_client_secret="client-secret",
    oauth_redirect_uri="https://api.example.test/mailbox/callback",
)


def _services(client: AsyncClient) -> ApiServices:
    repo = FirestoreRepository(client)
    return ApiServices(
        settings=SETTINGS, client=client, repo=repo, clock=SimClock(repo)
    )


@pytest.fixture
async def api(firestore: AsyncClient, tokens: TokenMinter) -> httpx.AsyncClient:
    init_firebase(SETTINGS)
    app.state.services = _services(firestore)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _auth(
    tokens: TokenMinter, email: str = "producer@example.invalid"
) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.mint(email, role='producer')}"}


# --------------------------------------------------------------------------- #
# Starting a consent
# --------------------------------------------------------------------------- #


async def test_the_consent_url_asks_for_a_refresh_token_every_time(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    """Without prompt=consent Google returns a refresh token only on a first
    authorization — so reconnecting after the seven-day expiry would hand back
    an access token that dies in an hour, which looks like it worked until the
    next tick."""
    body = (await api.post("/mailbox/start", headers=_auth(tokens))).json()

    query = parse_qs(urlparse(body["authorize_url"]).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [SETTINGS.oauth_redirect_uri]


async def test_starting_a_consent_needs_a_producer(api: httpx.AsyncClient) -> None:
    assert (await api.post("/mailbox/start")).status_code == 401


async def test_each_consent_gets_its_own_state(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    # One token, two requests — the same producer connecting twice, which is
    # what reconnecting after the seven-day expiry looks like.
    headers = _auth(tokens)
    first = (await api.post("/mailbox/start", headers=headers)).json()
    second = (await api.post("/mailbox/start", headers=headers)).json()

    states = [
        parse_qs(urlparse(b["authorize_url"]).query)["state"][0]
        for b in (first, second)
    ]
    assert states[0] != states[1]
    assert len(states[0]) > 30, "guessable is the same as absent here"


# --------------------------------------------------------------------------- #
# The state document
# --------------------------------------------------------------------------- #


async def test_a_state_is_spent_exactly_once(firestore: AsyncClient) -> None:
    """The replay guard. Two callbacks with the same value — a double-clicked
    popup, or somebody replaying one — and only one may win."""
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("s-1", UID, T0)

    first = await repo.claim_oauth_state("s-1", T0, timedelta(minutes=10))
    second = await repo.claim_oauth_state("s-1", T0, timedelta(minutes=10))

    assert first == UID
    assert second is None


async def test_a_stale_state_is_refused(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("s-1", UID, T0)

    claimed = await repo.claim_oauth_state(
        "s-1", T0 + timedelta(minutes=11), timedelta(minutes=10)
    )

    assert claimed is None


async def test_an_unknown_state_is_refused(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)

    assert (
        await repo.claim_oauth_state("never-minted", T0, timedelta(minutes=10)) is None
    )


async def test_a_spent_state_is_gone_from_storage(firestore: AsyncClient) -> None:
    """Deleted rather than flagged. A used value that still exists is one more
    thing that can be read back."""
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("s-1", UID, T0)

    _ = await repo.claim_oauth_state("s-1", T0, timedelta(minutes=10))

    snapshot = await firestore.collection("oauth_states").document("s-1").get()
    assert not snapshot.exists


# --------------------------------------------------------------------------- #
# The callback
# --------------------------------------------------------------------------- #


async def test_the_callback_refuses_an_unknown_state(api: httpx.AsyncClient) -> None:
    response = await api.get(
        "/mailbox/callback", params={"code": "x", "state": "not-a-real-state"}
    )

    assert response.status_code == 400
    assert "expired" in response.text


async def test_a_cancelled_consent_is_not_an_error(api: httpx.AsyncClient) -> None:
    """They pressed Cancel. Nothing is wrong and nothing has changed."""
    response = await api.get("/mailbox/callback", params={"error": "access_denied"})

    assert response.status_code == 200
    assert "Not connected" in response.text


async def test_the_callback_says_nothing_about_why_a_state_failed(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Unknown, spent and expired read identically.

    A legitimate producer gains nothing from the difference and somebody
    probing for a live value gains a great deal.
    """
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("spent", UID, T0)
    _ = await repo.claim_oauth_state("spent", T0, timedelta(minutes=10))

    unknown = await api.get("/mailbox/callback", params={"code": "x", "state": "nope"})
    spent = await api.get("/mailbox/callback", params={"code": "x", "state": "spent"})

    assert unknown.text == spent.text


# --------------------------------------------------------------------------- #
# The boundary this service exists to keep
# --------------------------------------------------------------------------- #


def test_this_service_cannot_spend_money() -> None:
    """The reason it is not a few more routes on the approvals service.

    That account holds an unconditioned datastore.user because approving writes
    a purchase order. Hanging chat and script upload off it would run all of
    them as the one identity that can spend. Same shape of guard as the one
    asserting the tick app exposes no /approve route.
    """
    paths = [getattr(route, "path", "") for route in app.routes]

    assert not [p for p in paths if "approve" in p or "purchase" in p], paths
    assert "orders" not in set(ApiServices.__dataclass_fields__)
    assert "orders_client" not in set(ApiServices.__dataclass_fields__)


def test_the_composition_root_builds_no_orders_client() -> None:
    services = build_api_services(SETTINGS)
    try:
        assert not hasattr(services, "orders")
    finally:
        services.client.close()


def test_the_api_service_holds_no_client_for_the_orders_database() -> None:
    """Belt and braces on the same boundary, one layer down.

    Even with a wrong IAM binding there is nothing in this service's
    composition root that could reach purchase orders — no orders client, and
    the repository it holds has no method that writes one.
    """
    fields = set(ApiServices.__dataclass_fields__)

    assert "orders" not in fields
    assert "orders_client" not in fields
    assert not [m for m in dir(FirestoreRepository) if "purchase_order" in m]


# --------------------------------------------------------------------------- #
# Talking to the agent
# --------------------------------------------------------------------------- #


async def _owned_project(
    firestore: AsyncClient, project_id: str, owner: str
) -> FirestoreRepository:
    repo = FirestoreRepository(firestore)
    await repo.create_project(
        project_id,
        ProjectRecord(
            title="Kopitiam",
            clock=ClockState(
                sim_now=T0, real_anchor=T0, speed=0.0, mode=ClockMode.FROZEN
            ),
            created_at=T0,
            owner_uid=owner,
        ),
    )
    await repo.save_item(
        project_id,
        "mirror",
        ItemRecord(
            name="Mirror",
            category="prop",
            status=ItemStatus.SOURCING,
            mentions=[
                SceneMention(scene_number="12", line="He threw it at the mirror")
            ],
        ),
    )
    await repo.save_supplier(
        project_id,
        "sup1",
        SupplierRecord(name="Ah Seng Rentals", email="s@example.invalid"),
    )
    await repo.save_negotiation(
        project_id,
        "neg1",
        NegotiationRecord(
            item_id="mirror",
            supplier_id="sup1",
            state=NegotiationState.READY_FOR_HUMAN,
            rounds_used=3,
            latest_quote=ExtractedQuote(
                unit_price=Money(amount=719, currency="MYR"),
                total=Money(amount=719, currency="MYR"),
            ),
            escalation_reason="GOOD_QUOTE",
            created_at=T0,
            updated_at=T0,
        ),
    )
    return repo


async def test_chat_answers_from_stored_facts(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    owner = tokens.create("owner@example.invalid")
    headers = {
        "Authorization": f"Bearer {tokens.grant(owner, 'owner@example.invalid')}"
    }
    _ = await _owned_project(firestore, "kopitiam", owner)

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": "kopitiam", "question": "what needs me?"},
        )
    ).json()

    assert body["waiting_on_you"] == 1
    assert "Ah Seng Rentals" in body["text"]
    assert "719" in body["text"]
    assert [r["id"] for r in body["references"]] == ["neg1"]


async def test_chat_refuses_another_producers_project(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The api service reads through the admin SDK, which bypasses
    firestore.rules entirely — the same reason purchase orders needed their own
    database. So the ownership check has to be here as well, or a signed-in
    producer can summarise a rival production.
    """
    _ = await _owned_project(firestore, "someone-elses", "a-different-producer")

    response = await api.post(
        "/chat",
        headers=_auth(tokens, "outsider@example.invalid"),
        json={"project_id": "someone-elses", "question": "what needs me?"},
    )

    assert response.status_code == 404
    assert "Kopitiam" not in response.text, "not even the title leaks"


async def test_a_missing_project_and_a_stranger_look_identical(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Telling them apart would say whether a project id exists, which is not
    the asker's to learn."""
    _ = await _owned_project(firestore, "real-one", "a-different-producer")
    headers = _auth(tokens, "prober@example.invalid")

    existing = await api.post(
        "/chat", headers=headers, json={"project_id": "real-one", "question": "hi"}
    )
    absent = await api.post(
        "/chat", headers=headers, json={"project_id": "no-such", "question": "hi"}
    )

    assert existing.status_code == absent.status_code == 404


async def test_chat_needs_a_producer(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/chat", json={"project_id": "anything", "question": "hi"}
    )

    assert response.status_code == 401
