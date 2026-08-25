"""Convert an uploaded screenplay into unpersisted prop drafts."""

import json
from typing import cast, final

from cinema_contracts import PropDraft, ScriptSource
from google.adk.agents import LlmAgent

from main_agent.gemini_schema import gemini_output_schema
from main_agent.runtime import AdkAgentRuntime

_INSTRUCTION = """You extract physical props from a screenplay.
Return only props explicitly supported by the supplied document. Do not invent
plausible items. Every prop must include at least one SceneMention containing
the scene and an exact supporting quote. Record quantity, whether the prop is
consumable or damaged, confidence, and useful notes.
Your response must satisfy the configured output schema.
"""


@final
class BreakdownParser:
    """Own the ADK agent specialized in shooting-breakdown extraction.

    Parsing is isolated from the other Role A capabilities so its prompt,
    schema, and future document tools can evolve without turning
    ``GeminiAgentBrain`` into a single large prompt router.
    """

    def __init__(self, *, model: str) -> None:
        # ADK 2.7 marks this concrete Pydantic model with ABCMeta even though it
        # has no abstract members; basedpyright otherwise reports a false positive.
        agent = LlmAgent(  # pyright: ignore[reportEmptyAbstractUsage]
            name="breakdown_parser",
            description="Extract auditable prop drafts from a screenplay.",
            model=model,
            instruction=_INSTRUCTION,
            output_schema=gemini_output_schema(list[PropDraft]),
            mode="chat",
        )
        self._runtime = AdkAgentRuntime(
            app_name="cinema_breakdown_parser",
            agent=agent,
        )

    async def parse(self, source: ScriptSource) -> list[PropDraft]:
        """Return validated prop drafts; persistence remains Role B's job."""
        response = await self._runtime.run_json(source.model_dump_json())
        decoded = cast(object, json.loads(response))
        if not isinstance(decoded, list):
            raise ValueError("Prop extraction response must be a JSON list.")
        items = cast(list[object], decoded)
        return [PropDraft.model_validate(item) for item in items]
