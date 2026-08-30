"""A deterministic ``AgentBrain`` with no LLM behind it.

Two jobs:

- **Role B** runs the whole tick loop against this before the real brain exists,
  which is what makes the daily end-to-end habit possible from day one without
  stub code sitting inside ``orchestrator``.
- **Role A** can assert their real implementation is substitutable for it, and
  reuse the test cases as a behavioural floor.

It is deliberately simple-minded. It is not a reference implementation of good
negotiating — it is a reference implementation of *obeying the contract*: never
guessing at an unreadable quote, never accepting above the floor, never buying.
"""

import re
from decimal import Decimal

from cinema_contracts.enums import EscalationReason, MessageDirection, MoveAction
from cinema_contracts.models import (
    ExtractedQuote,
    InboundMessage,
    ItemBrief,
    ItemResearch,
    NegotiationContext,
    NextMove,
    PropDraft,
    QuoteExtraction,
    ReferenceBand,
    SceneMention,
    ScriptSource,
    SupplierCandidate,
)
from cinema_contracts.money import Money

_PRICE = re.compile(
    r"(?:rm|myr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)|"
    r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:rm|myr|ringgit)",
    re.IGNORECASE,
)

_SILENCE_HOURS = 48.0
"""How long a supplier may go quiet before the fake brain chases them."""

_SCENE_HEADING = re.compile(r"^(?:SCENE\s+(\S+)|(?:INT|EXT)[.\s])", re.IGNORECASE)

_PROP_WORDS = frozenset(
    {
        "bottle",
        "briefcase",
        "camera",
        "candle",
        "chair",
        "cigarette",
        "cup",
        "glass",
        "gun",
        "key",
        "knife",
        "lamp",
        "laptop",
        "mirror",
        "newspaper",
        "phone",
        "plate",
        "rope",
        "table",
        "umbrella",
        "vase",
        "watch",
    }
)
"""A deliberately tiny vocabulary. The real brain reads; this one recognises."""

_DESTRUCTIVE = frozenset(
    {
        "threw",
        "throws",
        "throw",
        "smash",
        "smashes",
        "smashed",
        "shatter",
        "shatters",
        "shattered",
        "break",
        "breaks",
        "broke",
        "burn",
        "burns",
        "burned",
        "tears",
        "tore",
        "crushes",
        "crushed",
        "fires",
        "fired",
    }
)
"""Verbs that mean the object does not survive the take."""


