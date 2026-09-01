"""What the agent says it is doing, and whether it matches the screen.

Pure functions, no emulator. The rule under test is that a *prop* is one
decision however many suppliers quoted for it — because ``purchase_orders`` is
created keyed by the item, so approving any one quote settles the rest. Get
this wrong and the chat answers "8 decisions waiting" beside a panel that says
4, about the same production, at the same moment. Nothing would throw; the
producer would simply stop trusting both.
"""

from datetime import UTC, datetime

from cinema_contracts import ExtractedQuote, Money, NegotiationState, SceneMention
from orchestrator.briefing import summarise
from orchestrator.digest import build_digest
from orchestrator.records import ItemRecord, ItemStatus, NegotiationRecord

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _item(name: str) -> ItemRecord:
    return ItemRecord(
        name=name,
        category="prop",
        status=ItemStatus.SOURCING,
        mentions=[SceneMention(scene_number="12", line=f"He threw the {name}")],
    )


def _quote(amount: int) -> ExtractedQuote:
    return ExtractedQuote(
        unit_price=Money(amount=amount, currency="MYR"),
        total=Money(amount=amount, currency="MYR"),
    )


def _negotiation(
    item_id: str,
    supplier_id: str,
    amount: int | None,
    state: NegotiationState = NegotiationState.READY_FOR_HUMAN,
) -> NegotiationRecord:
    return NegotiationRecord(
        item_id=item_id,
        supplier_id=supplier_id,
        state=state,
        rounds_used=4,
        latest_quote=_quote(amount) if amount is not None else None,
        escalation_reason="GOOD_QUOTE",
        created_at=T0,
        updated_at=T0,
    )


def _digest(
    items: dict[str, ItemRecord],
    negotiations: dict[str, NegotiationRecord],
    suppliers: dict[str, str] | None = None,
):
    return build_digest(
        "p1", "Kopitiam", items, negotiations, suppliers or {"s1": "Ah Seng"}
    )


def test_two_quotes_for_one_prop_are_one_decision() -> None:
    digest = _digest(
        {"cup": _item("Cup")},
        {
            "n1": _negotiation("cup", "s1", 1439),
            "n2": _negotiation("cup", "s2", 719),
        },
        {"s1": "Skyline", "s2": "Ah Seng"},
    )

    assert digest.waiting_count == 1

    text, refs = summarise(digest, "what needs me?")

    assert "1 decision(s) waiting on you" in text
    # The cheapest leads, and it is the only one offered as a decision.
    assert "Ah Seng at MYR 719" in text
    assert len(refs) == 1
    assert refs[0][1] == "n2"


def test_the_dearer_quotes_are_still_mentioned() -> None:
    """A producer should know somebody else answered — just not be asked twice."""
    digest = _digest(
        {"cup": _item("Cup")},
        {"n1": _negotiation("cup", "s1", 1439), "n2": _negotiation("cup", "s2", 719)},
    )

    text, _ = summarise(digest, "what needs me?")

    assert "1 dearer quote(s)" in text


def test_a_supplier_who_named_no_price_does_not_win_on_price() -> None:
    """An absent quote must not sort as cheapest by being absent."""
    digest = _digest(
        {"cup": _item("Cup")},
        {
            "silent": _negotiation("cup", "s1", None),
            "priced": _negotiation("cup", "s2", 900),
        },
    )

    _, refs = summarise(digest, "what needs me?")

    assert refs[0][1] == "priced"


def test_props_are_counted_not_negotiations() -> None:
    """The number the rail shows, and the number this has to agree with."""
    digest = _digest(
        {"cup": _item("Cup"), "mirror": _item("Mirror")},
        {
            "a": _negotiation("cup", "s1", 719),
            "b": _negotiation("cup", "s2", 1439),
            "c": _negotiation("mirror", "s1", 719),
            "d": _negotiation("mirror", "s2", 1439),
        },
    )

    assert digest.waiting_count == 2
    assert "2 decision(s) waiting on you" in summarise(digest, "what needs me?")[0]


def test_only_ready_for_human_counts() -> None:
    """The stop condition, and nothing else. A queue that also collects
    merely-interesting rows trains people to ignore it."""
    digest = _digest(
        {"cup": _item("Cup")},
        {
            "a": _negotiation("cup", "s1", 719, NegotiationState.AWAITING_REPLY),
            "b": _negotiation("cup", "s2", 900, NegotiationState.DEAD),
        },
    )

    assert digest.waiting_count == 0
    assert "Nothing needs you right now." in summarise(digest, "what needs me?")[0]


def test_every_reference_is_one_the_digest_knows() -> None:
    """The check that makes a briefing verifiable rather than trusted."""
    digest = _digest(
        {"cup": _item("Cup")},
        {"n1": _negotiation("cup", "s1", 719)},
    )

    for question in ("what needs me?", "who is quiet?", "what are we spending?"):
        _, refs = summarise(digest, question)
        assert all(ref[1] in digest.known_ids() for ref in refs), question
