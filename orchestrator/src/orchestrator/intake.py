"""Getting a production started: a title, a screenplay, and a list signed off.

Lifted out of ``app.py``'s route handlers so that both services can run it. The
tick service has had these routes since Phase 3, reachable only from a shell
with a gcloud token; ``cinema-api`` needs the same three steps reachable from a
producer's browser, with the caller's own uid as the owner.

Two copies of this would have been the obvious way and the wrong one. The
DRAFT→confirm gate below is where a hallucinated prop is caught before it turns
into an email to a real seller, and a guardrail implemented twice is a guardrail
that will eventually be implemented differently. There is one of it.

Nothing here is a FastAPI handler: these take what they need and return values,
so both apps can wrap them in their own auth and their own error shapes, and so
they can be tested without a client. They read no clock of their own — every
timestamp comes from ``clock.now()``, per Hard Rule 2.
"""

from dataclasses import dataclass, field
from datetime import datetime

from cinema_contracts import AgentBrain, Money, ScriptSource

from orchestrator.clock import SimClock, initial_state
from orchestrator.records import ItemRecord, ItemStatus, ProjectRecord
from orchestrator.repository import FirestoreRepository
from orchestrator.sourcing import item_id_for


@dataclass(frozen=True, slots=True)
class FoundProp:
    """One prop, as offered back to the producer for confirmation."""

    item_id: str
    name: str
    category: str
    qty: int
    consumable: bool
    confidence: float
    scenes: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    """The script lines it was found in. The receipt, and the reason a producer
    can audit the list rather than trust it."""


@dataclass(frozen=True, slots=True)
class Choice:
    """A producer's decision about one draft prop."""

    item_id: str
    qty: int = 1
    include: bool = True
    floor_price: Money | None = None


@dataclass(frozen=True, slots=True)
class Confirmed:
    confirmed: list[str] = field(default_factory=list)
    abandoned: list[str] = field(default_factory=list)


class UnknownItemError(LookupError):
    """A confirmation named an item this project does not have."""


async def create_project(
    repo: FirestoreRepository,
    clock: SimClock,
    *,
    project_id: str,
    title: str,
    owner_uid: str,
    sim_start: datetime | None = None,
    budget_baseline: Money | None = None,
) -> datetime:
    """Make the production. Returns where simulated time starts.

    Raises ``google.api_core.exceptions.AlreadyExists`` if the id is taken —
    ``create()`` rather than ``set()``, so a second production can never quietly
    overwrite the first. Callers turn that into whatever their surface needs.
    """
    real_now = clock.real_now()
    start = sim_start or real_now
    await repo.create_project(
        project_id,
        ProjectRecord(
            title=title,
            clock=initial_state(start, real_now),
            budget_baseline=budget_baseline,
            created_at=start,
            owner_uid=owner_uid,
        ),
    )
    return start


async def read_script(
    repo: FirestoreRepository,
    clock: SimClock,
    brain: AgentBrain,
    *,
    project_id: str,
    source: ScriptSource,
) -> list[FoundProp]:
    """Read a screenplay and offer back the physical things the scenes need.

    Persists what it finds as ``DRAFT`` items, which are inert: nothing is
    researched and nobody is emailed until a producer confirms the list. That
    gap is the point — it is where a hallucinated prop gets caught, before it
    turns into a real message to a real seller.
    """
    now = await clock.now(project_id)
    drafts = await brain.extract_props(source)

    found: list[FoundProp] = []
    for draft in drafts:
        item_id = item_id_for(draft.name)
        scenes = [m.scene_number for m in draft.mentions]
        await repo.save_item(
            project_id,
            item_id,
            ItemRecord(
                name=draft.name,
                category=draft.category,
                scenes=scenes,
                qty=draft.qty,
                notes=draft.notes,
                mentions=list(draft.mentions),
                consumable=draft.consumable,
                status=ItemStatus.DRAFT,
                updated_at=now,
            ),
        )
        found.append(
            FoundProp(
                item_id=item_id,
                name=draft.name,
                category=draft.category,
                qty=draft.qty,
                consumable=draft.consumable,
                confidence=draft.confidence,
                scenes=scenes,
                lines=[m.line for m in draft.mentions],
            )
        )
    return found


async def confirm_items(
    repo: FirestoreRepository,
    clock: SimClock,
    *,
    project_id: str,
    choices: list[Choice],
) -> Confirmed:
    """The producer signs off the list. Only now does anything start moving.

    Quantity is set here rather than by the agent because a consumable prop
    needs one per take, and only a person knows how many takes the schedule
    allows. Everything left out is abandoned rather than deleted, so the
    breakdown still shows what the script asked for and what was dropped.
    """
    now = await clock.now(project_id)
    confirmed: list[str] = []
    abandoned: list[str] = []

    for choice in choices:
        item = await repo.get_item(project_id, choice.item_id)
        if item is None:
            raise UnknownItemError(choice.item_id)

        item.updated_at = now
        if choice.include:
            item.qty = choice.qty
            item.floor_price = choice.floor_price
            item.status = ItemStatus.RESEARCHING
            # Due immediately: confirming is the producer saying go, and a
            # delay here would look like the agent ignoring them.
            item.next_action_due_at = now
            confirmed.append(choice.item_id)
        else:
            item.status = ItemStatus.ABANDONED
            item.next_action_due_at = None
            abandoned.append(choice.item_id)

        await repo.save_item(project_id, choice.item_id, item)

    return Confirmed(confirmed=confirmed, abandoned=abandoned)
