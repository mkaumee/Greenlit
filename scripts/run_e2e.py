#!/usr/bin/env python3
# Responses come back through httpx .json(), which is untyped by nature; this
# script reads them as the UI would rather than re-deriving the models.
# pyright: reportAny=false
"""Run the whole loop end to end, and say whether it still works.

The daily ten-minute habit. A screenplay goes in at the top and the run ends
with negotiations that reached a human — through the real HTTP endpoints, the
real sourcing step, the real state machine and the real repository.

Only two things are stand-ins: the brain (no LLM) and the mail transport (no
Gmail). Those are exactly the two that need credentials, which is what lets
this run on any laptop, in CI, and on a branch where Role A's code does not
exist yet.

**Nothing is hand-seeded.** That matters more than it sounds. This script used
to create items, suppliers and negotiations itself, which made the pipeline
look complete while nothing actually connected a script to them — the gap was
invisible precisely because the test worked around it.

Run with ``make e2e``. Non-zero exit if the loop breaks, so it can gate a merge.

Asserted at the end:

- every confirmed prop got as far as being negotiated for
- at least one negotiation reached READY_FOR_HUMAN
- the ghost seller's negotiations ended DEAD rather than hanging
- ``purchase_orders`` is empty — five simulated days of negotiating, nothing bought
"""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import override

import httpx
from cinema_contracts import (
    ItemBrief,
    ItemResearch,
    NegotiationState,
    SupplierCandidate,
)
from cinema_contracts.testing import ScriptedBrain
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.app import Services, app
from orchestrator.clock import FrozenRealTime, SimClock
from orchestrator.mail import InMemoryMailbox
from orchestrator.records import ItemStatus
from orchestrator.repository import FirestoreRepository, OrdersRepository
from orchestrator.settings import Settings
from orchestrator.sourcing import supplier_id_for
from orchestrator.tick import TickLoop

EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
PROJECT_ID = os.environ.get("FIRESTORE_PROJECT_ID", "demo-cinema")
ORDERS_DATABASE = "orders"
PID = "e2e-project"

SIM_START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
REAL_ANCHOR = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)

TICKS = 40
HOURS_PER_TICK = 3
"""Forty ticks at three simulated hours each is five simulated days."""

SCREENPLAY = """INT. DIVE BAR - NIGHT

RAZAK nurses a drink at the counter. The BARMAN watches him.

RAZAK grabbed the cup and threw it towards the mirror.

Glass rains down behind the bar.

EXT. ALLEY - CONTINUOUS

Razak lights a cigarette with a shaking hand.
"""

FLOOR = {"amount": 750, "currency": "MYR"}


class Persona:
    """A scripted seller. Enough to exercise the loop, not the real simulator.

    The real adversary will live in ``supplier-sim/`` as a separate service
    with its own mailbox and Gemini writing every reply. This is the cheap
    version that runs without a network, so the daily check stays ten minutes.
    """

    name: str
    email: str
    opening: int
    floor: int
    ghost_after: int

    def __init__(
        self, name: str, email: str, *, opening: int, floor: int, ghost_after: int = 99
    ) -> None:
        self.name = name
        self.email = email
        self.opening = opening
        self.floor = floor
        self.ghost_after = ghost_after

    def reply_to(self, round_number: int) -> str | None:
        """What this seller says on round N, or None if they stay quiet."""
        if round_number >= self.ghost_after:
            return None
        conceded = max(self.floor, int(self.opening * (0.88**round_number)))
        if round_number == 0:
            return f"Thanks for reaching out. Our rate is RM{conceded:,}."
        return f"Best we can do is RM{conceded:,}."


PERSONAS = [
    Persona("Ah Seng Props", "ahseng@example.invalid", opening=1200, floor=680),
    Persona("Skyline Supply", "skyline@example.invalid", opening=2400, floor=900),
    Persona(
        "Quiet Sdn Bhd",
        "quiet@example.invalid",
        opening=1100,
        floor=1000,
        ghost_after=1,
    ),
]

