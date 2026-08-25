# The emulator's JSON and firebase-admin's records are Any by nature.
# pyright: reportAny=false, reportMissingTypeStubs=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""The admin script that decides who can spend money.

The test that matters is the last one: granting the claim has to actually
change what the approval endpoint does. A script that writes a claim nothing
reads would pass every unit test and leave the producer looking at a 403.
"""

import httpx
import pytest
from cinema_contracts import ClockMode, NegotiationState
from conftest import TokenMinter
from firebase_admin import auth as firebase_auth
from google.cloud.firestore_v1 import AsyncClient
from grant_producer import main
from orchestrator.approvals import ApprovalServices, app
from orchestrator.auth import init_firebase
from orchestrator.clock import ClockState, FrozenRealTime, SimClock
from orchestrator.records import (
    ItemRecord,
    ItemStatus,
    NegotiationRecord,
    ProjectRecord,
    SupplierRecord,
)
from orchestrator.repository import FirestoreRepository, OrdersRepository
from test_approvals import PROJECT, QUOTE, REAL0, SETTINGS, T0


@pytest.fixture
def granting(tokens: TokenMinter) -> TokenMinter:
    """A clean Auth emulator with firebase-admin pointed at it."""
    init_firebase(SETTINGS)
    return tokens


def _role_of(email: str) -> str:
    user = firebase_auth.get_user_by_email(email)
    return (user.custom_claims or {}).get("role", "")


def test_granting_sets_the_claim(granting: TokenMinter) -> None:
    _ = granting.create("producer@example.invalid")

    assert main(["producer@example.invalid"]) == 0
    assert _role_of("producer@example.invalid") == "producer"


def test_revoking_takes_it_away(granting: TokenMinter) -> None:
    _ = granting.create("producer@example.invalid")
    _ = main(["producer@example.invalid"])

    assert main(["--revoke", "producer@example.invalid"]) == 0
    assert _role_of("producer@example.invalid") == ""


def test_granting_against_the_emulator_creates_a_missing_user(
    granting: TokenMinter,
) -> None:
    """Local setup convenience, and only local: against a real project an
    unknown address is a typo, not an invitation to create an account."""
    _ = granting

    assert main(["nobody-yet@example.invalid"]) == 0
    assert _role_of("nobody-yet@example.invalid") == "producer"


def test_listing_users_needs_no_email(granting: TokenMinter) -> None:
    _ = granting.create("someone@example.invalid")

    assert main(["--list"]) == 0


def test_calling_it_with_nothing_explains_itself(granting: TokenMinter) -> None:
    _ = granting

    assert main([]) == 2


async def test_a_granted_producer_can_actually_approve(
    granting: TokenMinter, firestore: AsyncClient, orders_firestore: AsyncClient
) -> None:
    """End to end, which is the only version of this worth having.

    Sign up, get refused with a 403, get granted, sign in again, get through.
    Each step is one a real producer takes, including the sign-out — the claim
    is baked into the token at issue time, so the old one keeps being refused.
    """
    repo = FirestoreRepository(firestore)
    await repo.create_project(
        PROJECT,
        ProjectRecord(
            title="Nightfall",
            clock=ClockState(
                sim_now=T0, real_anchor=REAL0, speed=0.0, mode=ClockMode.FROZEN
            ),
            created_at=T0,
        ),
    )
    await repo.save_item(
        PROJECT,
        "mirror",
        ItemRecord(name="Mirror", category="prop", status=ItemStatus.READY_FOR_HUMAN),
    )
    await repo.save_supplier(
        PROJECT,
        "sup1",
        SupplierRecord(name="Glass Co", email="glass@example.invalid"),
    )
    await repo.save_negotiation(
        PROJECT,
        "neg1",
        NegotiationRecord(
            item_id="mirror",
            supplier_id="sup1",
            state=NegotiationState.READY_FOR_HUMAN,
            latest_quote=QUOTE,
            created_at=T0,
            updated_at=T0,
        ),
    )
    app.state.services = ApprovalServices(
        settings=SETTINGS,
        client=firestore,
        orders_client=orders_firestore,
        repo=repo,
        orders=OrdersRepository(orders_firestore),
        clock=SimClock(repo, FrozenRealTime(REAL0)),
    )
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    payload = {"project_id": PROJECT, "negotiation_id": "neg1"}

    _ = granting.create("newcomer@example.invalid")
    before = granting.sign_in("newcomer@example.invalid")
    refused = await api.post(
        "/items/mirror/approve",
        json=payload,
        headers={"Authorization": f"Bearer {before}"},
    )

    assert main(["newcomer@example.invalid"]) == 0

    stale = await api.post(
        "/items/mirror/approve",
        json=payload,
        headers={"Authorization": f"Bearer {before}"},
    )
    after = granting.sign_in("newcomer@example.invalid")
    allowed = await api.post(
        "/items/mirror/approve",
        json=payload,
        headers={"Authorization": f"Bearer {after}"},
    )

    assert refused.status_code == 403, "no claim yet"
    assert stale.status_code == 403, "the old token never carried the claim"
    assert allowed.status_code == 200, allowed.text
