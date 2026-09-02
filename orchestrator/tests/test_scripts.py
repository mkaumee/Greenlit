"""Turning an upload into text a brain can read.

The case that matters is the scanned PDF. `extract_text()` on a page with no
text layer returns an empty string rather than raising, so the naive version of
this hands the brain "" and gets back a confident list of no props — which on
screen is indistinguishable from a screenplay that genuinely needs nothing
bought. A producer would conclude the agent had read their script and found it
needed no props, and there is nothing anywhere that would contradict them.
"""

import base64
from io import BytesIO

import pytest
from orchestrator.scripts import UnreadableScriptError, decode_upload
from pypdf import PdfWriter

SCREENPLAY = "INT. KOPITIAM - NIGHT\n\nRazak throws the cup at the mirror.\n"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _blank_pdf() -> bytes:
    """A one-page PDF with no text layer — a scan, as far as anything can tell.

    pypdf opens it happily and extracts an empty string rather than raising,
    which is exactly the shape of the failure being guarded against.
    """
    writer = PdfWriter()
    _ = writer.add_blank_page(width=612, height=792)
    buffer = BytesIO()
    _ = writer.write(buffer)
    return buffer.getvalue()


def _pdf_with_text(line: str) -> bytes:
    """A minimal PDF carrying one line of real text.

    Written by hand rather than with a PDF library, because pypdf writes
    documents and does not draw text into them, and pulling in a rendering
    library to prove twenty lines of extraction would be the larger cost. This
    is the smallest file with a genuine text object in it.
    """
    body = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 12 Tf 72 720 Td (" + line.encode("ascii") + b") Tj ET"
    body.append(
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(body, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"

    xref = len(out)
    out += b"xref\n0 " + str(len(body) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(body) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def test_a_pdf_with_a_text_layer_is_read() -> None:
    """The success path, and the reason PDF is accepted at all: screenplays in
    the real world are PDFs."""
    text = decode_upload(
        filename="script.pdf",
        mime_type="application/pdf",
        content_b64=_b64(_pdf_with_text("Razak throws the cup at the mirror.")),
    )

    assert "cup" in text
    assert "mirror" in text


def test_plain_text_comes_back_unchanged() -> None:
    text = decode_upload(
        filename="script.txt",
        mime_type="text/plain",
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
        mime_type="application/xml",
        content_b64=_b64(fdx.encode()),
    )

    assert "glass" in text


def test_a_scanned_pdf_is_named_as_a_scan() -> None:
    """The failure this module exists for. Not an error the producer caused,
    and not one they can fix by trying again — so the message says what to do
    instead."""
    with pytest.raises(UnreadableScriptError) as caught:
        _ = decode_upload(
            filename="scan.pdf",
            mime_type="application/pdf",
            content_b64=_b64(_blank_pdf()),
        )

    detail = str(caught.value)
    assert "scan.pdf" in detail
    assert "scan" in detail.lower()
    assert "pasting the text" in detail


def test_something_that_is_not_a_pdf_at_all() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(
            filename="script.pdf",
            mime_type="application/pdf",
            content_b64=_b64(b"this is not a pdf"),
        )


def test_a_pdf_is_recognised_by_extension_when_the_mime_type_lies() -> None:
    """Browsers report application/octet-stream for a file dragged from some
    file managers. Decoding those bytes as UTF-8 would raise a message about
    text encoding, which is true and useless."""
    with pytest.raises(UnreadableScriptError) as caught:
        _ = decode_upload(
            filename="script.pdf",
            mime_type="application/octet-stream",
            content_b64=_b64(_blank_pdf()),
        )

    assert "scan" in str(caught.value).lower()


def test_a_word_document_says_which_formats_work() -> None:
    """.docx is a zip, so it fails the UTF-8 decode. The message names what to
    do rather than reporting a decode error."""
    with pytest.raises(UnreadableScriptError) as caught:
        _ = decode_upload(
            filename="script.docx",
            mime_type="application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document",
            content_b64=_b64(b"PK\x03\x04\xff\xfe\x00binary"),
        )

    assert "PDF" in str(caught.value)


def test_an_empty_file_is_not_a_screenplay_with_no_props() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(filename="empty.txt", mime_type="text/plain", content_b64="")


def test_whitespace_is_not_content() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(
            filename="blank.txt",
            mime_type="text/plain",
            content_b64=_b64(b"   \n\n  \t\n"),
        )


def test_a_corrupt_upload_is_reported_rather_than_crashing() -> None:
    with pytest.raises(UnreadableScriptError):
        _ = decode_upload(
            filename="script.txt", mime_type="text/plain", content_b64="not base64!!"
        )