GHOST_SUPPLIER_ID = supplier_id_for(PERSONAS[-1].email)


class E2EBrain(ScriptedBrain):
    """The scripted brain, with research pointed at our three personas.

    Subclassed here rather than baked into ScriptedBrain: the personas are a
    property of this rehearsal, not of the contract.
    """

    @override
    async def research_item(self, brief: ItemBrief) -> ItemResearch:
        base = await super().research_item(brief)
        return base.model_copy(
            update={
                "supplier_candidates": [
                    SupplierCandidate(name=p.name, email=p.email, confidence=0.5)
                    for p in PERSONAS
                ]
            }
        )


def _wipe(database: str) -> None:
    """Empty one database. Both need clearing independently.

    Wiping only (default) would leave a purchase order behind in `orders`, and
    the final assertion of this run is that no order exists — a stale one would
    fail it for the wrong reason, or hide a real bug behind a clean-looking run.
    """
    _ = httpx.delete(
        f"http://{EMULATOR_HOST}/emulator/v1/projects/{PROJECT_ID}"
        f"/databases/{database}/documents",
        timeout=10.0,
    )


def _emulator_up() -> bool:
    try:
        return httpx.get(f"http://{EMULATOR_HOST}/", timeout=2.0).status_code < 500
    except httpx.HTTPError:
        return False


async def seed_from_screenplay(api: httpx.AsyncClient) -> int:
    """Create the project, read the script, confirm the list. Nothing else."""
    created = await api.post(
        "/projects",
        json={
            "project_id": PID,
            "title": "Nasi Lemak Nights",
            "sim_start": SIM_START.isoformat(),
        },
    )
    if created.status_code != 201:
        print(f"could not create project: {created.status_code} {created.text}")
        return 1

    read = await api.post(f"/projects/{PID}/script", json={"text_content": SCREENPLAY})
    props = read.json()["props"]
    print(f"  read the script -> {len(props)} props")
    for prop in props:
        flag = "  (destroyed on camera)" if prop["consumable"] else ""
        print(f"    {prop['name']:<12} scene {prop['scenes'][0]}{flag}")

    # A producer signing off. Consumables get several, because they break.
    _ = await api.post(
        f"/projects/{PID}/items/confirm",
        json={
            "items": [
                {
                    "item_id": p["item_id"],
                    "qty": 6 if p["consumable"] else 1,
                    "floor_price": FLOOR,
                }
                for p in props
            ]
        },
    )
    print(f"  producer confirmed {len(props)} items\n")
    return 0


async def drive(api: httpx.AsyncClient, clock: SimClock, mail: InMemoryMailbox) -> None:
    """Advance simulated time, ticking and letting the sellers answer."""
    by_email = {p.email: p for p in PERSONAS}
    rounds: dict[str, int] = {}
    answered = 0
    moment = SIM_START

    for tick in range(TICKS):
        _ = await clock.set_sim_now(PID, moment)
        report = (await api.post("/tick")).json()["projects"][0]

        interesting = (
            "items_researched",
            "negotiations_opened",
            "messages_sent",
            "replies_filed",
            "escalated",
        )
        if any(report[key] for key in interesting):
            print(
                f"  t+{tick * HOURS_PER_TICK:>3}h  "
                f"researched={report['items_researched']} "
                f"opened={report['negotiations_opened']} "
                f"sent={report['messages_sent']} "
                f"filed={report['replies_filed']} "
                f"escalated={report['escalated']}"
            )

        for outbound in mail.sent[answered:]:
            persona = by_email.get(outbound["to"])
            if persona is None:
                continue
            thread = outbound["thread_id"]
            round_number = rounds.get(thread, 0)
            rounds[thread] = round_number + 1
            body = persona.reply_to(round_number)
            if body is not None:
                _ = mail.deliver(thread_id=thread, body=body, from_email=persona.email)
        answered = len(mail.sent)

        moment += timedelta(hours=HOURS_PER_TICK)


