"""Public Role A implementation of the shared ``AgentBrain`` contract.

``GeminiAgentBrain`` is a facade, not a prompt monolith.  It is the one object
Role B depends on, while five focused child modules own their ADK agent setup and
domain-specific instructions.  This keeps the A/B boundary small and permits a
capability to gain tools or stricter output handling without exposing ADK to the
orchestrator.
"""

from typing import final, override

from cinema_contracts import (
    AgentBrain,
    InboundMessage,
    ItemBrief,
    ItemResearch,
    NegotiationContext,
    NextMove,
    ProducerBriefing,
    ProducerQuestion,
    PropDraft,
    QuoteExtraction,
    ScriptSource,
)

from main_agent.breakdown import BreakdownParser
from main_agent.briefing import ProducerReporter
from main_agent.negotiation import NegotiationDecider
from main_agent.quote import QuoteExtractor
from main_agent.research import ItemResearcher


@final
class GeminiAgentBrain(AgentBrain):
    """Stateless Google ADK implementation of ``AgentBrain``.

    Construct one instance when a service process starts and reuse its immutable
    ADK agent definitions.  The child runtimes still create a fresh ADK session
    for each method call, so this object never becomes an alternative store for
    negotiation state; Firestore data supplied by Role B remains authoritative.

    ``model`` is required rather than read from ambient configuration.  The
    application wiring chooses the deployed Gemini model explicitly and tests
    can substitute a model without changing domain code.
    """

    def __init__(self, *, model: str) -> None:
        self._breakdown_parser = BreakdownParser(model=model)
        self._item_researcher = ItemResearcher(model=model)
        self._quote_extractor = QuoteExtractor(model=model)
        self._negotiation_decider = NegotiationDecider(model=model)
        self._producer_reporter = ProducerReporter(model=model)

    @override
    async def extract_props(self, source: ScriptSource) -> list[PropDraft]:
        """Extract auditable prop drafts from an uploaded screenplay."""
        return await self._breakdown_parser.parse(source)

    @override
    async def research_item(self, brief: ItemBrief) -> ItemResearch:
        """Research a reference price band and candidate suppliers."""
        return await self._item_researcher.research(brief)

    @override
    async def extract_quote(self, message: InboundMessage) -> QuoteExtraction:
        """Extract a supplier quote or explicitly ask for human review."""
        return await self._quote_extractor.extract(message)

    @override
    async def next_move(self, ctx: NegotiationContext) -> NextMove:
        """Recommend the next negotiation action and any required email."""
        return await self._negotiation_decider.decide(ctx)

    @override
    async def brief_producer(self, question: ProducerQuestion) -> ProducerBriefing:
        """Answer a producer about their production, from the supplied digest."""
        return await self._producer_reporter.brief(question)
