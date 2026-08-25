# The Firestore client is only partially typed; a couple of raw client calls
# here are deliberate, to check behaviour the repository deliberately hides.
# pyright: reportUnknownMemberType=false
"""Firestore repository tests, against the emulator.

The centrepiece is the purchase-order group. Those tests are the evidence
behind the claim that the agent cannot spend money twice, and they are written
to fail loudly if anyone ever swaps ``create()`` for ``set()``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cinema_contracts import (
    ClockMode,
    ExtractedQuote,
    Money,
    NegotiationState,
)
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.clock import ClockState, SimClock
from orchestrator.records import (
    ItemRecord,
    ItemStatus,
    MessageRecord,
    NegotiationRecord,
    ProjectRecord,
    PurchaseOrderRecord,
    SupplierRecord,
)
from orchestrator.repository import (
    DuplicateOrderError,
    FirestoreRepository,
    OrdersRepository,
)

PID = "proj1"
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
REAL0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)


def _project() -> ProjectRecord:
    return ProjectRecord(
        title="Nasi Lemak Nights",
        clock=ClockState(sim_now=T0, real_anchor=REAL0, speed=1.0, mode=ClockMode.LIVE),
        budget_baseline=Money(amount=50_000),
        created_at=T0,
    )


def _negotiation(
    *,
    item_id: str = "item1",
    supplier_id: str = "sup1",
    state: NegotiationState = NegotiationState.AWAITING_REPLY,
    due: datetime | None = None,
    thread: str = "",
) -> NegotiationRecord:
    return NegotiationRecord(
        item_id=item_id,
        supplier_id=supplier_id,
        state=state,
        next_action_due_at=due,
        gmail_thread_id=thread,
        created_at=T0,
        updated_at=T0,
    )


def _order(
    *, item_id: str = "item1", supplier_id: str = "sup1", amount: int = 880
) -> PurchaseOrderRecord:
    return PurchaseOrderRecord(
        item_id=item_id,
        project_id=PID,
        supplier_id=supplier_id,
        negotiation_id="neg1",
        price=Money(amount=amount),
        approved_by="producer-uid",
        approved_at=T0,
    )


# --------------------------------------------------------------------------- #
# The guardrail
# --------------------------------------------------------------------------- #


def test_the_tick_loops_repository_has_no_way_to_write_an_order() -> None:
    """Defence in depth, and the half that works without any cloud project.

    In production the agent's service account has no IAM binding on the orders
    database, which is the real control. This is the second lock: the object
    the tick loop is handed has no method that could write an order even if
    that binding were mistakenly granted.

    Firestore rules are *not* a third lock here. They do not apply to server
    SDKs at all, so they constrain a producer's browser and nothing else.
    """
    assert not hasattr(FirestoreRepository, "create_purchase_order")
    assert not hasattr(FirestoreRepository, "total_ordered")
    assert hasattr(OrdersRepository, "create_purchase_order")


async def test_the_two_databases_do_not_see_each_other(
    firestore: AsyncClient, orders_firestore: AsyncClient
) -> None:
    """The isolation the whole split rests on.

    If a future Firestore change ever made these the same store, every other
    guardrail test would still pass while the guarantee quietly evaporated.
    """
    orders = OrdersRepository(orders_firestore)
    await orders.create_purchase_order(_order(item_id="item1"))

    leaked = (
        await firestore.collection("purchase_orders").document("item1").get()
    ).exists

    assert not leaked, "an order written to `orders` is visible from (default)"


async def test_a_purchase_order_can_be_created_once(
    orders_firestore: AsyncClient,
) -> None:
    repo = OrdersRepository(orders_firestore)
    await repo.create_purchase_order(_order())

    stored = await repo.get_purchase_order("item1")
    assert stored is not None
    assert stored.price == Money(amount=880)
    assert stored.approved_by == "producer-uid"


async def test_a_second_order_for_the_same_item_is_refused(
    orders_firestore: AsyncClient,
) -> None:
    repo = OrdersRepository(orders_firestore)
    await repo.create_purchase_order(_order())

    with pytest.raises(DuplicateOrderError) as caught:
        await repo.create_purchase_order(_order())

    assert caught.value.item_id == "item1"


async def test_ordering_one_item_from_two_suppliers_is_the_same_violation(
    orders_firestore: AsyncClient,
) -> None:
    """The reason the key is the item and not the order.

    A per-order document ID would have let this through: two different orders,
    two different IDs, both created happily, one item bought twice. Keying on
    the item makes "same item, different supplier" collide with "same item,
    same supplier" and be refused identically.
    """
    repo = OrdersRepository(orders_firestore)
    await repo.create_purchase_order(_order(supplier_id="sup1", amount=880))

    with pytest.raises(DuplicateOrderError):
        await repo.create_purchase_order(_order(supplier_id="sup2", amount=750))


async def test_a_refused_order_does_not_disturb_the_one_already_there(
    orders_firestore: AsyncClient,
) -> None:
    """``create()`` rather than ``set()``. If this ever fails, someone swapped it."""
    repo = OrdersRepository(orders_firestore)
    await repo.create_purchase_order(_order(supplier_id="sup1", amount=880))

    with pytest.raises(DuplicateOrderError):
        await repo.create_purchase_order(_order(supplier_id="sup2", amount=1))

    stored = await repo.get_purchase_order("item1")
    assert stored is not None
    assert stored.supplier_id == "sup1"
    assert stored.price == Money(amount=880)


async def test_different_items_are_independent(orders_firestore: AsyncClient) -> None:
    repo = OrdersRepository(orders_firestore)
    await repo.create_purchase_order(_order(item_id="item1", amount=880))
    await repo.create_purchase_order(_order(item_id="item2", amount=120))

    assert await repo.total_ordered() == Money(amount=1000)


async def test_nothing_ordered_yet_is_none_rather_than_a_guessed_zero(
    orders_firestore: AsyncClient,
) -> None:
    assert await OrdersRepository(orders_firestore).total_ordered() is None


# --------------------------------------------------------------------------- #
# The tick loop's query
# --------------------------------------------------------------------------- #


async def test_due_negotiations_returns_only_what_is_due_oldest_first(
    firestore: AsyncClient,
) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())

    await repo.save_negotiation(PID, "later", _negotiation(due=T0 + timedelta(days=2)))
    await repo.save_negotiation(
        PID, "overdue", _negotiation(due=T0 - timedelta(days=1))
    )
    await repo.save_negotiation(PID, "just-now", _negotiation(due=T0))

    due = await repo.due_negotiations(T0)

    assert [d.negotiation_id for d in due] == ["overdue", "just-now"]


async def test_a_finished_negotiation_drops_out_of_the_query(
    firestore: AsyncClient,
) -> None:
    """Terminal states have the due field removed, not nulled.

    A null would still sit in the index and have to be filtered on every tick.
    """
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())

    await repo.save_negotiation(
        PID,
        "done",
        _negotiation(state=NegotiationState.ORDERED, due=T0 - timedelta(days=1)),
    )
    await repo.save_negotiation(
        PID,
        "dead",
        _negotiation(state=NegotiationState.DEAD, due=T0 - timedelta(days=1)),
    )
    await repo.save_negotiation(PID, "live", _negotiation(due=T0 - timedelta(days=1)))

    due = await repo.due_negotiations(T0)

    assert [d.negotiation_id for d in due] == ["live"]


async def test_a_tick_only_takes_a_bounded_bite(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    for index in range(8):
        await repo.save_negotiation(
            PID, f"n{index}", _negotiation(due=T0 - timedelta(hours=index + 1))
        )

    assert len(await repo.due_negotiations(T0, limit=3)) == 3


async def test_due_query_spans_projects(firestore: AsyncClient) -> None:
    """One collection-group query covers every project."""
    repo = FirestoreRepository(firestore)
    await repo.create_project("projA", _project())
    await repo.create_project("projB", _project())
    await repo.save_negotiation("projA", "a1", _negotiation(due=T0))
    await repo.save_negotiation("projB", "b1", _negotiation(due=T0))

    due = await repo.due_negotiations(T0)

    assert {d.project_id for d in due} == {"projA", "projB"}


# --------------------------------------------------------------------------- #
# Claiming
# --------------------------------------------------------------------------- #


async def test_claiming_takes_a_row_out_of_the_queue(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "n1", _negotiation(due=T0 - timedelta(hours=1)))

    (due,) = await repo.due_negotiations(T0)
    claimed = await repo.claim_negotiation(due, T0 + timedelta(minutes=15))

    assert claimed
    assert await repo.due_negotiations(T0) == []
    assert len(await repo.due_negotiations(T0 + timedelta(hours=1))) == 1


async def test_only_one_of_two_ticks_can_claim_the_same_row(
    firestore: AsyncClient,
) -> None:
    """The whole point. Both hold the same read; Firestore admits one.

    This is the deterministic version — both callers genuinely share one
    ``update_time``, with no reliance on how the event loop happens to
    interleave. ``test_tick.py`` has the realistic counterpart.
    """
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "n1", _negotiation(due=T0 - timedelta(hours=1)))

    (first,) = await repo.due_negotiations(T0)
    (second,) = await repo.due_negotiations(T0)
    assert first.update_time == second.update_time, "both ticks read the same version"

    won = await repo.claim_negotiation(first, T0 + timedelta(minutes=15))
    lost = await repo.claim_negotiation(second, T0 + timedelta(minutes=15))

    assert won
    assert not lost, "the second claim must be refused by Firestore, not by us"


async def test_any_intervening_write_invalidates_a_claim(
    firestore: AsyncClient,
) -> None:
    """Not just a competing claim — any write at all.

    A producer setting a floor from the approval service also bumps the version,
    and a tick still holding the older read must not overwrite that decision
    with a stale record.
    """
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "n1", _negotiation(due=T0 - timedelta(hours=1)))

    (due,) = await repo.due_negotiations(T0)
    record = await repo.get_negotiation(PID, "n1")
    assert record is not None
    record.floor_price = Money(amount=600)
    await repo.save_negotiation(PID, "n1", record)

    assert not await repo.claim_negotiation(due, T0 + timedelta(minutes=15))


async def test_items_are_claimed_the_same_way(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_item(
        PID,
        "mirror",
        ItemRecord(
            name="Mirror",
            category="prop",
            status=ItemStatus.RESEARCHING,
            next_action_due_at=T0 - timedelta(hours=1),
        ),
    )

    (first,) = await repo.due_items(T0)
    (second,) = await repo.due_items(T0)

    assert await repo.claim_item(first, T0 + timedelta(minutes=15))
    assert not await repo.claim_item(second, T0 + timedelta(minutes=15))
    assert await repo.due_items(T0) == []


# --------------------------------------------------------------------------- #
# Threading and messages
# --------------------------------------------------------------------------- #


async def test_inbound_mail_is_routed_by_thread_id(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "neg1", _negotiation(thread="thread-abc"))
    await repo.save_negotiation(
        PID, "neg2", _negotiation(item_id="item2", thread="thread-xyz")
    )

    found = await repo.find_by_thread("thread-xyz")

    assert found is not None
    assert found.negotiation_id == "neg2"
    assert found.project_id == PID


async def test_an_unknown_thread_finds_nothing_rather_than_guessing(
    firestore: AsyncClient,
) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "neg1", _negotiation(thread="thread-abc"))

    assert await repo.find_by_thread("thread-nope") is None
    assert await repo.find_by_thread("") is None


async def test_replaying_a_message_after_a_killed_tick_does_not_duplicate_it(
    firestore: AsyncClient,
) -> None:
    """Keyed by Gmail message ID, so redelivery is a no-op."""
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "neg1", _negotiation())

    message = MessageRecord(direction="INBOUND", body="RM1,250", sim_sent_at=T0)
    _ = await repo.append_message(PID, "neg1", "gmail-msg-1", message)
    _ = await repo.append_message(PID, "neg1", "gmail-msg-1", message)

    assert len(await repo.list_messages(PID, "neg1")) == 1


async def test_messages_come_back_in_simulated_order(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())
    await repo.save_negotiation(PID, "neg1", _negotiation())

    _ = await repo.append_message(
        PID,
        "neg1",
        "m2",
        MessageRecord(
            direction="INBOUND", body="second", sim_sent_at=T0 + timedelta(days=1)
        ),
    )
    _ = await repo.append_message(
        PID,
        "neg1",
        "m1",
        MessageRecord(direction="OUTBOUND", body="first", sim_sent_at=T0),
    )

    assert [m.body for m in await repo.list_messages(PID, "neg1")] == [
        "first",
        "second",
    ]


# --------------------------------------------------------------------------- #
# Round-tripping
# --------------------------------------------------------------------------- #


async def test_the_clock_persists_through_the_repository(
    firestore: AsyncClient,
) -> None:
    """FirestoreRepository satisfies ClockStore, so SimClock needs no Firestore import."""
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())

    clock = SimClock(repo)
    assert await clock.now(PID) >= T0

    _ = await clock.set_mode(PID, ClockMode.DEMO)
    reloaded = await repo.get_project(PID)
    assert reloaded is not None
    assert reloaded.clock.mode is ClockMode.DEMO


async def test_money_survives_a_round_trip_with_its_currency(
    firestore: AsyncClient,
) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())

    price = Money(amount=1250, currency="MYR")
    await repo.save_negotiation(
        PID,
        "neg1",
        NegotiationRecord(
            item_id="item1",
            supplier_id="sup1",
            floor_price=price,
            latest_quote=ExtractedQuote(
                unit_price=price, qty=2, total=Money(amount=2500)
            ),
            created_at=T0,
            updated_at=T0,
        ),
    )

    stored = await repo.get_negotiation(PID, "neg1")
    assert stored is not None
    assert stored.floor_price == price
    assert stored.latest_quote is not None
    assert stored.latest_quote.total == Money(amount=2500)


async def test_items_and_suppliers_round_trip(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_project(PID, _project())

    await repo.save_item(
        PID,
        "item1",
        ItemRecord(
            name="Arri SkyPanel S60", category="lighting", scenes=["4", "7"], qty=2
        ),
    )
    await repo.save_supplier(
        PID, "sup1", SupplierRecord(name="Ah Seng Rentals", email="s@example.invalid")
    )

    items = await repo.list_items(PID)
    supplier = await repo.get_supplier(PID, "sup1")

    assert items["item1"].scenes == ["4", "7"]
    assert supplier is not None
    assert supplier.email == "s@example.invalid"


async def test_a_missing_project_has_no_clock_to_read(firestore: AsyncClient) -> None:
    with pytest.raises(KeyError):
        _ = await FirestoreRepository(firestore).read("no-such-project")
