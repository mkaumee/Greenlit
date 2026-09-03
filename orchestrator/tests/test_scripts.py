"""Deciding what an upload is, without reading it.

This module used to extract text from PDFs, and the tests here used to prove it
handled a scan. Both are gone: a PDF now travels to Gemini as an attachment, so
the layout a screenplay depends on survives and a scanned script works — which
the extractor could never have managed, having no text layer to find.

What is left is a routing decision and two refusals. The refusals are the part
worth testing: an upload that cannot be sent on has to say so, because the
alternative is an empty screenplay reaching the brain and coming back as a
confident list of no props.
"""

import base64

import pytest
from orchestrator.scripts import (
    MAX_BYTES,
    UnreadableScriptError,
    check_document,
    decode_upload,
    is_document,
)

SCREENPLAY = "INT. KOPITIAM - NIGHT\n\nRazak throws the cup at the mirror.\n"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_plain_text_comes_back_unchanged() -> None:
    text = decode_upload(
        filename="script.txt",
        content_b64=_b64(SCREENPLAY.encode()),
    )

    assert text == SCREENPLAY


def test_final_draft_xml_is_passed_through_as_text() -> None:
    """.fdx is XML and .fountain is marked-up text. Neither gets a parser: the
    brain is being asked to find objects in prose, and a few stray tags cost
    far less than a format-specific reader would."""
    fdx = (
        '<?xml version="1.0"?><FinalDraft><Text>He drops the glass.</Text></FinalDraft>'
    )

    text = decode_upload(
        filename="script.fdx",
        content_b64=_b64(fdx.encode()),
    )

    assert "glass" in text


def test_a_pdf_is_a_document_not_text() -> None:
    assert is_document(filename="script.pdf", mime_type="application/pdf")
    assert not is_document(filename="script.txt", mime_type="text/plain")


def test_a_pdf_is_recognised_by_extension_when_the_mime_type_lies() -> None:
    """Browsers report application/octet-stream for a file dragged out of some
    file managers. Treating those bytes as text would produce a message about
    UTF-8 that is true and useless."""
    assert is_document(filename="script.pdf", mime_type="application/octet-stream")


def test_a_document_is_checked_without_being_read() -> None:
    """The whole point of not extracting: what is *in* the file is the model's
    business. Only whether it is real base64 and whether it will fit."""
    check_document(filename="script.pdf", content_b64=_b64(b"%PDF-1.4 not really"))


def test_a_file_too_large_to_send_says_so() -> None:
    """Cloud Run caps a request at 32 MB and base64 inflates by a third, so
    past this a file arrives as a truncated request rather than as an error
    anybody can read."""
    with pytest.raises(UnreadableScriptError) as caught:
        check_document(filename="huge.pdf", content_b64=_b64(b"x" * (MAX_BYTES + 1)))

    assert "huge.pdf" in str(caught.value)
    assert "limit" in str(caught.value)


def test_a_word_document_says_which_formats_work() -> None:
    """.docx is a zip, so it fails the UTF-8 decode. The message names what to
    do rather than reporting a decode error."""
    with pytest.raises(UnreadableScriptError) as caught:
        _ = decode_upload(
            filename="script.docx",
            content_b64=_b64(b"PK\x03\x04\xff\xfe\x00binary"),
        )

    assert "PDF" in str(caught.value)


def test_an_empty_file_is_not_a_screenplay_with_no_props() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(filename="empty.txt", content_b64="")


def test_whitespace_is_not_content() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(
            filename="blank.txt",
            content_b64=_b64(b"   \n\n  \t\n"),
        )


def test_a_corrupt_upload_is_reported_rather_than_crashing() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(filename="script.txt", content_b64="not base64!!")
