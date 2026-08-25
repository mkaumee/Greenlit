"""Small, stateless adapter around the Google ADK runtime.

An ``LlmAgent`` is configuration: model, instructions, tools, and output schema.
It is safe and useful to build that object once when a Cloud Run instance starts.
An ADK session is different: it contains conversation events and state.  The
project's source of truth is Firestore, reconstructed by Role B into a contracts
model for every call, so Role A must never depend on an ADK session surviving.

``AdkAgentRuntime`` therefore retains only the reusable agent definition.  It
creates a fresh in-memory session service and runner for each invocation.  The
in-memory service is merely request-local scratch space; losing it cannot change
the next decision or prevent a retry after an instance is reaped.
"""

from dataclasses import dataclass
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

_ROLE_B_USER_ID = "role-b"


class AdkResponseError(RuntimeError):
    """Raised when ADK finishes without a usable structured response."""


@dataclass(frozen=True, slots=True)
class AdkAgentRuntime:
    """Run one reusable ADK agent without retaining cross-request state."""

    app_name: str
    agent: LlmAgent

    async def run_json(self, payload: str) -> str:
        """Run the agent once and return its final JSON text.

        A unique session is deliberately created for every contracts call.
        The complete domain context is already present in ``payload``; reusing
        an ADK session would create a hidden second state store beside Firestore.
        """
        session_service = InMemorySessionService()
        session_id = uuid4().hex
        _ = await session_service.create_session(
            app_name=self.app_name,
            user_id=_ROLE_B_USER_ID,
            session_id=session_id,
        )
        runner = Runner(
            app_name=self.app_name,
            agent=self.agent,
            session_service=session_service,
        )
        message = types.Content(
            role="user",
            parts=[types.Part.from_text(text=payload)],
        )

        final_text: str | None = None
        async for event in runner.run_async(
            user_id=_ROLE_B_USER_ID,
            session_id=session_id,
            new_message=message,
        ):
            if not event.is_final_response() or event.content is None:
                continue
            text = "".join(
                part.text or "" for part in (event.content.parts or [])
            ).strip()
            if text:
                final_text = text

        if final_text is None:
            raise AdkResponseError(
                f"ADK agent {self.agent.name!r} returned no final text response."
            )
        return final_text
