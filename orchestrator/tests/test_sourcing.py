# httpx .json() and Starlette's app.state are untyped by nature.
# pyright: reportAny=false, reportExplicitAny=false
"""The front half: a screenplay becomes open negotiations.

This is the part that was missing until now — ``run_e2e.py`` hand-seeded items,
suppliers and negotiations, which made the pipeline look complete when nothing
actually connected the script to them.

The test worth reading is
``test_a_screenplay_becomes_negotiations_without_anything_hand_seeded``.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, override

import httpx
import pytest
from cinema_contracts import ItemBrief, ItemResearch, Money, NegotiationState
from cinema_contracts.testing import ScriptedBrain
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.app import Services, app
from orchestrator.clock import FrozenRealTime, SimClock
from orchestrator.mail import InMemoryMailbox
from orchestrator.records import ItemStatus
from orchestrator.repository import DueItem, FirestoreRepository
from orchestrator.settings import Settings
from orchestrator.sourcing import item_id_for, negotiation_id_for, supplier_id_for
from orchestrator.tick import TickLoop

PID = "nasi-lemak-nights"
REAL0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)

SETTINGS = Settings(_env_file=None, gcp_project="demo-cinema")  # pyright: ignore[reportCallIssue]

SCRIPT = """INT. BAR - NIGHT

The MAN grabbed the cup and threw it towards the mirror.

EXT. STREET - DAY

She lights a cigarette and checks her watch.
"""


@pytest.fixture
async def api(firestore: AsyncClient) -> httpx.AsyncClient:
    repo = FirestoreRepository(firestore)
    clock = SimClock(repo, FrozenRealTime(REAL0))
    brain = ScriptedBrain()
    mail = InMemoryMailbox()
    app.state.services = Services(
        settings=SETTINGS,
        client=firestore,
        repo=repo,
        clock=clock,
        brain=brain,
        mail=mail,
        loop=TickLoop(repo, clock, brain, mail),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _new_project(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/projects", json={"project_id": PID, "title": "Nasi Lemak Nights"}
    )
    assert response.status_code == 201, response.text


async def _upload(api: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await api.post(f"/projects/{PID}/script", json={"text_content": SCRIPT})
    assert response.status_code == 200, response.text
    return response.json()["props"]


async def _confirm_everything(api: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Producer signs off the whole list, which is what puts items in the queue."""
    props = await _upload(api)
    response = await api.post(
        f"/projects/{PID}/items/confirm",
        json={"items": [{"item_id": p["item_id"], "qty": 1} for p in props]},
    )
    assert response.status_code == 200, response.text
    return props


# --------------------------------------------------------------------------- #
# Reading the script
# --------------------------------------------------------------------------- #


async def test_uploading_a_screenplay_finds_the_props(api: httpx.AsyncClient) -> None:
    await _new_project(api)

    props = await _upload(api)

    assert {p["name"] for p in props} == {"cup", "mirror", "cigarette", "watch"}


async def test_every_prop_comes_back_with_the_line_that_justifies_it(
    api: httpx.AsyncClient,
) -> None:
    """The producer audits the list instead of trusting it."""
    await _new_project(api)

    for prop in await _upload(api):
        assert prop["lines"], f"{prop['name']} arrived with no script line"
        assert all(line.strip() for line in prop["lines"])


async def test_a_prop_that_gets_destroyed_is_flagged_for_the_producer(
    api: httpx.AsyncClient,
) -> None:
    await _new_project(api)

    by_name = {p["name"]: p for p in await _upload(api)}

    assert by_name["mirror"]["consumable"] is True
    assert by_name["watch"]["consumable"] is False


