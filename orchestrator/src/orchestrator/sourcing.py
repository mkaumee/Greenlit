"""Turning confirmed items into open negotiations.

The front half of the pipeline. Everything here runs inside the tick, off the
same due-date queue that drives negotiations, for the same reason: it makes
long LLM-backed work killable without a second recovery story.

::

    DRAFT ──a human confirms──> RESEARCHING ──> SOURCING ──> NEGOTIATING
                                    │               │
                              research_item    open one negotiation
                              band + sellers   per candidate seller

Two steps rather than one, deliberately. Research is a slow LLM call and
opening negotiations fans out into several writes; splitting them means a tick
that dies partway leaves a clearly-defined amount of work done, and the next
tick picks up exactly where it stopped.

Nothing in here emails anyone. Opening a negotiation only creates a document in
``DRAFTED``; the negotiation half of the tick sends the first message on its
next pass. That keeps "who talks to sellers" in one place.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cinema_contracts import AgentBrain, ItemBrief, NegotiationState

from orchestrator.records import (
    ItemRecord,
    ItemStatus,
    NegotiationRecord,
    SupplierRecord,
)
from orchestrator.repository import DueItem, FirestoreRepository

RESEARCH_RETRY_HOURS = 6.0
"""How long to wait before retrying an item whose research produced nothing."""

CLAIM_LEASE_HOURS = 0.25
"""How far ahead claiming an item parks it. See ``tick.CLAIM_LEASE_HOURS``."""

MAX_SUPPLIERS_PER_ITEM = 3
"""How many sellers to approach for one item.

Three is enough to have something to compare and to survive one going quiet.
More would multiply the mail volume without improving the decision, and every
extra negotiation is another five-day conversation to keep alive.
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(slots=True)
class SourcingReport:
    """What the item half of a tick did."""

    items_examined: int = 0
    researched: int = 0
    negotiations_opened: int = 0
    suppliers_written: int = 0
    abandoned: int = 0
    claims_lost: int = 0
    errors: list[str] = field(default_factory=list)


