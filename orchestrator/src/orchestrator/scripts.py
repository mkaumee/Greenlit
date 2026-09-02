"""Turning whatever a producer dropped on the page into text a brain can read.

``ScriptSource.text_content`` is what the breakdown parser actually reads, so
everything has to become text before it reaches Role A. Screenplays in the real
world are PDFs, which is why this exists rather than the upload accepting only
what is already text.

Deliberately narrow. It extracts a text layer; it does not OCR, render, or
follow anything embedded. A PDF with no text layer is a scan or an image
export, and that is reported as what it is — the alternative is handing the
brain an empty string and getting back a confident list of no props, which
reads exactly like a screenplay that needed nothing.
"""

import base64
import binascii
from io import BytesIO

PDF_MIME = "application/pdf"

# .fdx is Final Draft's XML and .fountain is plain text with markup. Both are
# readable as-is: the brain is being asked to find objects in prose, and a few
# stray tags cost far less than a parser per format would.
TEXT_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/xml",
        "text/xml",
        "application/octet-stream",
    }
)


class UnreadableScriptError(ValueError):
    """The upload could not be turned into text. The message is for a person."""


def decode_upload(*, filename: str, mime_type: str, content_b64: str) -> str:
    """Base64 in, screenplay text out.

    Base64 rather than multipart because the whole producer-facing API is JSON
    and one encoding is worth more than the bytes it costs.
    """
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UnreadableScriptError(
            "That upload was not valid base64, so nothing could be read from it."
        ) from exc

    if not raw:
        raise UnreadableScriptError(f"{filename or 'That file'} is empty.")

    if mime_type == PDF_MIME or filename.lower().endswith(".pdf"):
        return _from_pdf(raw, filename)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnreadableScriptError(
            f"{filename or 'That file'} is not text this can read. "
            "Plain text, Fountain, Final Draft (.fdx) and PDF all work; "
            "a .doc or .docx has to be exported first."
        ) from exc

    if not text.strip():
        raise UnreadableScriptError(f"{filename or 'That file'} has no text in it.")
    return text


def _from_pdf(raw: bytes, filename: str) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, OSError) as exc:
        raise UnreadableScriptError(
            f"{filename or 'That PDF'} could not be opened. If it is password "
            "protected, remove the password and upload it again."
        ) from exc

    text = "\n".join(pages).strip()
    if not text:
        # The failure worth naming. Handing an empty string to the brain would
        # come back as a confident list of no props, indistinguishable from a
        # screenplay that genuinely needs nothing bought.
        raise UnreadableScriptError(
            f"{filename or 'That PDF'} has no text in it — it is almost "
            "certainly a scan or an image export. This reads a PDF's text "
            "layer and does not run OCR, so it needs a PDF that was written "
            "rather than photographed. Exporting again from the original, or "
            "pasting the text, both work."
        )
    return text
