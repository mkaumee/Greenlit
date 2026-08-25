"""Extract a structured quote from one inbound supplier email."""

from typing import final

from cinema_contracts import InboundMessage, QuoteExtraction
from google.adk.agents import LlmAgent

from main_agent.gemini_schema import gemini_output_schema
from main_agent.runtime import AdkAgentRuntime

_INSTRUCTION = """You extract a supplier quote from one inbound email.
Never guess a price, quantity, currency, delivery scope, or term. Escalate with
the contract's appropriate reason when the reply is ambiguous, asks a question,
cannot be parsed, or puts its price in an attachment. Your response must satisfy
the configured output schema.
"""


@final
class QuoteExtractor:
    """Own the deliberately conservative supplier-email extraction agent.

    Keeping extraction separate makes its safety rule visible: an unreadable
    reply becomes a human escalation rather than context for a negotiating
    agent to reinterpret optimistically.
    """

    def __init__(self, *, model: str) -> None:
        # ADK 2.7 exposes a concrete class through ABCMeta; see parser.py.
        agent = LlmAgent(  # pyright: ignore[reportEmptyAbstractUsage]
            name="quote_extractor",
            description="Extract or safely refuse to extract a supplier quote.",
            model=model,
            instruction=_INSTRUCTION,
            output_schema=gemini_output_schema(QuoteExtraction),
            mode="chat",
        )
        self._runtime = AdkAgentRuntime(
            app_name="cinema_quote_extractor",
            agent=agent,
        )

    async def extract(self, message: InboundMessage) -> QuoteExtraction:
        """Return a validated quote or an explicit human escalation."""
        response = await self._runtime.run_json(message.model_dump_json())
        return QuoteExtraction.model_validate_json(response)