def item_id_for(name: str) -> str:
    """A stable id derived from the prop's name.

    Deterministic so that re-uploading a revised script updates the items it
    already found rather than duplicating them — a mirror is the same mirror
    on the second draft. It also makes `purchase_orders/{item_id}` readable in
    the console, which matters when the guardrail is being demonstrated.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:100]
    return slug or "unnamed-item"


def supplier_id_for(email: str) -> str:
    """A stable id derived from the address.

    Deterministic on purpose. Research runs more than once — retries, a second
    item needing the same seller — and a generated id would file the same
    company twice, then open two negotiations with one inbox.
    """
    return re.sub(r"[^a-z0-9]+", "-", email.strip().lower()).strip("-")[:120]


def negotiation_id_for(item_id: str, supplier_id: str) -> str:
    """One negotiation per item-supplier pair, named after the pair.

    The same trick purchase orders use. A tick killed midway through opening
    three negotiations re-runs and collides on the ones it already wrote, so
    ``create()`` refuses them rather than emailing those sellers twice.
    """
    return f"{item_id}--{supplier_id}"


def looks_like_an_address(email: str) -> bool:
    """Cheap sanity check before we commit to writing to someone.

    Not validation — that is what a bounce is for. This only catches the brain
    returning a placeholder or a company name where an address belongs, which
    would otherwise cost a simulated day waiting for a reply that cannot come.
    """
    return bool(_EMAIL.match(email.strip()))


class SourcingLoop:
    """Advances items from confirmed, through research, to open negotiations."""

    _repo: FirestoreRepository
    _brain: AgentBrain

    def __init__(self, repo: FirestoreRepository, brain: AgentBrain) -> None:
        self._repo = repo
        self._brain = brain

    async def run(self, now: datetime, *, limit: int = 25) -> SourcingReport:
        report = SourcingReport()
        for due in await self._repo.due_items(now, limit=limit):
            report.items_examined += 1
            if not await self._repo.claim_item(
                due, now + timedelta(hours=CLAIM_LEASE_HOURS)
            ):
                # Another overlapping tick has this item. Letting both through
                # would mean paying for the same research_item call twice.
                report.claims_lost += 1
                continue
            try:
                if due.record.status is ItemStatus.RESEARCHING:
                    await self._research(due, now, report)
                elif due.record.status is ItemStatus.SOURCING:
                    await self._open_negotiations(due, now, report)
            except Exception as exc:
                report.errors.append(f"{due.item_id}: {exc}")
        return report

    # ------------------------------------------------------------------ #

    async def _research(
        self, due: DueItem, now: datetime, report: SourcingReport
    ) -> None:
        """Ask the brain what this costs and who sells it, then store both."""
        item = due.record
        research = await self._brain.research_item(brief_of(due.item_id, item))

        usable = [
            candidate
            for candidate in research.supplier_candidates
            if looks_like_an_address(candidate.email)
        ][:MAX_SUPPLIERS_PER_ITEM]

        found_ids: list[str] = []
        for candidate in usable:
            supplier_id = supplier_id_for(candidate.email)
            await self._repo.save_supplier(
                due.project_id,
                supplier_id,
                SupplierRecord(
                    name=candidate.name,
                    email=candidate.email.strip().lower(),
                    source_url=candidate.source_url,
                    confidence=candidate.confidence,
                    verified=candidate.verified,
                ),
            )
            found_ids.append(supplier_id)
        report.suppliers_written += len(usable)

        item.supplier_ids = found_ids
        item.reference_band = research.reference_band
        item.updated_at = now

        if not usable:
            # A band with nobody to buy from is not a failure the agent can
            # solve by trying harder, but it might be a transient search
            # problem, so it gets a few retries before a human is bothered.
            item.status = ItemStatus.RESEARCHING
            item.next_action_due_at = now + timedelta(hours=RESEARCH_RETRY_HOURS)
            item.notes = f"{item.notes}\nNo usable supplier address found.".strip()
        else:
            item.status = ItemStatus.SOURCING
            item.next_action_due_at = now

        await self._repo.save_item(due.project_id, due.item_id, item)
        report.researched += 1

    async def _open_negotiations(
        self, due: DueItem, now: datetime, report: SourcingReport
    ) -> None:
        """Open one negotiation per known seller for this item."""
        item = due.record

        opened = 0
        for supplier_id in item.supplier_ids[:MAX_SUPPLIERS_PER_ITEM]:
            created = await self._repo.create_negotiation(
                due.project_id,
                negotiation_id_for(due.item_id, supplier_id),
                NegotiationRecord(
                    item_id=due.item_id,
                    supplier_id=supplier_id,
                    state=NegotiationState.DRAFTED,
                    floor_price=item.floor_price,
                    # Due immediately: the negotiation half of this same tick
                    # will pick it up and send the opening email.
                    next_action_due_at=now,
                    created_at=now,
                    updated_at=now,
                ),
            )
            if created:
                opened += 1

        report.negotiations_opened += opened

        item.updated_at = now
        if opened == 0 and not item.supplier_ids:
            item.status = ItemStatus.ABANDONED
            item.next_action_due_at = None
            report.abandoned += 1
        else:
            item.status = ItemStatus.NEGOTIATING
            # The negotiations carry the schedule from here. Leaving the item
            # in the queue would re-run this every tick for no reason.
            item.next_action_due_at = None

        await self._repo.save_item(due.project_id, due.item_id, item)


def brief_of(item_id: str, item: ItemRecord) -> ItemBrief:
    """The item as the brain sees it. Shared by sourcing and the tick loop."""
    return ItemBrief(
        item_id=item_id,
        name=item.name,
        category=item.category,
        scenes=item.scenes,
        qty=item.qty,
        consumable=item.consumable,
        notes=item.notes,
        reference_band=item.reference_band,
    )
