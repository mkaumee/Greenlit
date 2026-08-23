"""The tick loop. One pass: advance the clock, read the mail, act on what is due.

Called every minute by Cloud Scheduler in live mode, and in a tight loop by
``/demo/run`` for judge mode. Same code path either way.

## Why this is written the way it is

Cloud Run reaps instances whenever it likes, and a tick that is killed halfway
must leave the system in a state the next tick can pick up. Three things buy
that:

**Nothing is held in memory across the loop.** Each negotiation is read,
decided and written before the next one is touched. Kill the process after the
third of eight and the first three are done, the other five are still due, and
the next tick simply finds them.

**Filing a reply is idempotent.** Inbound messages are keyed by Gmail message
ID. ``append_message`` reports whether the document was new, and the state
transition only fires when it was — otherwise a redelivered message would burn
a negotiation round for a reply the supplier sent once.

**The clock is derived, not incremented.** A missed tick loses no simulated
time, so there is nothing to catch up on and nothing to replay.

## What this module refuses to do

It never composes email text. The brain writes the body; this sends what the
brain wrote. And it never applies a human event — approving a purchase order
goes through a separate authenticated endpoint, because the whole point is that
the tick loop cannot get there.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cinema_contracts import (
    AgentBrain,
    InboundMessage,
    ItemBrief,
    MessageDirection,
    MessageSummary,
    MoveAction,
    NegotiationContext,
    NegotiationState,
    QuoteExtraction,
    SupplierCandidate,
)

from orchestrator.clock import SimClock
from orchestrator.mail import MailTransport, RawInbound
from orchestrator.records import MessageRecord, NegotiationRecord
from orchestrator.repository import DueNegotiation, FirestoreRepository
from orchestrator.sourcing import SourcingLoop
from orchestrator.state_machine import (
    NegotiationEvent,
    allowed_events,
    apply_event,
    event_for_move,
    is_terminal,
)

MIN_CHECK_HOURS = 1.0
MAX_CHECK_HOURS = 72.0
DEFAULT_CHECK_HOURS = 12.0
"""Bounds on how far ahead a negotiation may be rescheduled.

The brain suggests a delay; this module decides. An unclamped suggestion of
"check back in 900 hours" would park a negotiation past the end of the
production, and a suggestion of zero would spin the loop.
"""

SILENCE_HOURS = 48.0
"""Simulated hours of supplier silence before the loop raises SILENCE_TIMEOUT."""

CLAIM_LEASE_HOURS = 0.25
"""How far ahead claiming a row parks it before anyone may retry.

