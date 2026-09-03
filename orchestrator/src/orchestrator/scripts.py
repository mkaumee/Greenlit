"""What a producer dropped on the page, on its way to the brain.

Deliberately thin, and thinner than it was. An earlier version flattened PDFs
to text with pypdf before Role A ever saw them, which threw away the thing a
screenplay most depends on: scene headings at the margin, character names
centred, dialogue indented. It also refused a scanned script, because there was
no text layer to extract — while the model it was feeding would have read the
page perfectly well.

So PDFs are not read here at all. They go to Gemini as an attachment and it
reads the document. What is left is decoding: text files become strings,
because that is what ``ScriptSource.text_content`` is for, and anything else
travels as bytes.

Decoding is not parsing. Nothing here inspects a screenplay.
"""

import base64
import binascii

PDF_MIME = "application/pdf"

MAX_BYTES = 15 * 1024 * 1024
"""Refused above this, decoded.

Cloud Run caps a request at 32 MB and base64 inflates by a third, so a file
much past this arrives as a truncated request rather than as an error anybody
can read. Feature screenplays are a few megabytes; ``ScriptSource.gcs_uri``
exists, unused, as the door for whatever is not.
"""


class UnreadableScriptError(ValueError):
    """The upload cannot be sent on. The message is written for a person."""


def is_document(*, filename: str, mime_type: str) -> bool:
    """True when this should travel as a file rather than as text.

    Extension as well as mime type, because a browser reports
    ``application/octet-stream`` for a file dragged out of some file managers,
    and decoding a PDF's bytes as UTF-8 produces a message about text encoding
    that is true and useless.
    """
    return mime_type == PDF_MIME or filename.lower().endswith(".pdf")


def decode_upload(*, filename: str, content_b64: str) -> str:
    """Base64 in, screenplay text out. Text formats only.

    No mime type: it decided which branch to take when this also read PDFs,
    and now :func:`is_document` makes that call before anything gets here. A
    PDF reaching this function is a bug in the caller, not a bad upload.
    """
    raw = _raw(filename, content_b64)

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


def check_document(*, filename: str, content_b64: str) -> None:
    """Refuse a file that cannot be sent on, without decoding what it says.

    The only questions worth asking about a document here are whether it is
    real base64 and whether it will fit. What is *in* it is the model's
    business — that is the whole point of not extracting it.
    """
    _ = _raw(filename, content_b64)


def _raw(filename: str, content_b64: str) -> bytes:
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UnreadableScriptError(
            "That upload was not valid base64, so nothing could be read from it."
        ) from exc

    if not raw:
        raise UnreadableScriptError(f"{filename or 'That file'} is empty.")
    if len(raw) > MAX_BYTES:
        raise UnreadableScriptError(
            f"{filename or 'That file'} is {len(raw) // (1024 * 1024)} MB, "
            f"over the {MAX_BYTES // (1024 * 1024)} MB limit. A feature "
            "screenplay is normally a few megabytes — if this one is not, it "
            "is probably scanned at a much higher resolution than reading it "
            "needs."
        )
    return raw