class ScriptedBrain:
    """Rule-based stand-in for the real brain. Satisfies ``AgentBrain``."""

    _anchor: Money

    def __init__(self, *, anchor: Money | None = None) -> None:
        self._anchor = anchor or Money(amount=1000)

    async def extract_props(self, source: ScriptSource) -> list[PropDraft]:
        """Spot known nouns in action lines and quote the line they came from.

        A keyword scan against a fixed vocabulary — nothing like what the real
        brain will do. It exists so the pipeline has something honest to run on,
        and so the *shape* of a correct answer is pinned: every prop carries the
        line it was found in, and a prop hit by a destructive verb comes back
        ``consumable``.
        """
        found: dict[str, list[SceneMention]] = {}
        destroyed: set[str] = set()
        scene = "1"
        scene_counter = 0

        for raw in source.text_content.splitlines():
            line = raw.strip()
            if not line:
                continue

            heading = _SCENE_HEADING.match(line)
            if heading is not None:
                scene_counter += 1
                scene = heading.group(1) or str(scene_counter)
                continue

            lowered = line.lower()
            is_destructive = any(verb in lowered for verb in _DESTRUCTIVE)
            for word in _PROP_WORDS:
                if re.search(rf"\b{word}s?\b", lowered):
                    found.setdefault(word, []).append(
                        SceneMention(scene_number=scene, line=line)
                    )
                    if is_destructive:
                        destroyed.add(word)

        return [
            PropDraft(
                name=word,
                category="prop",
                qty=1,
                mentions=mentions,
                consumable=word in destroyed,
                confidence=0.4,
                notes=(
                    "Destroyed on camera — needs one per take."
                    if word in destroyed
                    else ""
                ),
            )
            for word, mentions in sorted(found.items())
        ]

    async def research_item(self, brief: ItemBrief) -> ItemResearch:
        """A band bracketing the anchor, with a placeholder source."""
        return ItemResearch(
            reference_band=ReferenceBand(
                low=self._anchor.scaled_by(Decimal("0.8")),
                high=self._anchor.scaled_by(Decimal("1.3")),
                source_urls=["https://example.invalid/scripted-reference"],
                confidence=0.4,
                notes="Scripted band. Not real market data.",
            ),
            supplier_candidates=[
                SupplierCandidate(
                    name=f"Scripted Supplier for {brief.name}",
                    email="supplier@example.invalid",
                    confidence=0.4,
                )
            ],
        )

    async def extract_quote(self, message: InboundMessage) -> QuoteExtraction:
        """Read the first ringgit figure, or refuse.

        Attachments always escalate. That mirrors the persona who buries the
        price in a PDF, and encodes the rule that a covering note is not a quote.
        """
        if message.has_attachments:
            return QuoteExtraction(
                needs_human=True,
                escalation_reason=EscalationReason.PRICE_IN_ATTACHMENT,
                confidence=0.9,
                notes=f"Price is likely inside {message.attachment_filenames}.",
            )

        match = _PRICE.search(message.body)
        if match is None:
            return QuoteExtraction(
                needs_human=True,
                escalation_reason=EscalationReason.UNPARSEABLE_REPLY,
                confidence=0.8,
                notes="No ringgit figure found in the reply.",
            )

        raw = (match.group(1) or match.group(2)).replace(",", "")
        unit = Money.from_major(Decimal(raw))
        return QuoteExtraction(
            quote=ExtractedQuote(unit_price=unit, qty=1, total=unit),
            confidence=0.7,
        )

    async def next_move(self, ctx: NegotiationContext) -> NextMove:
        """A fixed ladder: open, counter toward the floor, accept, or escalate."""
        if not ctx.history:
            return NextMove(
                action=MoveAction.SEND_OPENING,
                reasoning=(
                    f"No contact yet with {ctx.supplier.name}. Opening with the "
                    f"requirement for {ctx.item.qty}x {ctx.item.name}."
                ),
                draft_subject=f"Quote request: {ctx.item.name}",
                draft_body=(
                    f"Hi {ctx.supplier.name},\n\n"
                    f"We are sourcing {ctx.item.qty}x {ctx.item.name} for a shoot. "
                    f"Could you send your best price and lead time?\n\nThanks."
                ),
                suggest_next_check_in_sim_hours=24.0,
                confidence=0.6,
            )

        quote = ctx.latest_quote
        if quote is None:
            silence = ctx.sim_hours_since_last_outbound
            if silence is not None and silence >= _SILENCE_HOURS:
                return NextMove(
                    action=MoveAction.CHASE,
                    reasoning=(
                        f"No reply in {silence:.0f} simulated hours. Sending one "
                        f"nudge before treating this supplier as unresponsive."
                    ),
                    draft_subject=f"Following up: {ctx.item.name}",
                    draft_body=(
                        f"Hi {ctx.supplier.name},\n\nJust following up on my note "
                        f"about {ctx.item.name}. Any indication of price would help.\n\n"
                        f"Thanks."
                    ),
                    suggest_next_check_in_sim_hours=24.0,
                    confidence=0.5,
                )
            return NextMove(
                action=MoveAction.WAIT,
                reasoning="Waiting for a first reply inside the expected window.",
                suggest_next_check_in_sim_hours=12.0,
                confidence=0.5,
            )

        if ctx.floor_price is not None and quote.unit_price <= ctx.floor_price:
            return NextMove(
                action=MoveAction.ACCEPT,
                reasoning=(
                    f"{quote.unit_price} is at or below the producer's floor of "
                    f"{ctx.floor_price}. Handing back for approval — the agent "
                    f"does not buy."
                ),
                escalation_reason=EscalationReason.GOOD_QUOTE,
                confidence=0.8,
            )

        if ctx.rounds_remaining <= 0:
            return NextMove(
                action=MoveAction.ESCALATE,
                reasoning=(
                    f"Used all {ctx.max_rounds} rounds. Best offer is "
                    f"{quote.unit_price}. A person should decide whether to take "
                    f"it or walk."
                ),
                escalation_reason=EscalationReason.ROUNDS_EXHAUSTED,
                confidence=0.7,
            )

        target = (
            ctx.floor_price
            if ctx.floor_price is not None
            else quote.unit_price.scaled_by(Decimal("0.85"))
        )
        inbound = sum(1 for m in ctx.history if m.direction is MessageDirection.INBOUND)
        return NextMove(
            action=MoveAction.COUNTER,
            reasoning=(
                f"Quoted {quote.unit_price} after {inbound} reply(s). Countering at "
                f"{target} with {ctx.rounds_remaining} round(s) left."
            ),
            draft_subject=f"Re: {ctx.item.name}",
            draft_body=(
                f"Thanks for coming back to us. {quote.unit_price} is above what "
                f"we have budgeted. Could you do {target}?"
            ),
            target_price=target,
            suggest_next_check_in_sim_hours=24.0,
            confidence=0.6,
        )