Not a correctness knob. Two overlapping ticks are separated by the
compare-and-swap in ``FirestoreRepository._claim``, which holds however long
this is. What the lease decides is how long a row sits idle when the tick that
won it is then killed mid-work — fifteen simulated minutes, which in live mode
is fifteen real ones.
"""


def _references(record: NegotiationRecord) -> str:
    """The ``References`` header: thread root first, then the latest message.

    Deliberately not the full chain. A five-day negotiation would accumulate a
    header that grows every round, and root-plus-last threads correctly in
    every client that matters.
    """
    ids = [record.thread_root_rfc822_id, record.last_rfc822_id]
    seen = [i for n, i in enumerate(ids) if i and i not in ids[:n]]
    return " ".join(seen)


def _require(value: datetime | None) -> datetime:
    """Narrow an optional timestamp that the caller has already checked."""
    if value is None:
        raise AssertionError("timestamp was checked as present and is not")
    return value


@dataclass(slots=True)
class TickReport:
    """What one pass did. Returned to the caller and logged; never persisted."""

    sim_now: datetime
    items_examined: int = 0
    items_researched: int = 0
    negotiations_opened: int = 0
    replies_filed: int = 0
    replies_skipped: int = 0
    replies_after_stop: int = 0
    unmatched_replies: int = 0
    negotiations_examined: int = 0
    claims_lost: int = 0
    """Rows another tick was already working on. Zero unless ticks overlap, and
    a number that climbs is the signal that ticks are running long."""
    messages_sent: int = 0
    escalated: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def did_something(self) -> bool:
        return bool(
            self.replies_filed
            or self.messages_sent
            or self.escalated
            or self.items_researched
            or self.negotiations_opened
        )


class TickLoop:
    """One project's worth of work per pass.

    Holds references to its collaborators and nothing else — no negotiation
    state, no cursors, no counters. Hard Rule 3.
    """

    _repo: FirestoreRepository
    _clock: SimClock
    _brain: AgentBrain
    _mail: MailTransport
    _sourcing: SourcingLoop

    def __init__(
        self,
        repo: FirestoreRepository,
        clock: SimClock,
        brain: AgentBrain,
        mail: MailTransport,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._brain = brain
        self._mail = mail
        self._sourcing = SourcingLoop(repo, brain)

    async def run_tick(self, project_id: str, *, limit: int = 50) -> TickReport:
        now = await self._clock.advance(project_id)
        report = TickReport(sim_now=now)

        for raw in await self._mail.poll():
            await self._file_reply(raw, now, report)

        # Items first, so a negotiation opened by this pass gets its opening
        # email in the same pass rather than waiting a minute for the next one.
        sourcing = await self._sourcing.run(now, limit=limit)
        report.items_examined = sourcing.items_examined
        report.items_researched = sourcing.researched
        report.negotiations_opened = sourcing.negotiations_opened
        report.claims_lost += sourcing.claims_lost
        report.errors.extend(sourcing.errors)

        for due in await self._repo.due_negotiations(now, limit=limit):
            try:
                await self._advance_negotiation(due, now, report)
            except Exception as exc:
                # One negotiation must not take the rest of the project down
                # with it — over five days, a project that stops being ticked is
                # a negotiation that dies. The sourcing half already does this.
                #
                # Safe to swallow only because the row has been claimed by now:
                # it is parked for the lease rather than retried on every tick,
                # so a permanently broken negotiation cannot spin.
                report.errors.append(f"{due.negotiation_id}: {exc}")

        return report

    # ------------------------------------------------------------------ #
    # Inbound
    # ------------------------------------------------------------------ #

    async def _file_reply(
        self, raw: RawInbound, now: datetime, report: TickReport
    ) -> None:
        """Route one inbound message to its negotiation and read it."""
        target = await self._repo.find_by_thread(raw.thread_id)
        if target is None:
            # Someone emailed the agent outside any negotiation we started, or
            # a thread was deleted. Not an error, and deliberately not an
            # escalation — there is no negotiation to escalate.
            report.unmatched_replies += 1
            return

        record = target.record
        extraction = await self._brain.extract_quote(
            InboundMessage(
                message_id=raw.message_id,
                thread_id=raw.thread_id,
                from_email=raw.from_email,
                subject=raw.subject,
                body=raw.body,
                received_at=now,
                has_attachments=raw.has_attachments,
                attachment_filenames=raw.attachment_filenames,
            )
        )

        was_new = await self._repo.append_message(
            target.project_id,
            target.negotiation_id,
            raw.message_id,
            MessageRecord(
                direction=MessageDirection.INBOUND,
                body=raw.body,
                subject=raw.subject,
                sim_sent_at=now,
                gmail_message_id=raw.message_id,
                extracted_quote=extraction.quote,
                needs_human=extraction.needs_human,
            ),
        )
        if not was_new:
            # Already filed on an earlier tick. Applying the transition again
            # would spend a round on a reply the supplier sent once.
            report.replies_skipped += 1
            return

        event = (
            NegotiationEvent.REPLY_NEEDS_HUMAN
            if extraction.needs_human
            else NegotiationEvent.QUOTE_RECEIVED
        )

        if event in allowed_events(record.state):
            self._apply_extraction(record, extraction, now)
            report.replies_filed += 1
        else:
            # The negotiation has already stopped — waiting on a producer, or
            # finished. Suppliers do keep emailing after that, and the message
            # belongs in the timeline, but it must not restart anything.
            #
            # latest_quote is deliberately left alone. It is the number on the
            # approval screen, and moving it while a human is deciding would
            # change what they think they are approving.
            record.last_inbound_at = now
            record.updated_at = now
            report.replies_after_stop += 1

        await self._repo.save_negotiation(
            target.project_id, target.negotiation_id, record
        )

    def _apply_extraction(
        self, record: NegotiationRecord, extraction: QuoteExtraction, now: datetime
    ) -> None:
        """Fold a reply into the negotiation record. Pure bookkeeping."""
        record.last_inbound_at = now
        record.updated_at = now

        if extraction.needs_human:
            record.state = apply_event(record.state, NegotiationEvent.REPLY_NEEDS_HUMAN)
            record.escalation_reason = (
                extraction.escalation_reason.value
                if extraction.escalation_reason is not None
                else ""
            )
            record.latest_reasoning = extraction.notes
            record.next_action_due_at = None
            return

        record.state = apply_event(record.state, NegotiationEvent.QUOTE_RECEIVED)
        record.latest_quote = extraction.quote
        if record.first_quote is None:
            record.first_quote = extraction.quote
        # Due immediately: there is a new price on the table and the next tick
        # should decide what to do about it rather than waiting out a timer.
        record.next_action_due_at = now

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #

    async def _advance_negotiation(
        self, due: DueNegotiation, now: datetime, report: TickReport
    ) -> None:
        record = due.record
        report.negotiations_examined += 1

        # Before the claim, deliberately. A terminal negotiation returns here
        # without saving, and claiming writes next_action_due_at — which would
        # put a finished negotiation back into the very queue that
        # save_negotiation drops it from.
        if is_terminal(record.state):
            return

        if not await self._repo.claim_negotiation(
            due, now + timedelta(hours=CLAIM_LEASE_HOURS)
        ):
            # Another tick is already working on this row. Not an error, and
            # not worth retrying — that tick will finish it.
            report.claims_lost += 1
            return

        if self._awaiting_reply(record):
            silent_for = (
                now - _require(record.last_outbound_at)
            ).total_seconds() / 3600
            if silent_for >= SILENCE_HOURS:
                await self._raise_silence(due, now, report)
            else:
                # We have asked and they have not answered yet. There is nothing
                # to decide until they do, so do not ask the brain and do not
                # send anything — a second email before the first is answered
                # reads as pestering, and lets a brain that always has an
                # opinion talk over a supplier who has simply gone quiet.
                # Leaving the timer anchored to our last outbound is also what
                # makes the silence window reachable at all: rescheduling from
                # `now` each tick would push the deadline forward forever.
                record.next_action_due_at = _require(
                    record.last_outbound_at
                ) + timedelta(hours=SILENCE_HOURS)
                record.updated_at = now
                await self._repo.save_negotiation(
                    due.project_id, due.negotiation_id, record
                )
            return

        context = await self._build_context(due, now)
        if context is None:
            # Returning without saving leaves the claim's lease in place, which
            # is what we want: a negotiation whose item or supplier has gone
            # backs off instead of being re-examined every sixty seconds
            # forever.
            report.errors.append(
                f"{due.negotiation_id}: item or supplier missing; cannot decide"
            )
            return

        move = await self._brain.next_move(context)
        record.latest_reasoning = move.reasoning

        if move.action is MoveAction.WAIT:
            record.next_action_due_at = self._next_due(
                now, move.suggest_next_check_in_sim_hours
            )
            record.updated_at = now
            await self._repo.save_negotiation(
                due.project_id, due.negotiation_id, record
            )
            return

        if move.action in {
            MoveAction.SEND_OPENING,
            MoveAction.COUNTER,
            MoveAction.CHASE,
        }:
            supplier = await self._repo.get_supplier(due.project_id, record.supplier_id)
            if supplier is None:
                # Leased, as above.
                report.errors.append(f"{due.negotiation_id}: supplier record vanished")
                return

            # Threading is built from RFC-822 header ids, never from the
            # transport's own message id. See SentMessage in mail.py for why
            # confusing the two silently shreds the supplier's thread.
            sent = await self._mail.send(
                to=supplier.email,
                subject=move.draft_subject or f"Regarding {record.item_id}",
                body=move.draft_body,
                thread_id=record.gmail_thread_id,
                in_reply_to=record.last_rfc822_id,
                references=_references(record),
            )
            record.gmail_thread_id = sent.thread_id
            record.last_msg_id = sent.message_id
            if not record.thread_root_rfc822_id:
                record.thread_root_rfc822_id = sent.rfc822_message_id
            record.last_rfc822_id = sent.rfc822_message_id
            record.last_outbound_at = now
            report.messages_sent += 1

            _ = await self._repo.append_message(
                due.project_id,
                due.negotiation_id,
                sent.message_id,
                MessageRecord(
                    direction=MessageDirection.OUTBOUND,
                    body=move.draft_body,
                    subject=move.draft_subject,
                    sim_sent_at=now,
                    gmail_message_id=sent.message_id,
                ),
            )

            if move.action is MoveAction.COUNTER:
                record.rounds_used += 1
                record.target_price = move.target_price

        event = event_for_move(move.action)
        if event is not None:
            record.state = apply_event(record.state, event)

        # SENT exists only to mark "handed to Gmail, thread not yet recorded".
        # We have the thread ID in hand, so close that gap in the same write
        # rather than leaving a state the next tick would have to reconcile.
        if record.state is NegotiationState.SENT:
            record.state = apply_event(record.state, NegotiationEvent.SEND_CONFIRMED)

        if record.state is NegotiationState.READY_FOR_HUMAN:
            record.escalation_reason = (
                move.escalation_reason.value
                if move.escalation_reason is not None
                else ""
            )
            report.escalated += 1

        record.next_action_due_at = (
            None
            if is_terminal(record.state)
            or record.state is NegotiationState.READY_FOR_HUMAN
            else self._next_due(now, move.suggest_next_check_in_sim_hours)
        )
        record.updated_at = now
        await self._repo.save_negotiation(due.project_id, due.negotiation_id, record)

    async def _raise_silence(
        self, due: DueNegotiation, now: datetime, report: TickReport
    ) -> None:
        """The supplier has gone quiet past the window. Chase, or give up."""
        record = due.record
        record.state = apply_event(record.state, NegotiationEvent.SILENCE_TIMEOUT)
        record.updated_at = now
        record.next_action_due_at = None if is_terminal(record.state) else now
        await self._repo.save_negotiation(due.project_id, due.negotiation_id, record)
        report.negotiations_examined += 0  # already counted

    @staticmethod
    def _awaiting_reply(record: NegotiationRecord) -> bool:
        """True when the ball is in the supplier's court.

        That is: we sent something, and nothing has come back since. The state
        alone is not enough to tell — NEGOTIATING covers both "they just quoted,
        decide what to do" and "we countered, waiting".
        """
        waiting_states = {
            NegotiationState.SENT,
            NegotiationState.AWAITING_REPLY,
            NegotiationState.NEGOTIATING,
            NegotiationState.CHASING,
        }
        if record.state not in waiting_states or record.last_outbound_at is None:
            return False
        return (
            record.last_inbound_at is None
            or record.last_inbound_at <= record.last_outbound_at
        )

    # ------------------------------------------------------------------ #
    # Context assembly
    # ------------------------------------------------------------------ #

    async def _build_context(
        self, due: DueNegotiation, now: datetime
    ) -> NegotiationContext | None:
        """Rebuild everything the brain is allowed to know, from Firestore.

        Assembled fresh every tick rather than carried forward, which is what
        lets the brain stay memoryless and the loop stay killable.
        """
        record = due.record
        item = await self._repo.get_item(due.project_id, record.item_id)
        supplier = await self._repo.get_supplier(due.project_id, record.supplier_id)
        if item is None or supplier is None:
            return None

        messages = await self._repo.list_messages(due.project_id, due.negotiation_id)

        return NegotiationContext(
            negotiation_id=due.negotiation_id,
            state=record.state,
            item=ItemBrief(
                item_id=record.item_id,
                name=item.name,
                category=item.category,
                scenes=item.scenes,
                qty=item.qty,
                consumable=item.consumable,
                notes=item.notes,
                reference_band=item.reference_band,
            ),
            supplier=SupplierCandidate(
                name=supplier.name,
                email=supplier.email,
                source_url=supplier.source_url,
                confidence=supplier.confidence,
                verified=supplier.verified,
            ),
            floor_price=record.floor_price,
            target_price=record.target_price,
            rounds_used=record.rounds_used,
            max_rounds=record.max_rounds,
            first_quote=record.first_quote,
            latest_quote=record.latest_quote,
            history=[
                MessageSummary(
                    direction=MessageDirection(message.direction),
                    body=message.body,
                    sim_sent_at=message.sim_sent_at,
                    extracted_quote=message.extracted_quote,
                )
                for message in messages
            ],
            now=now,
            last_inbound_at=record.last_inbound_at,
            last_outbound_at=record.last_outbound_at,
        )

    @staticmethod
    def _next_due(now: datetime, suggested: float | None) -> datetime:
        """Clamp the brain's suggested delay into something sane."""
        hours = DEFAULT_CHECK_HOURS if suggested is None else suggested
        hours = max(MIN_CHECK_HOURS, min(MAX_CHECK_HOURS, hours))
        return now + timedelta(hours=hours)
