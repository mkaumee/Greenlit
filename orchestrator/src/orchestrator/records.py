"""What actually lives in Firestore.

Kept separate from ``cinema_contracts.models`` on purpose. Those are the shapes
that cross to Role A; these are the shapes on disk. They overlap heavily today,
and conflating them would be convenient right up until one needs a field the
other must not have — a storage detail leaking into the brain's context, or a
brain-facing field forcing a migration.

Layout::

    projects/{pid}
    projects/{pid}/items/{iid}
    projects/{pid}/suppliers/{sid}
    projects/{pid}/negotiations/{nid}
    projects/{pid}/negotiations/{nid}/messages/{mid}
    purchase_orders/{iid}          <- top level, keyed by item

``purchase_orders`` is top level and keyed by item ID rather than by its own
generated ID. That is the whole guardrail: see ``repository.py``.
"""

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from cinema_contracts import (
    ExtractedQuote,
    Money,
    NegotiationState,
    ReferenceBand,
    SceneMention,
)
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.clock import ClockState


class _Record(BaseModel):
    """Base for persisted documents.

    Unknown fields are allowed here, unlike the contract models. A document
    written by a newer deployment must not break an older reader mid-demo, and
    dropping an unrecognised field is better than refusing to load the
    negotiation it belongs to.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    def to_firestore(self) -> dict[str, object]:
        """Plain dict for the Firestore client.

        ``mode="python"`` deliberately: datetimes stay native so Firestore
        stores real timestamps, and ``StrEnum`` members are already strings.
        """
        return self.model_dump(mode="python", exclude_none=True)


class ItemStatus(StrEnum):
    """Where an item is in the procurement flow, for the breakdown screen.

    The first two transitions are the interesting ones::

        DRAFT ──confirmed by a producer──> RESEARCHING ──> SOURCING ──> NEGOTIATING

    Nothing is researched and nobody is emailed while an item is ``DRAFT``. The
    agent has read the script and produced a list; a human still has to say
    that list is right. Skipping that would mean a hallucinated prop quietly
    turning into a real email to a real seller.
    """

    DRAFT = "DRAFT"
    """Found in the script, not yet confirmed by a person. Inert."""

    RESEARCHING = "RESEARCHING"
    """Confirmed. Due for a reference band and supplier candidates."""

    SOURCING = "SOURCING"
    """Researched. Due for negotiations to be opened."""

    NEGOTIATING = "NEGOTIATING"
    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    ORDERED = "ORDERED"
    ABANDONED = "ABANDONED"
    """Dropped by the producer at confirmation, or nothing could be sourced."""


ITEM_TERMINAL_STATUSES: frozenset[ItemStatus] = frozenset(
    {ItemStatus.ORDERED, ItemStatus.ABANDONED}
)


class ProjectRecord(_Record):
    """``projects/{pid}``"""

    title: str
    clock: ClockState
    budget_baseline: Money | None = None
    created_at: datetime


class ItemRecord(_Record):
    """``projects/{pid}/items/{iid}``"""

    name: str
    category: str
    scenes: list[str] = Field(default_factory=list)
    qty: int = Field(ge=1, default=1)
    notes: str = ""

    mentions: list[SceneMention] = Field(default_factory=list)
    """The script lines this item was found in. Shown on the item detail screen
    so a producer can see why the agent thinks the shoot needs this."""

    consumable: bool = False
    """Destroyed on camera, so the quantity is per take rather than per shoot."""

    reference_band: ReferenceBand | None = None
    status: ItemStatus = ItemStatus.DRAFT
    chosen_quote: ExtractedQuote | None = None

    supplier_ids: list[str] = Field(default_factory=list)
    """Sellers research found *for this item*.

    Suppliers are stored per project because one company often sells several
    things, but negotiations are opened from this list rather than from every
    supplier in the project — otherwise finding a lighting hire firm for the
    SkyPanel would open a negotiation with them about the smoke machine too.
    """

    floor_price: Money | None = None
    """The producer's ceiling, set when they confirm the item.

    Lives on the item rather than only on each negotiation so that every seller
    approached for it inherits the same limit, including ones opened later.
    """

    next_action_due_at: datetime | None = None
    """Drives the item side of the tick, exactly as it does for negotiations.

    Absent while an item is ``DRAFT`` — an unconfirmed item must never be
    picked up — and removed again once it is terminal, which drops it out of
    the index rather than leaving a row to filter on every pass.
    """

    updated_at: datetime | None = None


class SupplierRecord(_Record):
    """``projects/{pid}/suppliers/{sid}``"""

    name: str
    email: str
    source_url: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    verified: bool = False


class NegotiationRecord(_Record):
    """``projects/{pid}/negotiations/{nid}``

    ``next_action_due_at`` is the only field the tick loop queries on, and it is
    deliberately absent rather than null once a negotiation reaches a terminal
    state. A missing field drops the document out of the index entirely, so
    finished negotiations cost nothing to skip — there is no filter to write and
    no rows to read.
    """

    item_id: str
    supplier_id: str
    state: NegotiationState = NegotiationState.DRAFTED

    floor_price: Money | None = None
    target_price: Money | None = None
    rounds_used: int = Field(ge=0, default=0)
    max_rounds: int = Field(ge=1, default=4)

    gmail_thread_id: str = ""
    last_msg_id: str = ""
    """Gmail's API id for our last outbound. For lookups and debugging."""

    thread_root_rfc822_id: str = ""
    last_rfc822_id: str = ""
    """RFC-822 ``Message-ID`` headers: the thread's first message, and the most
    recent one in it.

    These are what ``In-Reply-To`` and ``References`` are built from. Keeping
    the root and the last one is bounded — a full ``References`` chain grows
    with every round — and is enough for every mail client to thread correctly.
    """

    first_quote: ExtractedQuote | None = None
    latest_quote: ExtractedQuote | None = None

    next_action_due_at: datetime | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None

    escalation_reason: str = ""
    latest_reasoning: str = ""
    """The brain's last explanation, shown on the item detail screen."""

    created_at: datetime
    updated_at: datetime


class MessageRecord(_Record):
    """``projects/{pid}/negotiations/{nid}/messages/{mid}``

    Append-only. The timeline is the only proof that simulated days passed, and
    it stops being evidence the moment anything can rewrite it.
    """

    direction: str
    body: str
    subject: str = ""
    sim_sent_at: datetime
    gmail_message_id: str = ""
    extracted_quote: ExtractedQuote | None = None
    needs_human: bool = False


class PurchaseOrderRecord(_Record):
    """``purchase_orders/{item_id}``

    Note there is no ``id`` field of its own: the document ID *is* the item ID.
    Storing ``item_id`` in the body too is redundant on purpose — the security
    rule checks the two agree, so a client cannot write an order whose payload
    claims a different item than the key it was filed under.
    """

    item_id: str
    project_id: str
    supplier_id: str
    negotiation_id: str
    price: Money
    approved_by: str
    approved_at: datetime
