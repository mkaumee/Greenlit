"""Tests for the A/B boundary itself.

These are not tests of business logic. They prove that the contract refuses
malformed data, so that a mistake on either side shows up on the next
end-to-end run rather than during a demo.
"""

from datetime import UTC, datetime

import pytest
from cinema_contracts import (
    AgentBrain,
    CurrencyMismatchError,
    EscalationReason,
    ExtractedQuote,
    InboundMessage,
    ItemBrief,
    MessageDirection,
    MessageSummary,
    Money,
    MoveAction,
    NegotiationContext,
    NegotiationState,
    NextMove,
    PropDraft,
    QuoteExtraction,
    SceneMention,
    ScriptSource,
    SupplierCandidate,
)
from cinema_contracts.testing import ScriptedBrain
from pydantic import ValidationError

# Fixed simulation instants. Literal datetimes, never clock reads.
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
T1 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
T4 = datetime(2026, 3, 5, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Money
# --------------------------------------------------------------------------- #


def test_money_serialises_as_amount_and_currency_never_a_string() -> None:
    assert Money(amount=880).model_dump() == {"amount": 880, "currency": "MYR"}


def test_money_arithmetic_within_one_currency() -> None:
    assert Money(amount=880) + Money(amount=120) == Money(amount=1000)
    assert Money(amount=880) - Money(amount=80) == Money(amount=800)
    assert Money(amount=1000).scaled_by(1.6) == Money(amount=1600)


def test_mixing_currencies_raises_rather_than_guessing() -> None:
    with pytest.raises(CurrencyMismatchError):
        _ = Money(amount=880, currency="MYR") + Money(amount=100, currency="USD")
    with pytest.raises(CurrencyMismatchError):
        _ = Money(amount=880, currency="MYR") < Money(amount=100, currency="USD")


def test_from_major_rounds_half_up_at_the_boundary() -> None:
    assert Money.from_major("880.50") == Money(amount=881)
    assert Money.from_major("880.49") == Money(amount=880)
    assert Money.from_major(880) == Money(amount=880)


def test_money_is_frozen_and_hashable() -> None:
    m = Money(amount=880)
    with pytest.raises(ValidationError):
        m.amount = 900
    assert len({Money(amount=880), Money(amount=880)}) == 1


def test_negative_money_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _ = Money(amount=-1)


# --------------------------------------------------------------------------- #
# QuoteExtraction — the refusal-to-guess rules
# --------------------------------------------------------------------------- #


def test_extraction_cannot_be_quietly_empty() -> None:
    """No quote and no escalation would stall a negotiation with a blank screen."""
    with pytest.raises(ValidationError):
        _ = QuoteExtraction()


def test_escalation_must_say_why() -> None:
    with pytest.raises(ValidationError):
        _ = QuoteExtraction(needs_human=True)


def test_valid_escalation_and_valid_quote_both_accepted() -> None:
    esc = QuoteExtraction(
        needs_human=True, escalation_reason=EscalationReason.PRICE_IN_ATTACHMENT
    )
    assert esc.quote is None

    price = Money(amount=880)
    ok = QuoteExtraction(quote=ExtractedQuote(unit_price=price, total=price))
    assert ok.quote is not None
    assert ok.quote.unit_price == price


# --------------------------------------------------------------------------- #
# NextMove — the rules that keep the agent honest
# --------------------------------------------------------------------------- #


def test_a_move_that_sends_mail_must_carry_a_body() -> None:
    for action in (MoveAction.SEND_OPENING, MoveAction.COUNTER, MoveAction.CHASE):
        with pytest.raises(ValidationError):
            _ = NextMove(action=action, reasoning="x", target_price=Money(amount=1))


def test_counter_must_name_a_target_price() -> None:
    with pytest.raises(ValidationError):
        _ = NextMove(
            action=MoveAction.COUNTER, reasoning="x", draft_body="please do less"
        )


def test_stopping_for_a_human_must_give_a_reason() -> None:
    for action in (MoveAction.ACCEPT, MoveAction.ESCALATE):
        with pytest.raises(ValidationError):
            _ = NextMove(action=action, reasoning="x")


def test_reasoning_is_mandatory_because_the_producer_reads_it() -> None:
    with pytest.raises(ValidationError):
        _ = NextMove(action=MoveAction.WAIT, reasoning="")


def test_there_is_no_move_that_buys_anything() -> None:
    """ACCEPT is the most permissive action the brain has, and it only escalates.

    If this ever fails because a new action was added, that addition needs a
    conversation, not a merge.
    """
    assert set(MoveAction) == {
        MoveAction.SEND_OPENING,
        MoveAction.COUNTER,
        MoveAction.ACCEPT,
        MoveAction.CHASE,
        MoveAction.WALK_AWAY,
        MoveAction.ESCALATE,
        MoveAction.WAIT,
    }
    assert not any("ORDER" in a or "BUY" in a or "PAY" in a for a in MoveAction)


def test_unknown_fields_are_rejected_on_both_sides() -> None:
    with pytest.raises(ValidationError):
        _ = NextMove(action=MoveAction.WAIT, reasoning="x", invented_field=1)  # pyright: ignore[reportCallIssue]


# --------------------------------------------------------------------------- #
# The protocol, exercised through the scripted brain
# --------------------------------------------------------------------------- #


def _ctx(
    *,
    state: NegotiationState = NegotiationState.AWAITING_REPLY,
    floor_price: Money | None = None,
    rounds_used: int = 0,
    max_rounds: int = 4,
    latest_quote: ExtractedQuote | None = None,
    history: list[MessageSummary] | None = None,
    now: datetime = T1,
    last_outbound_at: datetime | None = None,
) -> NegotiationContext:
    return NegotiationContext(
        negotiation_id="neg1",
        state=state,
        item=ItemBrief(item_id="i1", name="Arri SkyPanel S60", category="lighting"),
        supplier=SupplierCandidate(name="Ah Seng Rentals", email="s@example.invalid"),
        floor_price=floor_price,
        rounds_used=rounds_used,
        max_rounds=max_rounds,
        latest_quote=latest_quote,
        history=history if history is not None else [],
        now=now,
        last_outbound_at=last_outbound_at,
    )


def test_scripted_brain_satisfies_the_protocol() -> None:
    assert isinstance(ScriptedBrain(), AgentBrain)


async def test_attachment_escalates_instead_of_guessing() -> None:
    result = await ScriptedBrain().extract_quote(
        InboundMessage(
            message_id="m1",
            thread_id="t1",
            from_email="s@example.invalid",
            subject="Quote",
            body="Please see attached for our pricing.",
            received_at=T1,
            has_attachments=True,
            attachment_filenames=["quote.pdf"],
        )
    )
    assert result.needs_human
    assert result.escalation_reason is EscalationReason.PRICE_IN_ATTACHMENT
    assert result.quote is None


async def test_unreadable_reply_escalates() -> None:
    result = await ScriptedBrain().extract_quote(
        InboundMessage(
            message_id="m1",
            thread_id="t1",
            from_email="s@example.invalid",
            subject="Re: Quote",
            body="who is this?",
            received_at=T1,
        )
    )
    assert result.needs_human
    assert result.escalation_reason is EscalationReason.UNPARSEABLE_REPLY


async def test_a_readable_price_is_extracted_with_its_currency() -> None:
    result = await ScriptedBrain().extract_quote(
        InboundMessage(
            message_id="m1",
            thread_id="t1",
            from_email="s@example.invalid",
            subject="Re: Quote",
            body="Can do RM1,250 per day, 3 days lead time.",
            received_at=T1,
        )
    )
    assert result.quote is not None
    assert result.quote.unit_price == Money(amount=1250, currency="MYR")


async def test_first_move_on_an_empty_history_is_an_opening_email() -> None:
    move = await ScriptedBrain().next_move(_ctx())
    assert move.action is MoveAction.SEND_OPENING
    assert move.draft_body.strip()


async def test_a_quote_at_the_floor_hands_back_to_a_human_and_does_not_buy() -> None:
    price = Money(amount=800)
    move = await ScriptedBrain().next_move(
        _ctx(
            latest_quote=ExtractedQuote(unit_price=price, total=price),
            floor_price=Money(amount=900),
            history=[
                MessageSummary(
                    direction=MessageDirection.INBOUND, body="RM800", sim_sent_at=T1
                )
            ],
        )
    )
    assert move.action is MoveAction.ACCEPT
    assert move.escalation_reason is EscalationReason.GOOD_QUOTE


async def test_exhausted_rounds_escalate_rather_than_conceding() -> None:
    price = Money(amount=1500)
    move = await ScriptedBrain().next_move(
        _ctx(
            latest_quote=ExtractedQuote(unit_price=price, total=price),
            floor_price=Money(amount=900),
            rounds_used=4,
            max_rounds=4,
            history=[
                MessageSummary(
                    direction=MessageDirection.INBOUND, body="RM1500", sim_sent_at=T1
                )
            ],
        )
    )
    assert move.action is MoveAction.ESCALATE
    assert move.escalation_reason is EscalationReason.ROUNDS_EXHAUSTED


async def test_silence_past_the_window_produces_a_chase() -> None:
    move = await ScriptedBrain().next_move(
        _ctx(
            now=T4,
            last_outbound_at=T0,
            history=[
                MessageSummary(
                    direction=MessageDirection.OUTBOUND, body="hi", sim_sent_at=T0
                )
            ],
        )
    )
    assert move.action is MoveAction.CHASE


# --------------------------------------------------------------------------- #
# Reading a screenplay for objects
# --------------------------------------------------------------------------- #

SCRIPT = """INT. BAR - NIGHT

The MAN grabbed the cup and threw it towards the mirror.

EXT. STREET - DAY

She lights a cigarette and checks her watch.
"""


async def _props() -> list[PropDraft]:
    return await ScriptedBrain().extract_props(
        ScriptSource(
            filename="script.fountain", mime_type="text/plain", text_content=SCRIPT
        )
    )


async def test_props_are_found_in_the_prose() -> None:
    assert {p.name for p in await _props()} == {"cup", "mirror", "cigarette", "watch"}


async def test_every_prop_quotes_the_line_it_came_from() -> None:
    """The receipt. A producer audits the list instead of trusting it."""
    for prop in await _props():
        assert prop.mentions, f"{prop.name} has no scene mention"
        assert prop.mentions[0].line.strip()


async def test_props_are_attributed_to_the_right_scene() -> None:
    by_name = {p.name: p for p in await _props()}
    assert by_name["mirror"].mentions[0].scene_number == "1"
    assert by_name["watch"].mentions[0].scene_number == "2"


async def test_a_prop_that_gets_destroyed_is_flagged_consumable() -> None:
    """The mirror gets smashed, so one is not enough for a day of takes."""
    by_name = {p.name: p for p in await _props()}
    assert by_name["mirror"].consumable
    assert not by_name["watch"].consumable


async def test_a_mention_cannot_have_an_empty_line() -> None:
    """No line, no prop — the cheapest check against an invented one."""
    with pytest.raises(ValidationError):
        _ = SceneMention(scene_number="1", line="")


async def test_a_script_with_no_props_returns_an_empty_list() -> None:
    drafts = await ScriptedBrain().extract_props(
        ScriptSource(
            filename="s.txt",
            mime_type="text/plain",
            text_content="INT. VOID - NIGHT\n\nShe thinks about her childhood.\n",
        )
    )
    assert drafts == []
