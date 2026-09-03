"""Answer a producer asking about their own production."""

import json
from typing import cast, final

from cinema_contracts import ProducerBriefing, ProducerQuestion
from google.adk.agents import LlmAgent

from main_agent.gemini_schema import gemini_output_schema
from main_agent.runtime import AdkAgentRuntime

_INSTRUCTION = """You brief a film producer on their own production.

Everything you may say is in the supplied question object. It lists every prop
and every negotiation the producer is allowed to be told about. Do not mention
a supplier, prop, price or negotiation that is not in it, and do not estimate a
price nobody has quoted. Referencing an id that was not supplied gets the
reference discarded, so cite only ids you were given.

Saying you do not know is a good answer. "No supplier has quoted for the mirror
yet" is correct and useful; guessing what one might quote is neither, because
the producer cannot tell the two apart.

Counting is not your job — the caller already has the numbers. Yours is
judgement: whether a price is worth pushing on, whose silence is worth chasing,
what to do first, and why. Be brief and concrete, and name the thing you are
talking about.

Never suggest that anything has been bought or could be bought without the
producer. Nothing is purchased until they approve it.

Your response must satisfy the configured output schema.
"""


@final
class ProducerReporter:
    """Own the ADK agent that talks to a producer.

    Separate from the other four capabilities for the same reason they are
    separate from each other: this one has a different failure mode. The others
    act on their output; this one is read by a person, so a confident sentence
    about a supplier who does not exist is indistinguishable from a true one
    until somebody checks. The instruction above and the caller's reference
    check are both aimed at that.
    """

    def __init__(self, *, model: str) -> None:
        # ADK 2.7 marks this concrete Pydantic model with ABCMeta even though it
        # has no abstract members; basedpyright otherwise reports a false positive.
        agent = LlmAgent(  # pyright: ignore[reportEmptyAbstractUsage]
            name="producer_reporter",
            description="Answer a producer's question about their production.",
            model=model,
            instruction=_INSTRUCTION,
            output_schema=gemini_output_schema(ProducerBriefing),
            mode="chat",
        )
        self._runtime = AdkAgentRuntime(
            app_name="cinema_producer_reporter",
            agent=agent,
        )

    async def brief(self, question: ProducerQuestion) -> ProducerBriefing:
        """Return prose and the ids it pointed at. Nothing here causes a write."""
        response = await self._runtime.run_json(question.model_dump_json())
        decoded = cast(object, json.loads(response))
        return ProducerBriefing.model_validate(decoded)
