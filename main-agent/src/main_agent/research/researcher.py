"""Research defensible prices and potential suppliers for one item."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false

from typing import final

from cinema_contracts import ItemBrief, ItemResearch
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from parallel import (
    AsyncParallel,
    ParallelError,
)

from main_agent.gemini_schema import gemini_output_schema
from main_agent.runtime import AdkAgentRuntime

_INSTRUCTION = """You research one film-production procurement item.
Use search_web whenever current web information is needed. For every call, create
both a self-contained objective and two or three concise search queries of three
to six words each. Use different search angles when that improves coverage.
If the results are insufficient, contradictory, or outdated, call search_web
again with an improved objective or different queries. You may call it as many
times as needed.
Return a defensible reference price band and supplier candidates. Include source
URLs that actually support the prices. Supplier addresses are candidates only
and must remain unverified until Role B checks them. Never fabricate a source.
Your response must satisfy the configured output schema.
"""


async def search_web(
    objective: str,
    search_queries: list[str],
) -> dict[str, object]:
    """Search the web for information needed to achieve an objective.

    Args:
        objective: A self-contained description of the required information and
            why it is needed.
        search_queries: Two or three concise keyword queries, ideally three to
            six words each.

    Returns:
        Relevant results containing only titles, URLs, publication dates, and
        supporting excerpts. Call this tool again with a revised objective or
        different queries when the results are insufficient.
    """
    normalized_objective = objective.strip()
    normalized_queries = [query.strip() for query in search_queries if query.strip()]

    if not normalized_objective:
        return {"error": "objective must not be empty"}
    if not normalized_queries:
        return {"error": "search_queries must contain at least one query"}

    try:
        async with AsyncParallel() as client:
            response = await client.search(
                objective=normalized_objective,
                search_queries=normalized_queries,
                mode="basic",
                max_chars_total=12_000,
            )
    except ParallelError:
        return {
            "error": (
                "Web search failed. Retry with fewer or more focused search queries."
            )
        }

    results: list[dict[str, object]] = []
    for item in response.results:
        result: dict[str, object] = {
            "url": item.url,
            "excerpts": item.excerpts,
        }
        if item.title:
            result["title"] = item.title
        if item.publish_date:
            result["publish_date"] = item.publish_date
        results.append(result)

    return {"results": results}


_SEARCH_TOOL = FunctionTool(func=search_web)


@final
class ItemResearcher:
    """Own the ADK agent whose only concern is market and supplier research."""

    def __init__(self, *, model: str) -> None:
        # ADK 2.7 exposes a concrete class through ABCMeta; see parser.py.
        agent = LlmAgent(  # pyright: ignore[reportEmptyAbstractUsage]
            name="item_researcher",
            description="Research reference prices and supplier candidates.",
            model=model,
            instruction=_INSTRUCTION,
            tools=[_SEARCH_TOOL],
            output_schema=gemini_output_schema(ItemResearch),
            mode="chat",
        )
        self._runtime = AdkAgentRuntime(
            app_name="cinema_item_researcher",
            agent=agent,
        )

    async def research(self, brief: ItemBrief) -> ItemResearch:
        """Return research validated against the shared contracts model."""
        response = await self._runtime.run_json(brief.model_dump_json())
        return ItemResearch.model_validate_json(response)
