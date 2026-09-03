"""A screenplay PDF goes as a document, never as text in the prompt.

The exclusion is the one thing in this path that would be silently expensive to
get wrong. `BreakdownParser` dumps the whole `ScriptSource` as the prompt, so
leaving `content_b64` in that dump would hand the model a megabyte of base64 as
prose. Nothing would raise. The model would produce *something*, the props
would look thin or invented, and the cause would be invisible — while the bill
counted every one of those tokens.

No network: the ADK runtime is replaced with one that records what it was
handed.
"""

import base64
import json
from typing import final

from cinema_contracts import ScriptSource

from main_agent.breakdown.parser import BreakdownParser

PDF = b"%PDF-1.4 pretend this is a screenplay"


@final
class _Recorder:
    """Stands in for AdkAgentRuntime, and answers with an empty prop list."""

    payload: str
    attachment: tuple[bytes, str] | None

    def __init__(self) -> None:
        self.payload = ""
        self.attachment = None

    async def run_json(
        self, payload: str, *, attachment: tuple[bytes, str] | None = None
    ) -> str:
        self.payload = payload
        self.attachment = attachment
        return "[]"


def _parser() -> tuple[BreakdownParser, _Recorder]:
    parser = BreakdownParser(model="gemini-3.7-flash")
    recorder = _Recorder()
    # The runtime is private because nothing but the parser should drive it;
    # a test standing in for the network is the exception that proves it.
    parser._runtime = recorder  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return parser, recorder


async def test_a_pdf_is_attached_as_a_document() -> None:
    parser, recorder = _parser()
    source = ScriptSource(
        filename="nightfall.pdf",
        mime_type="application/pdf",
        content_b64=base64.b64encode(PDF).decode(),
    )

    _ = await parser.parse(source)

    assert recorder.attachment == (PDF, "application/pdf")


async def test_the_base64_never_reaches_the_prompt_text() -> None:
    """The expensive mistake. Asserted on the payload, not on the call."""
    parser, recorder = _parser()
    encoded = base64.b64encode(PDF).decode()
    source = ScriptSource(
        filename="nightfall.pdf",
        mime_type="application/pdf",
        content_b64=encoded,
    )

    _ = await parser.parse(source)

    assert encoded not in recorder.payload
    assert "content_b64" not in json.loads(recorder.payload)
    # Everything else still travels, so the model knows what it is looking at.
    assert json.loads(recorder.payload)["filename"] == "nightfall.pdf"


async def test_a_text_screenplay_is_sent_as_text_with_no_attachment() -> None:
    parser, recorder = _parser()
    source = ScriptSource(
        filename="script.txt",
        mime_type="text/plain",
        text_content="INT. KOPITIAM - NIGHT\n\nHe throws the cup.\n",
    )

    _ = await parser.parse(source)

    assert recorder.attachment is None
    assert "throws the cup" in recorder.payload


async def test_a_document_with_no_mime_type_is_assumed_to_be_a_pdf() -> None:
    """Some file managers report nothing useful, and Gemini needs a mime type
    to treat the bytes as a document at all."""
    parser, recorder = _parser()
    source = ScriptSource(
        filename="script.pdf",
        mime_type="",
        content_b64=base64.b64encode(PDF).decode(),
    )

    _ = await parser.parse(source)

    assert recorder.attachment is not None
    assert recorder.attachment[1] == "application/pdf"