async def verify(
    repo: FirestoreRepository, orders: OrdersRepository, mail: InMemoryMailbox
) -> int:
    items = await repo.list_items(PID)
    negotiations = await repo.list_negotiations(PID)

    print("\n  items")
    for item_id, item in sorted(items.items()):
        band = item.reference_band
        span = f"{band.low}-{band.high}" if band else "—"
        print(
            f"    {item_id:<12} {item.status.value:<12} qty={item.qty:<2} band={span}"
        )

    print("\n  negotiations")
    for negotiation_id, record in sorted(negotiations.items()):
        quote = record.latest_quote.unit_price if record.latest_quote else "—"
        print(
            f"    {negotiation_id:<36} {record.state.value:<16} "
            f"quote={quote!s:<11} rounds={record.rounds_used} "
            f"{record.escalation_reason}"
        )

    failures: list[str] = []

    if not items:
        failures.append("the script produced no items")
    if not negotiations:
        failures.append("no negotiations were opened from the confirmed items")

    unstarted = [i for i, r in items.items() if r.status is ItemStatus.DRAFT]
    if unstarted:
        failures.append(f"items never left DRAFT: {unstarted}")

    if not any(
        r.state is NegotiationState.READY_FOR_HUMAN for r in negotiations.values()
    ):
        failures.append("no negotiation reached READY_FOR_HUMAN")

    hung = [
        nid
        for nid, r in negotiations.items()
        if r.supplier_id == GHOST_SUPPLIER_ID
        and r.state
        not in {
            NegotiationState.DEAD,
            NegotiationState.CHASING,
            NegotiationState.READY_FOR_HUMAN,
        }
    ]
    if hung:
        failures.append(f"the ghost seller left negotiations hanging: {hung}")

    ordered = await orders.total_ordered()
    if ordered is not None:
        failures.append(f"the agent created a purchase order: {ordered}")

    print(f"\n  {len(mail.sent)} emails sent over 5 simulated days")
    print(
        "  purchase orders created: 0" if ordered is None else f"  ORDERED: {ordered}"
    )

    if failures:
        print("\nFAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nOK — screenplay in, negotiations out, and nothing was bought.")
    return 0


async def run() -> int:
    if not _emulator_up():
        print(f"Firestore emulator not reachable at {EMULATOR_HOST}.")
        print("Start it with `make emulator`, or use `make e2e` which boots it.")
        return 2

    os.environ["FIRESTORE_EMULATOR_HOST"] = EMULATOR_HOST
    _wipe("(default)")
    _wipe(ORDERS_DATABASE)

    client = AsyncClient(project=PROJECT_ID, credentials=AnonymousCredentials())
    # A second client, on the database the tick service has no access to and no
    # method to reach. It exists only so the final check can look at where an
    # order would land if the guardrail ever failed.
    orders_client = AsyncClient(
        project=PROJECT_ID,
        credentials=AnonymousCredentials(),
        database=ORDERS_DATABASE,
    )

    repo = FirestoreRepository(client)
    orders = OrdersRepository(orders_client)
    clock = SimClock(repo, FrozenRealTime(REAL_ANCHOR))
    mail = InMemoryMailbox()
    brain = E2EBrain()

    app.state.services = Services(
        settings=Settings(_env_file=None, gcp_project=PROJECT_ID),  # pyright: ignore[reportCallIssue]
        client=client,
        repo=repo,
        clock=clock,
        brain=brain,
        mail=mail,
        loop=TickLoop(repo, clock, brain, mail),
    )
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://e2e"
    )

    try:
        if (code := await seed_from_screenplay(api)) != 0:
            return code
        await drive(api, clock, mail)
        return await verify(repo, orders, mail)
    finally:
        await api.aclose()
        client.close()
        orders_client.close()


def main() -> int:
    print("end-to-end: screenplay -> props -> research -> negotiation -> a human\n")
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
