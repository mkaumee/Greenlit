"""The data shapes that cross between Role A (the brain) and Role B (the runtime).

Five of these are the load-bearing payloads:

1. ``Money``            — the unit primitive, in ``money.py``
2. ``ItemBrief``        — B tells A what is being bought
3. ``NegotiationContext`` — B tells A everything known about one negotiation
4. ``QuoteExtraction``  — A tells B what a supplier's email actually said
5. ``NextMove``         — A tells B what to do next

Everything else here is a value object appearing inside one of those five.

Changing any model in this file is a two-person decision. Both services import
it, and a field added on one side without the other is exactly the divergence
the daily end-to-end run exists to catch.
"""

from datetime import datetime
from typing import ClassVar, override

from pydantic import BaseModel, ConfigDict, Field

from cinema_contracts.enums import (
    EscalationReason,
    MessageDirection,
    MoveAction,
    NegotiationState,
)
from cinema_contracts.money import Currency, Money


class _Frozen(BaseModel):
    """Base for value objects. Immutable, and rejects unknown fields.

    ``extra="forbid"`` is deliberate: if Role A starts sending a field Role B
    has not agreed to, the boundary fails loudly on the next end-to-end run
    instead of silently dropping data for a week.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Research inputs and outputs
# --------------------------------------------------------------------------- #


class ReferenceBand(_Frozen):
    """What this kind of item usually costs, with receipts.

    Shown next to supplier quotes in the UI so the producer can see whether a
    number is reasonable without knowing the market themselves. ``source_urls``
    is what makes it defensible rather than a hallucinated range.
    """

    low: Money
    high: Money
    source_urls: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    notes: str = ""


class SupplierCandidate(_Frozen):
    """A supplier the research step believes exists and can be emailed.

    ``verified`` stays False until a human or a successful send confirms the
    address. The agent will not open a negotiation against an unverified
    address without that check, because a bounced first contact burns a
    simulated day.
    """

    name: str
    email: str
    source_url: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    verified: bool = False
    notes: str = ""


class ScriptSource(_Frozen):
    """An uploaded screenplay, before anyone has read it for objects.

    Text or bytes, never both and never neither.

    ``content_b64`` exists because a screenplay is a layout format — scene
    headings at the margin, character names centred, dialogue indented — and
    flattening a PDF to a string before the model sees it throws that away.
    Role A hands the document to Gemini as an attachment instead, which also
    means a scanned script works: the model reads the page, and an extractor
    would have found no text layer and given up.

    Role A must exclude ``content_b64`` from any prompt text it builds. A
    megabyte of base64 in the prose is not a document to a model; it is noise
    it may well try to read.
    """

    filename: str
    mime_type: str
    text_content: str = ""
    content_b64: str = ""
    """The file itself, for formats that are not usefully text: PDF today."""
    gcs_uri: str = ""
    """Unused. The door for a screenplay too large to inline."""

    @override
    def model_post_init(self, _context: object, /) -> None:
        has_text = bool(self.text_content.strip())
        has_bytes = bool(self.content_b64.strip())
        if has_text == has_bytes:
            # Neither is the failure this whole area keeps circling: an empty
            # source comes back as a confident list of no props, which reads
            # exactly like a screenplay that needs nothing bought. Both is two
            # sources of truth for one screenplay, and nothing says which wins.
            raise ValueError(
                "ScriptSource needs exactly one of text_content or "
                "content_b64. Neither means the brain would be asked to read "
                "nothing and would answer anyway; both means two screenplays."
            )


class SceneMention(_Frozen):
    """Where a prop was spotted, and the words that put it there.

    ``line`` is the receipt. A producer checking the list needs to see *"he
    grabbed the cup and threw it at the mirror"* next to "mirror", because that
    is the difference between auditing the agent's work and taking its word.
    It is also the fastest way to catch a hallucinated prop: no line, no prop.
    """

    scene_number: str
    line: str = Field(min_length=1, description="The sentence the prop came from.")
    page: int | None = None


class PropDraft(_Frozen):
    """One physical thing a scene needs, as found in the script.

    A draft because nothing is persisted until the producer confirms the list.
    """

    name: str
    category: str = "prop"
    """prop, set dressing, costume, equipment, vehicle, ..."""

    qty: int = Field(ge=1, default=1)
    mentions: list[SceneMention] = Field(default_factory=list)

    consumable: bool = False
    """True when the action destroys or uses up the object.

    The mirror gets smashed; the cup might survive. A destroyed prop needs one
    per take, not one per production, and getting that wrong is how a shoot
    stops at lunchtime. The brain flags it here; a human decides the multiple,
    because only they know the shooting schedule.
    """

    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    notes: str = ""


class ItemBrief(_Frozen):
    """What Role A is told about an item it is researching or negotiating for."""

    item_id: str
    name: str
    category: str
    scenes: list[str] = Field(default_factory=list)
    qty: int = Field(ge=1, default=1)
    consumable: bool = False
    """Carried through to negotiation: it changes what we are asking to buy.

    Sourcing one mirror and sourcing six identical breakaway mirrors are
    different conversations, and the seller needs to be told which one it is.
    """

    notes: str = ""
    reference_band: ReferenceBand | None = None
    currency: Currency = "MYR"


class ItemResearch(_Frozen):
    """What the brain found out about an item: what it costs and who sells it."""

    reference_band: ReferenceBand
    supplier_candidates: list[SupplierCandidate] = Field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------- #
# Reading a supplier's reply
# --------------------------------------------------------------------------- #


class InboundMessage(_Frozen):
    """A supplier email, as received, before anyone has understood it.

    ``has_attachments`` matters: the persona who buries the price in a PDF is a
    deliberate test case, and the correct response is to escalate rather than
    to guess at a number from the covering note.
    """

    message_id: str
    thread_id: str
    from_email: str
    subject: str
    body: str
    received_at: datetime = Field(description="Simulation time, from clock.now().")
    has_attachments: bool = False
    attachment_filenames: list[str] = Field(default_factory=list)


class ExtractedQuote(_Frozen):
    """A price the brain is confident it read correctly out of an email."""

    unit_price: Money
    qty: int = Field(ge=1, default=1)
    total: Money
    lead_time_days: int | None = None
    includes_delivery: bool | None = None
    terms: str = ""


class QuoteExtraction(_Frozen):
    """The result of trying to read a quote out of a supplier's reply.

    Either ``quote`` is set, or ``needs_human`` is True with a reason. Both at
    once is a contradiction; neither is a bug. ``model_post_init`` enforces it,
    because an extraction that is quietly empty turns into an agent that stalls
    with no explanation on screen.
    """

    quote: ExtractedQuote | None = None
    needs_human: bool = False
    escalation_reason: EscalationReason | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    notes: str = ""

    @override
    def model_post_init(self, _context: object, /) -> None:
        if self.quote is None and not self.needs_human:
            raise ValueError(
                "QuoteExtraction must either carry a quote or set needs_human. "
                "An empty extraction leaves the negotiation stuck silently."
            )
        if self.needs_human and self.escalation_reason is None:
            raise ValueError(
                "needs_human requires an escalation_reason — it is shown to the "
                "producer as the explanation for why the agent stopped."
            )


# --------------------------------------------------------------------------- #
# Deciding what to do next
# --------------------------------------------------------------------------- #


class MessageSummary(_Frozen):
    """One turn of the conversation, as handed to the brain for context."""

    direction: MessageDirection
    body: str
    sim_sent_at: datetime
    extracted_quote: ExtractedQuote | None = None


class NegotiationContext(_Frozen):
    """Everything the brain is allowed to know when deciding its next move.

    Assembled fresh from Firestore on every tick. The brain holds no memory
    between calls — Hard Rule 3 — so if a fact is not in here, it does not
    exist as far as the decision is concerned.
    """

    negotiation_id: str
    state: NegotiationState
    item: ItemBrief
    supplier: SupplierCandidate

    floor_price: Money | None = Field(
        default=None,
        description=(
            "The producer's stop condition. The agent may not accept above this "
            "and may not keep pushing below it. Set by a human, never by the agent."
        ),
    )
    target_price: Money | None = None
    rounds_used: int = Field(ge=0, default=0)
    max_rounds: int = Field(ge=1, default=4)

    first_quote: ExtractedQuote | None = None
    latest_quote: ExtractedQuote | None = None

    history: list[MessageSummary] = Field(default_factory=list)

    now: datetime = Field(description="Simulation time. Never wall-clock time.")
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None

    @property
    def rounds_remaining(self) -> int:
        return max(0, self.max_rounds - self.rounds_used)

    @property
    def sim_hours_since_last_outbound(self) -> float | None:
        """How long the supplier has been silent, in simulated hours."""
        if self.last_outbound_at is None:
            return None
        return (self.now - self.last_outbound_at).total_seconds() / 3600.0


class NextMove(_Frozen):
    """The brain's decision, and the reasoning a producer will read.

    ``reasoning`` is not a debug field. It is rendered on the item detail screen
    underneath the recommendation, and it is a large part of why the system
    reads as an agent making judgements rather than a form submitting values.

    ``suggest_next_check_in_sim_hours`` is advice, not a command. Role B clamps
    it and owns the actual ``next_action_due_at`` that goes in Firestore.
    """

    action: MoveAction
    reasoning: str = Field(min_length=1)

    draft_subject: str = ""
    draft_body: str = ""
    target_price: Money | None = None

    escalation_reason: EscalationReason | None = None
    suggest_next_check_in_sim_hours: float | None = Field(default=None, gt=0)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    @override
    def model_post_init(self, _context: object, /) -> None:
        sends_mail = self.action in {
            MoveAction.SEND_OPENING,
            MoveAction.COUNTER,
            MoveAction.CHASE,
        }
        if sends_mail and not self.draft_body.strip():
            raise ValueError(
                f"{self.action} has to produce an email body — Role B sends "
                f"exactly what the brain wrote and does not compose text itself."
            )
        if self.action is MoveAction.COUNTER and self.target_price is None:
            raise ValueError(
                "A counter-offer needs a target_price so the UI can show the "
                "producer what the agent is pushing for."
            )
        needs_reason = {MoveAction.ESCALATE, MoveAction.ACCEPT}
        if self.action in needs_reason and self.escalation_reason is None:
            raise ValueError(
                f"{self.action} stops the negotiation for a human, so it must say "
                f"why. Use GOOD_QUOTE when the negotiation simply succeeded."
            )


# --------------------------------------------------------------------------- #
# Talking to a producer
# --------------------------------------------------------------------------- #


class BriefingItem(_Frozen):
    """One prop, flattened for a question about the production."""

    item_id: str
    name: str
    status: str
    qty: int = Field(ge=0, default=1)
    best_quote: Money | None = None
    reference_low: Money | None = None
    reference_high: Money | None = None
    script_line: str = Field(
        default="",
        description=(
            "The line it was found in. The receipt, and the reason a producer "
            "can check the agent rather than trust it."
        ),
    )


class BriefingNegotiation(_Frozen):
    """One negotiation, flattened for the same."""

    negotiation_id: str
    item_id: str
    item_name: str
    supplier: str
    state: NegotiationState
    rounds_used: int = Field(ge=0, default=0)
    max_rounds: int = Field(ge=1, default=4)
    first_quote: Money | None = None
    latest_quote: Money | None = None
    reasoning: str = Field(
        default="",
        description="The brain's own last explanation, so a briefing can say why.",
    )
    waiting_on_human: bool = False
    escalation_reason: str = ""


class ProducerQuestion(_Frozen):
    """Everything the brain may draw on when answering a producer, and nothing else.

    The digest is a boundary, not a convenience. Whatever is not in here cannot
    be talked about, and the briefing that comes back is checked against the ids
    it carried — so a model that invents a supplier or a prop is caught by
    comparison rather than trusted. Handing over the raw collections would make
    that check meaningless.

    It is also the same data the panel reads, so the chat and the screens cannot
    disagree about what is true.
    """

    project_id: str
    title: str
    question: str
    items: list[BriefingItem] = Field(default_factory=list)
    negotiations: list[BriefingNegotiation] = Field(default_factory=list)


class ProducerBriefing(_Frozen):
    """An answer, and the things it pointed at.

    Prose and ids only. No field here causes a write, so the brain cannot reach
    money even by being wrong about everything — the same reason ``ACCEPT``
    routes to a human instead of buying.
    """

    text: str = Field(min_length=1)
    referenced_item_ids: list[str] = Field(default_factory=list)
    referenced_negotiation_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
