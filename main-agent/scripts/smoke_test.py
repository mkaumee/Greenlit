import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime

from cinema_contracts import (
    BreakdownSource,
    InboundMessage,
    ItemBrief,
    MessageDirection,
    MessageSummary,
    Money,
    NegotiationContext,
    NegotiationState,
    SupplierCandidate,
)
from pydantic import BaseModel

from main_agent import GeminiAgentBrain

SIMULATION_TIME = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def print_result(title: str, value: BaseModel | Sequence[BaseModel]) -> None:
    """Print a readable test result."""
    print(f"\n{'=' * 20} {title} {'=' * 20}")

    if isinstance(value, BaseModel):
        print(value.model_dump_json(indent=2))
        return

    for item in value:
        print(item.model_dump_json(indent=2))


async def main() -> None:
    model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
    brain = GeminiAgentBrain(model=model)

    print(f"Using model: {model}")

    breakdown = BreakdownSource(
        filename="smoke-test-breakdown.txt",
        mime_type="text/plain",
        text_content="""
Scene 12 - Hotel ballroom, night

Required equipment:
- 2 x Aputure 600D lights
- 1 x haze machine
- 4 x wireless lavalier microphones

Props:
- 1 x vintage leather suitcase
""".strip(),
    )

    drafts = await brain.parse_breakdown(breakdown)
    print_result("parse_breakdown", drafts)

    item = ItemBrief(
        item_id="smoke-test-aputure-600d",
        name="Aputure 600D",
        category="lighting",
        scenes=["Scene 12"],
        qty=2,
        notes="Needed for one night shoot in Kuala Lumpur.",
        currency="MYR",
    )

    research = await brain.research_item(item)
    print_result("research_item", research)

    inbound_body = """
Hello,

We can supply two Aputure 600D lights for MYR 880 per unit.
The total is MYR 1,760, including delivery in Kuala Lumpur.
Lead time is two days. Payment is due within 30 days.

Regards,
Demo Lighting Supplier
""".strip()

    inbound = InboundMessage(
        message_id="smoke-message-1",
        thread_id="smoke-thread-1",
        from_email="sales@example.invalid",
        subject="Quotation for two Aputure 600D lights",
        body=inbound_body,
        received_at=SIMULATION_TIME,
    )

    extraction = await brain.extract_quote(inbound)
    print_result("extract_quote", extraction)

    if extraction.quote is None:
        raise RuntimeError(
            "Quote extraction failed:\n", f"{extraction.model_dump_json(indent=2)}"
        )

    test_quote = extraction.quote

    context = NegotiationContext(
        negotiation_id="smoke-negotiation-1",
        state=NegotiationState.QUOTED,
        item=item,
        supplier=SupplierCandidate(
            name="Demo Lighting Supplier",
            email="sales@example.invalid",
            verified=True,
        ),
        floor_price=Money(amount=800, currency="MYR"),
        target_price=Money(amount=820, currency="MYR"),
        rounds_used=0,
        max_rounds=4,
        first_quote=test_quote,
        latest_quote=test_quote,
        history=[
            MessageSummary(
                direction=MessageDirection.INBOUND,
                body=inbound_body,
                sim_sent_at=SIMULATION_TIME,
                extracted_quote=test_quote,
            )
        ],
        now=SIMULATION_TIME,
        last_inbound_at=SIMULATION_TIME,
    )

    move = await brain.next_move(context)
    print_result("next_move", move)


if __name__ == "__main__":
    asyncio.run(main())
