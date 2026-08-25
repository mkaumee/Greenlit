# httpx .json() and Starlette's app.state are both untyped by nature.
# pyright: reportAny=false, reportUnknownMemberType=false
"""The money path.

Every test here goes through a real Firebase ID token from the Auth emulator.
Nothing overrides the auth dependency — an approval test that injected its own
``Producer`` would prove the ordering logic while skipping the only question
that matters about this service, which is who is allowed to reach it.
"""

from datetime import UTC, datetime

import httpx
import pytest
from cinema_contracts import ClockMode, ExtractedQuote, Money, NegotiationState
from conftest import AUTH_HOST, TokenMinter
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.approvals import ApprovalServices, app
from orchestrator.auth import init_firebase
from orchestrator.clock import ClockState, FrozenRealTime, SimClock
from orchestrator.records import (
    ItemRecord,
    ItemStatus,
    NegotiationRecord,
    ProjectRecord,
    PurchaseOrderRecord,
    SupplierRecord,
)
from orchestrator.repository import FirestoreRepository, OrdersRepository
from orchestrator.settings import Settings

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
REAL0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)
PROJECT = "projA"

SETTINGS = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    gcp_project="demo-cinema",
    auth_emulator_host=AUTH_HOST,
)

QUOTE = ExtractedQuote(
    unit_price=Money(amount=880, currency="MYR"),
    qty=1,
    total=Money(amount=880, currency="MYR"),
)


async def _seed(
    repo: FirestoreRepository,
    *,
    state: NegotiationState = NegotiationState.READY_FOR_HUMAN,
    quote: ExtractedQuote | None = QUOTE,
    negotiation_id: str = "neg1",
    supplier_id: str = "sup1",
) -> None:
    """One project, one item, one negotiation parked in front of a person."""
    existing = await repo.get_project(PROJECT)
    if existing is None:
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
            ItemRecord(
                name="Mirror", category="prop", status=ItemStatus.READY_FOR_HUMAN
            ),
        )
    await repo.save_supplier(
        PROJECT,
        supplier_id,
        SupplierRecord(name="Glass Co", email="glass@example.invalid", verified=True),
    )
    await repo.save_negotiation(
        PROJECT,
        negotiation_id,
        NegotiationRecord(
            item_id="mirror",
            supplier_id=supplier_id,
            state=state,
            latest_quote=quote,
            escalation_reason="price is good, your call",
            next_action_due_at=None,
            created_at=T0,
            updated_at=T0,
        ),
    )


@pytest.fixture
async def api(
    firestore: AsyncClient, orders_firestore: AsyncClient, tokens: TokenMinter
) -> httpx.AsyncClient:
    """The approval app wired to both emulated databases.

    ``init_firebase`` runs here rather than through the lifespan because the
    ASGI transport is driven directly; it is idempotent, so repeated tests share
    one Firebase app.
    """
    _ = tokens  # ordering: the Auth emulator has to be clean before any request
    repo = FirestoreRepository(firestore)
    app.state.services = ApprovalServices(
        settings=SETTINGS,
        client=firestore,
        orders_client=orders_firestore,
        repo=repo,
        orders=OrdersRepository(orders_firestore),
        clock=SimClock(repo, FrozenRealTime(REAL0)),
    )
    init_firebase(SETTINGS)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Who is asking
# --------------------------------------------------------------------------- #


async def test_approving_without_a_token_is_refused(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    await _seed(FirestoreRepository(firestore))

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
    )

    assert response.status_code == 401


async def test_a_forged_token_is_refused(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Unsigned emulator tokens still have to be well-formed and well-addressed."""
    await _seed(FirestoreRepository(firestore))

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as("not.a.jwt"),
    )

    assert response.status_code == 401


async def test_the_agents_own_identity_cannot_approve(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The claim this whole phase exists to make, at the application layer.

    A signed-in identity with no ``producer`` claim gets 403 rather than 401 —
    it is authenticated and still refused, which is the line worth having in an
    audit log. Firestore refuses it a second time and independently; that half
    lives in ``web/tests/rules.test.ts``.
    """
    await _seed(FirestoreRepository(firestore))
    agent = tokens.mint("agent@example.invalid")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(agent),
    )

    assert response.status_code == 403
    assert "not a producer" in response.json()["detail"]


