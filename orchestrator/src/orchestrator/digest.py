"""What the agent knows about a production, gathered for a person to ask about.

Role B assembles the facts; Role A phrases them. Same split as the other four
contract methods — a fully-formed context goes in, prose comes back, and the
reasoning side never touches Firestore. That is not ceremony here: it is what
stops a model that is asked "how are we doing" from being able to read a
project it was not handed.

## Why a digest and not the raw records

Three reasons, and the second is the one that matters.

The records are large and mostly irrelevant to a producer's question — RFC-822
header ids, Gmail thread ids, claim timestamps. None of it helps anyone answer
"what needs me today", and all of it costs tokens.

**A digest is a boundary.** Whatever is not in it cannot be talked about. The
briefing that comes back is checked against the ids the digest carried, so a
model that invents a supplier or an item is caught by comparison rather than
trusted. Handing over the raw collection would make that check meaningless.

And it is the same data the panel already shows, so the chat and the screens
cannot disagree about what is true — they are reading one set of facts.
"""

from dataclasses import dataclass, field

from cinema_contracts import Money, NegotiationState

from orchestrator.records import ItemRecord, ItemStatus, NegotiationRecord

WAITING_STATES = frozenset({NegotiationState.READY_FOR_HUMAN})
"""Where the agent has stopped and cannot continue without a person.

One state, deliberately. ``READY_FOR_HUMAN`` is the stop condition and the only
thing that should ever be counted as "needs you" — a queue that also collects
merely-interesting rows trains people to ignore it.
"""


@dataclass(frozen=True, slots=True)
class ItemLine:
    """One prop, as a producer would describe it."""

    item_id: str
    name: str
    status: str
    qty: int
    best_quote: Money | None
    reference_low: Money | None
    reference_high: Money | None
    script_line: str
    """The line it was found in. The receipt, and the reason a producer can
    check the agent rather than trust it."""


@dataclass(frozen=True, slots=True)
class NegotiationLine:
    negotiation_id: str
    item_id: str
    item_name: str
    supplier: str
    state: str
    rounds_used: int
    max_rounds: int
    first_quote: Money | None
    latest_quote: Money | None
    reasoning: str
    """The brain's own last explanation, carried forward so a briefing can say
    why rather than only what."""
    waiting_on_human: bool
    escalation_reason: str


@dataclass(frozen=True, slots=True)
class ProjectDigest:
    """Everything a briefing may draw on, and nothing else."""

    project_id: str
    title: str
    items: list[ItemLine] = field(default_factory=list)
    negotiations: list[NegotiationLine] = field(default_factory=list)

    @property
    def waiting_count(self) -> int:
        """How many *props* need a person, not how many negotiations do.

        The agent writes to several suppliers about the same item, so three
        negotiations can sit at ``READY_FOR_HUMAN`` for one cup. A purchase
        order is created keyed by the item, so approving any one of them
        settles all three — meaning the producer has one decision to make, not
        three. Counting negotiations would tell them eight when the panel,
        which groups the same way, says four.
        """
        return len({n.item_id for n in self.negotiations if n.waiting_on_human})

    def known_ids(self) -> frozenset[str]:
        """Every id a briefing is allowed to mention.

        A model that cites an item or negotiation absent from here has invented
        it, and the caller rejects the reference rather than rendering a link to
        nothing. Cheap to check, and the alternative is a producer clicking
        through to a prop that does not exist.
        """
        return frozenset(
            [i.item_id for i in self.items]
            + [n.negotiation_id for n in self.negotiations]
        )


def build_digest(
    project_id: str,
    title: str,
    items: dict[str, ItemRecord],
    negotiations: dict[str, NegotiationRecord],
    supplier_names: dict[str, str],
) -> ProjectDigest:
    """Flatten the stored records into the facts a person would ask about.

    Pure — takes what a caller already read and returns a value. The repository
    calls belong to the handler so this stays testable without an emulator, and
    so the same digest can be built from a tick's in-memory records later
    without a second round trip.
    """
    lines: list[ItemLine] = []
    for item_id, item in sorted(items.items()):
        if item.status is ItemStatus.ABANDONED:
            # Dropped by the producer. Keeping it in the breakdown is right —
            # the script asked for it — but a briefing that mentions it is
            # answering about work nobody wants done.
            continue
        band = item.reference_band
        lines.append(
            ItemLine(
                item_id=item_id,
                name=item.name,
                status=item.status.value,
                qty=item.qty,
                best_quote=item.chosen_quote.unit_price if item.chosen_quote else None,
                reference_low=band.low if band else None,
                reference_high=band.high if band else None,
                script_line=item.mentions[0].line if item.mentions else "",
            )
        )

    talks: list[NegotiationLine] = []
    for negotiation_id, record in sorted(negotiations.items()):
        item = items.get(record.item_id)
        talks.append(
            NegotiationLine(
                negotiation_id=negotiation_id,
                item_id=record.item_id,
                item_name=item.name if item else record.item_id,
                supplier=supplier_names.get(record.supplier_id, record.supplier_id),
                state=record.state.value,
                rounds_used=record.rounds_used,
                max_rounds=record.max_rounds,
                first_quote=(
                    record.first_quote.unit_price if record.first_quote else None
                ),
                latest_quote=(
                    record.latest_quote.unit_price if record.latest_quote else None
                ),
                reasoning=record.latest_reasoning,
                waiting_on_human=record.state in WAITING_STATES,
                escalation_reason=record.escalation_reason,
            )
        )

    return ProjectDigest(
        project_id=project_id, title=title, items=lines, negotiations=talks
    )
