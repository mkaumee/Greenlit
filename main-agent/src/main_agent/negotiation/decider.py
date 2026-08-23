"""Choose the next safe move for one reconstructed negotiation context."""

from typing import final

from cinema_contracts import NegotiationContext, NextMove
from google.adk.agents import LlmAgent

from main_agent.gemini_schema import gemini_output_schema
from main_agent.runtime import AdkAgentRuntime

_INSTRUCTION = """You decide one next action in a procurement negotiation.
Use only the supplied context. Compose any required email, explain the decision
for the producer, respect the floor and round limit, and stop for a human at
ACCEPT or ESCALATE. You can never buy or create a purchase order. Your response
must satisfy the configured output schema.
"""


@final
class NegotiationDecider:
    """Own the ADK agent that recommends, but never executes, a next move.

    The complete conversation and simulated time arrive in
    ``NegotiationContext`` on every call.  This class intentionally keeps no
    negotiation history, ensuring a cold-started instance reaches the same kind
    of decision from the same persisted facts.
    """

    def __init__(self, *, model: str) -> None:
        # ADK 2.7 exposes a concrete class through ABCMeta; see parser.py.
        agent = LlmAgent(  # pyright: ignore[reportEmptyAbstractUsage]
            name="negotiation_decider",
            description="Recommend the next safe procurement negotiation move.",
            model=model,
            instruction=_INSTRUCTION,
            output_schema=gemini_output_schema(NextMove),
            mode="chat",
        )
        self._runtime = AdkAgentRuntime(
            app_name="cinema_negotiation_decider",
            agent=agent,
        )

    async def decide(self, ctx: NegotiationContext) -> NextMove:
        """Return a validated recommendation without performing side effects."""
        response = await self._runtime.run_json(ctx.model_dump_json())
        return NextMove.model_validate_json(response)