async def test_a_producer_can_approve(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    await _seed(FirestoreRepository(firestore))
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    assert response.status_code == 200, response.text
    assert response.json()["price"] == {"amount": 880, "currency": "MYR"}


# --------------------------------------------------------------------------- #
# What approving does
# --------------------------------------------------------------------------- #


async def test_approving_writes_the_purchase_order(
    api: httpx.AsyncClient,
    firestore: AsyncClient,
    orders_firestore: AsyncClient,
    tokens: TokenMinter,
) -> None:
    await _seed(FirestoreRepository(firestore))
    producer = tokens.mint("producer@example.invalid", role="producer")

    _ = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    order = await OrdersRepository(orders_firestore).get_purchase_order("mirror")
    assert order is not None
    assert order.price == Money(amount=880, currency="MYR")
    assert order.negotiation_id == "neg1"
    assert order.supplier_id == "sup1"
    assert order.approved_at == T0, "the order is stamped in simulated time"


async def test_the_order_records_who_approved_it(
    api: httpx.AsyncClient,
    firestore: AsyncClient,
    orders_firestore: AsyncClient,
    tokens: TokenMinter,
) -> None:
    """Not a boolean "approved" — the uid of the person who did it.

    An order nobody is named on is not an approval, it is a fact that money was
    spent.
    """
    await _seed(FirestoreRepository(firestore))
    producer = tokens.mint("producer@example.invalid", role="producer")

    body = (
        await api.post(
            "/items/mirror/approve",
            json={"project_id": PROJECT, "negotiation_id": "neg1"},
            headers=_as(producer),
        )
    ).json()

    order = await OrdersRepository(orders_firestore).get_purchase_order("mirror")
    assert order is not None
    assert order.approved_by == body["approved_by"]
    assert order.approved_by


async def test_approving_moves_the_negotiation_and_the_item(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    producer = tokens.mint("producer@example.invalid", role="producer")

    _ = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    negotiation = await repo.get_negotiation(PROJECT, "neg1")
    item = await repo.get_item(PROJECT, "mirror")
    assert negotiation is not None
    assert item is not None
    assert negotiation.state is NegotiationState.ORDERED
    assert item.status is ItemStatus.ORDERED
    assert item.chosen_quote == QUOTE


async def test_an_ordered_negotiation_is_never_ticked_again(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Terminal means out of the queue, not merely filtered out of it."""
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    producer = tokens.mint("producer@example.invalid", role="producer")

    _ = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    later = datetime(2027, 1, 1, tzinfo=UTC)
    assert await repo.due_negotiations(later) == []
    assert await repo.due_items(later) == []


# --------------------------------------------------------------------------- #
# What approving refuses
# --------------------------------------------------------------------------- #


async def test_approving_a_negotiation_still_in_progress_is_refused(
    api: httpx.AsyncClient,
    firestore: AsyncClient,
    orders_firestore: AsyncClient,
    tokens: TokenMinter,
) -> None:
    """Only what the agent has handed back. Anything else buys at a number
    nobody has agreed to yet."""
    await _seed(FirestoreRepository(firestore), state=NegotiationState.NEGOTIATING)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    assert response.status_code == 409
    assert "READY_FOR_HUMAN" in response.json()["detail"]
    orders = OrdersRepository(orders_firestore)
    assert await orders.get_purchase_order("mirror") is None


async def test_approving_with_no_quote_on_the_table_is_refused(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """READY_FOR_HUMAN is also where unparseable replies land, and those have
    no price to order at."""
    await _seed(FirestoreRepository(firestore), quote=None)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    assert response.status_code == 409
    assert "no quote" in response.json()["detail"]


async def test_a_negotiation_for_a_different_item_is_refused(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The URL says what is being bought and the body says which conversation
    justified it. If they disagree, one of them is a mistake."""
    await _seed(FirestoreRepository(firestore))
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/smoke-machine/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    assert response.status_code == 400


async def test_approving_something_that_does_not_exist_is_a_404(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    await _seed(FirestoreRepository(firestore))
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "nope"},
        headers=_as(producer),
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# The guardrail, and the retry that must not be mistaken for it
# --------------------------------------------------------------------------- #


async def test_the_same_item_cannot_be_bought_from_a_second_supplier(
    api: httpx.AsyncClient,
    firestore: AsyncClient,
    orders_firestore: AsyncClient,
    tokens: TokenMinter,
) -> None:
    """The guardrail. Two negotiations for one mirror, both READY_FOR_HUMAN,
    both approved by a legitimate producer — and the second is refused by
    Firestore's ``create()`` rather than by a check written here.
    """
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    await _seed(repo, negotiation_id="neg2", supplier_id="sup2")
    producer = tokens.mint("producer@example.invalid", role="producer")

    first = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )
    second = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg2"},
        headers=_as(producer),
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already been ordered" in second.json()["detail"]

    order = await OrdersRepository(orders_firestore).get_purchase_order("mirror")
    assert order is not None
    assert order.negotiation_id == "neg1", "the first order stands, untouched"


async def test_the_losing_negotiation_is_left_alone(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """A refused approval must not half-move the negotiation it refused."""
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    await _seed(repo, negotiation_id="neg2", supplier_id="sup2")
    producer = tokens.mint("producer@example.invalid", role="producer")

    _ = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )
    _ = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg2"},
        headers=_as(producer),
    )

    loser = await repo.get_negotiation(PROJECT, "neg2")
    assert loser is not None
    assert loser.state is NegotiationState.READY_FOR_HUMAN


async def test_a_half_finished_approval_is_completed_not_refused(
    api: httpx.AsyncClient,
    firestore: AsyncClient,
    orders_firestore: AsyncClient,
    tokens: TokenMinter,
) -> None:
    """The crash the write order is designed around.

    The order was written and the process died before the negotiation moved.
    That is exactly what a retry should finish — and it arrives as the same
    ``DuplicateOrderError`` as the guardrail, so telling the two apart is load
    bearing. Same negotiation means retry; different means refuse.
    """
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    await OrdersRepository(orders_firestore).create_purchase_order(
        PurchaseOrderRecord(
            item_id="mirror",
            project_id=PROJECT,
            supplier_id="sup1",
            negotiation_id="neg1",
            price=Money(amount=880, currency="MYR"),
            approved_by="whoever-crashed",
            approved_at=T0,
        )
    )
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    assert response.status_code == 200, response.text
    assert response.json()["already_existed"] is True

    negotiation = await repo.get_negotiation(PROJECT, "neg1")
    assert negotiation is not None
    assert negotiation.state is NegotiationState.ORDERED


async def test_a_retry_does_not_rewrite_the_order(
    api: httpx.AsyncClient,
    firestore: AsyncClient,
    orders_firestore: AsyncClient,
    tokens: TokenMinter,
) -> None:
    """Finishing the job is not the same as redoing it. The original approver
    and the original price survive the retry."""
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    orders = OrdersRepository(orders_firestore)
    await orders.create_purchase_order(
        PurchaseOrderRecord(
            item_id="mirror",
            project_id=PROJECT,
            supplier_id="sup1",
            negotiation_id="neg1",
            price=Money(amount=700, currency="MYR"),
            approved_by="the-first-producer",
            approved_at=T0,
        )
    )
    producer = tokens.mint("producer@example.invalid", role="producer")

    body = (
        await api.post(
            "/items/mirror/approve",
            json={"project_id": PROJECT, "negotiation_id": "neg1"},
            headers=_as(producer),
        )
    ).json()

    order = await orders.get_purchase_order("mirror")
    assert order is not None
    assert order.approved_by == "the-first-producer"
    assert order.price == Money(amount=700, currency="MYR")
    assert body["price"] == {"amount": 700, "currency": "MYR"}, (
        "the response reports what was actually ordered, not what was asked for"
    )


async def test_approving_twice_is_safe(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """A double-clicked button must not be a second-guessing moment."""
    await _seed(FirestoreRepository(firestore))
    producer = tokens.mint("producer@example.invalid", role="producer")
    payload = {"project_id": PROJECT, "negotiation_id": "neg1"}

    first = await api.post("/items/mirror/approve", json=payload, headers=_as(producer))
    second = await api.post(
        "/items/mirror/approve", json=payload, headers=_as(producer)
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["already_existed"] is False
    assert second.json()["already_existed"] is True
    assert second.json()["approved_by"] == first.json()["approved_by"]


async def test_an_ordered_negotiation_with_no_order_behind_it_is_refused(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The careful half of treating a double-click as success.

    ``ORDERED`` on its own is not proof that anything was bought — it is exactly
    what the write order in this route exists to prevent, and if it ever shows
    up it means something wrote it by hand. Reporting 200 for it would confirm a
    purchase that does not exist, so it gets the ordinary refusal instead.
    """
    await _seed(FirestoreRepository(firestore), state=NegotiationState.ORDERED)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/items/mirror/approve",
        json={"project_id": PROJECT, "negotiation_id": "neg1"},
        headers=_as(producer),
    )

    assert response.status_code == 409
    assert "ORDERED" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# Handing it back
# --------------------------------------------------------------------------- #


async def test_a_floor_sends_the_negotiation_back_to_the_agent(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The interesting half of the handoff: not accept, not abandon, but the
    producer changing the agent's instructions mid-conversation."""
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/negotiations/neg1/floor",
        json={
            "project_id": PROJECT,
            "floor_price": {"amount": 600, "currency": "MYR"},
        },
        headers=_as(producer),
    )

    assert response.status_code == 200, response.text
    record = await repo.get_negotiation(PROJECT, "neg1")
    assert record is not None
    assert record.state is NegotiationState.NEGOTIATING
    assert record.floor_price == Money(amount=600, currency="MYR")
    assert record.escalation_reason == "", "the old escalation no longer applies"


async def test_a_returned_negotiation_is_picked_up_by_the_next_tick(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Due immediately, in simulated time. A handoff the loop does not notice
    is a producer waiting on nothing."""
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    producer = tokens.mint("producer@example.invalid", role="producer")

    _ = await api.post(
        "/negotiations/neg1/floor",
        json={
            "project_id": PROJECT,
            "floor_price": {"amount": 600, "currency": "MYR"},
        },
        headers=_as(producer),
    )

    due = await repo.due_negotiations(T0)
    assert [d.negotiation_id for d in due] == ["neg1"]


async def test_a_floor_on_an_already_dead_negotiation_is_a_409(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Two screens open on one negotiation is ordinary; a 500 is not."""
    await _seed(FirestoreRepository(firestore), state=NegotiationState.DEAD)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/negotiations/neg1/floor",
        json={
            "project_id": PROJECT,
            "floor_price": {"amount": 600, "currency": "MYR"},
        },
        headers=_as(producer),
    )

    assert response.status_code == 409
    assert "HUMAN_RETURNED_WITH_FLOOR is not legal" in response.json()["detail"]


async def test_a_floor_needs_a_producer_too(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    await _seed(FirestoreRepository(firestore))
    agent = tokens.mint("agent@example.invalid")

    response = await api.post(
        "/negotiations/neg1/floor",
        json={
            "project_id": PROJECT,
            "floor_price": {"amount": 600, "currency": "MYR"},
        },
        headers=_as(agent),
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# Stopping
# --------------------------------------------------------------------------- #


async def test_a_producer_can_cancel(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    repo = FirestoreRepository(firestore)
    await _seed(repo)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/negotiations/neg1/cancel",
        json={"project_id": PROJECT},
        headers=_as(producer),
    )

    assert response.status_code == 200, response.text
    record = await repo.get_negotiation(PROJECT, "neg1")
    assert record is not None
    assert record.state is NegotiationState.DEAD
    assert await repo.due_negotiations(datetime(2027, 1, 1, tzinfo=UTC)) == []


async def test_cancelling_works_mid_conversation(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """A producer does not have to wait to be asked before pulling out."""
    repo = FirestoreRepository(firestore)
    await _seed(repo, state=NegotiationState.AWAITING_REPLY)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/negotiations/neg1/cancel",
        json={"project_id": PROJECT},
        headers=_as(producer),
    )

    assert response.status_code == 200
    record = await repo.get_negotiation(PROJECT, "neg1")
    assert record is not None
    assert record.state is NegotiationState.DEAD


async def test_cancelling_an_ordered_negotiation_is_refused(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """ORDERED is terminal. Money has moved; unwinding it is not an API call."""
    await _seed(FirestoreRepository(firestore), state=NegotiationState.ORDERED)
    producer = tokens.mint("producer@example.invalid", role="producer")

    response = await api.post(
        "/negotiations/neg1/cancel",
        json={"project_id": PROJECT},
        headers=_as(producer),
    )

    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def test_health_says_whether_tokens_are_really_being_verified(
    api: httpx.AsyncClient,
) -> None:
    """Trusting unsigned tokens is fine locally and alarming anywhere else, so
    the service says which it is doing rather than leaving it to be inferred
    from which terminal you are looking at."""
    body = (await api.get("/health")).json()

    assert body["status"] == "ok"
    assert body["orders_database"] == "orders"
    assert body["auth_emulator"] == AUTH_HOST