async def test_uploading_a_revised_draft_updates_rather_than_duplicates(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Item ids are derived from the name, so a second draft is not a second mirror."""
    await _new_project(api)
    first = await _upload(api)
    second = await _upload(api)

    items = await FirestoreRepository(firestore).list_items(PID)

    assert [p["item_id"] for p in first] == [p["item_id"] for p in second]
    assert set(items) == {item_id_for(p["name"]) for p in first}
    assert len(items) == 4, "a revised draft should update items, not duplicate them"


# --------------------------------------------------------------------------- #
# Nothing moves until a human says so
# --------------------------------------------------------------------------- #


async def test_an_uploaded_script_starts_nothing(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """The gap where a hallucinated prop gets caught.

    Upload, then tick repeatedly. No research, no suppliers, no negotiations,
    no email — because a person has not confirmed the list yet.
    """
    await _new_project(api)
    _ = await _upload(api)

    for _ in range(3):
        body = (await api.post("/tick")).json()["projects"][0]
        assert body["items_examined"] == 0
        assert body["negotiations_opened"] == 0
        assert body["messages_sent"] == 0

    repo = FirestoreRepository(firestore)
    assert await repo.list_suppliers(PID) == {}
    assert await repo.list_negotiations(PID) == {}
    assert all(
        i.status is ItemStatus.DRAFT for i in (await repo.list_items(PID)).values()
    )


async def test_items_left_out_at_confirmation_are_abandoned_not_deleted(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """The breakdown still shows what the script asked for and what was dropped."""
    await _new_project(api)
    _ = await _upload(api)

    response = await api.post(
        f"/projects/{PID}/items/confirm",
        json={
            "items": [
                {"item_id": item_id_for("mirror"), "qty": 6, "include": True},
                {"item_id": item_id_for("watch"), "include": False},
            ]
        },
    )
    body = response.json()

    assert body["confirmed"] == [item_id_for("mirror")]
    assert body["abandoned"] == [item_id_for("watch")]

    items = await FirestoreRepository(firestore).list_items(PID)
    assert items[item_id_for("watch")].status is ItemStatus.ABANDONED
    assert items[item_id_for("mirror")].qty == 6


async def test_the_producer_sets_the_quantity_for_a_consumable(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Six breakaway mirrors, because only a human knows the shooting schedule."""
    await _new_project(api)
    _ = await _upload(api)

    _ = await api.post(
        f"/projects/{PID}/items/confirm",
        json={"items": [{"item_id": item_id_for("mirror"), "qty": 6}]},
    )

    item = await FirestoreRepository(firestore).get_item(PID, item_id_for("mirror"))
    assert item is not None
    assert item.qty == 6
    assert item.consumable


# --------------------------------------------------------------------------- #
# The whole front half
# --------------------------------------------------------------------------- #


async def test_a_screenplay_becomes_negotiations_without_anything_hand_seeded(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Script in, open negotiations out. The hole this phase existed to close."""
    await _new_project(api)
    props = await _upload(api)

    _ = await api.post(
        f"/projects/{PID}/items/confirm",
        json={
            "items": [
                {"item_id": p["item_id"], "qty": 1, "floor_price": {"amount": 900}}
                for p in props
            ]
        },
    )

    # Research, then open negotiations, then send. Each is its own tick so that
    # a process dying between them loses only that step.
    for _ in range(3):
        _ = await api.post("/tick")

    repo = FirestoreRepository(firestore)
    items = await repo.list_items(PID)
    suppliers = await repo.list_suppliers(PID)
    negotiations = await repo.list_negotiations(PID)

    assert suppliers, "research produced no suppliers"
    assert negotiations, "no negotiations were opened"
    assert all(i.status is ItemStatus.NEGOTIATING for i in items.values())
    assert all(i.reference_band is not None for i in items.values())

    # And the loop took over: opening emails went out.
    states = {n.state for n in negotiations.values()}
    assert states == {NegotiationState.AWAITING_REPLY}


async def test_the_floor_set_at_confirmation_reaches_every_negotiation(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """One ceiling per item, inherited by each seller approached for it."""
    await _new_project(api)
    _ = await _upload(api)
    _ = await api.post(
        f"/projects/{PID}/items/confirm",
        json={
            "items": [
                {"item_id": item_id_for("mirror"), "floor_price": {"amount": 450}}
            ]
        },
    )

    for _ in range(2):
        _ = await api.post("/tick")

    negotiations = await FirestoreRepository(firestore).list_negotiations(PID)
    assert negotiations
    assert all(n.floor_price == Money(amount=450) for n in negotiations.values())


async def test_opening_negotiations_twice_does_not_email_anyone_twice(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Negotiation ids are derived from the item and supplier.

    A tick killed midway through opening three of them re-runs and collides on
    the ones already written, so create() refuses rather than starting a second
    conversation with the same seller.
    """
    await _new_project(api)
    _ = await _upload(api)
    _ = await api.post(
        f"/projects/{PID}/items/confirm",
        json={"items": [{"item_id": item_id_for("cup")}]},
    )

    for _ in range(4):
        _ = await api.post("/tick")

    repo = FirestoreRepository(firestore)
    negotiations = await repo.list_negotiations(PID)
    item = await repo.get_item(PID, item_id_for("cup"))
    assert item is not None

    expected = {
        negotiation_id_for(item_id_for("cup"), supplier_id_for(s.email))
        for s in (await repo.list_suppliers(PID)).values()
    }
    assert set(negotiations) == expected


async def test_research_is_retried_rather_than_abandoned_when_it_finds_nobody(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """An empty search might be transient; giving up immediately would be wrong."""

    class NoSuppliers(ScriptedBrain):
        @override
        async def research_item(self, brief: ItemBrief) -> ItemResearch:
            found = await super().research_item(brief)
            return found.model_copy(update={"supplier_candidates": []})

    repo = FirestoreRepository(firestore)
    clock = SimClock(repo, FrozenRealTime(REAL0))
    brain = NoSuppliers()
    app.state.services = Services(
        settings=SETTINGS,
        client=firestore,
        repo=repo,
        clock=clock,
        brain=brain,
        mail=InMemoryMailbox(),
        loop=TickLoop(repo, clock, brain, InMemoryMailbox()),
    )

    await _new_project(api)
    _ = await _upload(api)
    _ = await api.post(
        f"/projects/{PID}/items/confirm",
        json={"items": [{"item_id": item_id_for("cup")}]},
    )
    _ = await api.post("/tick")

    item = await repo.get_item(PID, item_id_for("cup"))
    assert item is not None
    assert item.status is ItemStatus.RESEARCHING
    assert item.next_action_due_at is not None, "it should come back and try again"
    assert item.next_action_due_at > REAL0 - timedelta(days=1)


# --------------------------------------------------------------------------- #
# Endpoint edges
# --------------------------------------------------------------------------- #


async def test_uploading_to_a_project_that_does_not_exist_is_a_404(
    api: httpx.AsyncClient,
) -> None:
    response = await api.post("/projects/ghost/script", json={"text_content": SCRIPT})
    assert response.status_code == 404


async def test_creating_the_same_project_twice_is_a_409(
    api: httpx.AsyncClient,
) -> None:
    await _new_project(api)
    again = await api.post("/projects", json={"project_id": PID, "title": "again"})
    assert again.status_code == 409


async def test_an_empty_script_is_rejected(api: httpx.AsyncClient) -> None:
    await _new_project(api)
    response = await api.post(f"/projects/{PID}/script", json={"text_content": ""})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Overlapping ticks
# --------------------------------------------------------------------------- #


class _CountingBrain(ScriptedBrain):
    """Counts research calls. The thing overlapping ticks would pay for twice."""

    researched: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.researched = []

    @override
    async def research_item(self, brief: ItemBrief) -> ItemResearch:
        self.researched.append(brief.item_id)
        return await super().research_item(brief)


class _RendezvousRepository(FirestoreRepository):
    """Holds both ticks at the due-item read, so they genuinely race."""

    _barrier: asyncio.Barrier

    def __init__(self, client: AsyncClient, barrier: asyncio.Barrier) -> None:
        super().__init__(client)
        self._barrier = barrier

    @override
    async def due_items(self, now: datetime, *, limit: int = 25) -> list[DueItem]:
        due = await super().due_items(now, limit=limit)
        _ = await self._barrier.wait()
        return due


async def test_two_overlapping_ticks_research_an_item_once(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Losing this race costs money rather than a supplier's goodwill.

    ``research_item`` is a slow LLM call, and two ticks arriving together would
    both make it and then write identical results — the damage invisible in the
    data and visible only on the bill.

    Note the third assertion, which was written the wrong way round first. It
    originally required that every researched item also be *counted* as
    researched, and it failed roughly one run in five with
    ``409 Transaction lock timeout`` — Firestore aborting one of two genuinely
    concurrent writes to the same document. That is not a test artifact and not
    something to retry around: it is what contention looks like, and it will
    happen on Cloud Run for the same reason it happens here.

    What the system owes us under contention is not that every item succeeds.
    It is that no item is silently dropped: each one either completes or is
    reported, and a reported one comes back when its lease runs out.
    """
    await _new_project(api)
    _ = await _confirm_everything(api)

    barrier = asyncio.Barrier(2)
    brains = [_CountingBrain(), _CountingBrain()]
    loops = [
        TickLoop(
            repo := _RendezvousRepository(firestore, barrier),
            SimClock(repo, FrozenRealTime(REAL0)),
            brain,
            InMemoryMailbox(),
        )
        for brain in brains
    ]

    reports = await asyncio.gather(*(loop.run_tick(PID) for loop in loops))

    researched = brains[0].researched + brains[1].researched
    assert len(researched) == len(set(researched)), (
        "no item may be researched twice across two overlapping ticks"
    )
    assert sum(r.claims_lost for r in reports) > 0, "the ticks did actually race"

    counted = sum(r.items_researched for r in reports)
    reported = sum(len(r.errors) for r in reports)
    assert counted + reported == len(set(researched)), (
        "every item is either finished or reported — none vanish"
    )


async def test_an_item_lost_to_contention_comes_back(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """The other half of the promise above: reported is not the same as dropped.

    Whatever the two racing ticks failed to finish is claimed, so it is quiet
    until the lease expires and then due again. A later tick completes it, and
    every confirmed item ends up sourced.
    """
    await _new_project(api)
    props = await _confirm_everything(api)

    barrier = asyncio.Barrier(2)
    loops = [
        TickLoop(
            repo := _RendezvousRepository(firestore, barrier),
            SimClock(repo, FrozenRealTime(REAL0)),
            ScriptedBrain(),
            InMemoryMailbox(),
        )
        for _ in range(2)
    ]
    _ = await asyncio.gather(*(loop.run_tick(PID) for loop in loops))

    # Past the lease, with a single tick this time.
    repo = FirestoreRepository(firestore)
    clock = SimClock(repo, FrozenRealTime(REAL0))
    _ = await clock.set_sim_now(PID, REAL0 + timedelta(hours=1))
    settled = TickLoop(repo, clock, ScriptedBrain(), InMemoryMailbox())
    for _ in range(3):
        _ = await settled.run_tick(PID)

    items = await repo.list_items(PID)
    assert len(items) == len(props)
    assert all(i.status is ItemStatus.NEGOTIATING for i in items.values()), {
        k: v.status for k, v in items.items()
    }
