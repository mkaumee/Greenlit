# The fake below mirrors googleapiclient's own untyped resource shapes, so
# dict[str, Any] is the honest annotation rather than a shortcut.
# The unused userId/format parameters are deliberate: the fake must accept the
# same call signature the real client is invoked with, or the tests would not
# be exercising the calls we actually make.
# pyright: reportExplicitAny=false, reportAny=false, reportUnusedParameter=false
"""Gmail transport tests, against a faked API resource.

No network and no credentials, so these run anywhere — which matters because
the live round-trip needs two mailboxes and a consent screen that do not exist
yet. Everything the transport actually decides (headers, threading, ordering,
attachment detection, marking read) is decided here.
"""

import base64
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Any, final

import pytest
from orchestrator.gmail import (
    FileTokenStore,
    GmailTransport,
    SecretManagerTokenStore,
    token_store_for,
)
from orchestrator.settings import Settings, TokenBackend

SETTINGS = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    agent_email="agent@cinema.test",
    agent_display_name="Agentic Cinema",
)


# --------------------------------------------------------------------------- #
# A stand-in for googleapiclient's resource object
# --------------------------------------------------------------------------- #


@final
class _Executable:
    _result: dict[str, Any]

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self) -> dict[str, Any]:
        return self._result


@final
class _Messages:
    _fake: FakeGmail

    def __init__(self, fake: FakeGmail) -> None:
        self._fake = fake

    def send(self, *, userId: str, body: dict[str, Any]) -> _Executable:  # noqa: N803
        self._fake.sent.append(body)
        index = len(self._fake.sent)
        return _Executable(
            {
                "id": f"api-{index:012x}",
                "threadId": body.get("threadId") or f"thread-{index}",
            }
        )

    def list(self, *, userId: str, q: str) -> _Executable:  # noqa: N803
        self._fake.queries.append(q)
        return _Executable({"messages": [{"id": m["id"]} for m in self._fake.inbox]})

    def get(self, *, userId: str, id: str, format: str) -> _Executable:  # noqa: A002, N803
        for message in self._fake.inbox:
            if message["id"] == id:
                return _Executable(message)
        raise AssertionError(f"no such message {id}")

    def modify(self, *, userId: str, id: str, body: dict[str, Any]) -> _Executable:  # noqa: A002, N803
        self._fake.modified.append((id, body))
        return _Executable({})


@final
class _Users:
    _messages: _Messages

    def __init__(self, fake: FakeGmail) -> None:
        self._messages = _Messages(fake)

    def messages(self) -> _Messages:
        return self._messages


@final
class FakeGmail:
    """Records what the transport asked the API to do."""

    sent: list[dict[str, Any]]
    inbox: list[dict[str, Any]]
    modified: list[tuple[str, dict[str, Any]]]
    queries: list[str]
    _users: _Users

    def __init__(self) -> None:
        self.sent = []
        self.inbox = []
        self.modified = []
        self.queries = []
        self._users = _Users(self)

    def users(self) -> _Users:
        return self._users


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _sent_mime(fake: FakeGmail, index: int = -1) -> Message:
    raw = fake.sent[index]["raw"]
    return message_from_bytes(base64.urlsafe_b64decode(raw.encode()))


def _inbound(
    *,
    message_id: str = "in-1",
    thread_id: str = "t-1",
    headers: list[dict[str, str]] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_headers = headers or [
        {"name": "From", "value": "seller@example.test"},
        {"name": "Subject", "value": "Re: quote"},
        {"name": "Message-ID", "value": "<abc@example.test>"},
    ]
    return {
        "id": message_id,
        "threadId": thread_id,
        "payload": payload
        or {
            "mimeType": "text/plain",
            "headers": base_headers,
            "body": {"data": _b64("RM1,250 per day.")},
        },
    }


def _transport() -> tuple[GmailTransport, FakeGmail]:
    fake = FakeGmail()
    return GmailTransport(fake, SETTINGS), fake


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #


async def test_send_mints_its_own_message_id_header() -> None:
    """So we know the thread id without fetching the sent message back."""
    transport, fake = _transport()

    sent = await transport.send(to="seller@example.test", subject="Hi", body="Hello")

    mime = _sent_mime(fake)
    assert mime["Message-ID"] == sent.rfc822_message_id
    assert sent.rfc822_message_id.startswith("<")
    assert sent.rfc822_message_id.endswith(">")
    assert "cinema.test" in sent.rfc822_message_id


async def test_the_api_id_and_the_header_id_are_different_things() -> None:
    transport, _ = _transport()

    sent = await transport.send(to="seller@example.test", subject="Hi", body="Hello")

    assert sent.message_id.startswith("api-")
    assert sent.message_id != sent.rfc822_message_id


async def test_a_first_message_starts_a_thread() -> None:
    transport, fake = _transport()

    sent = await transport.send(to="seller@example.test", subject="Hi", body="Hello")

    assert "threadId" not in fake.sent[0]
    assert sent.thread_id == "thread-1"
    mime = _sent_mime(fake)
    assert mime["In-Reply-To"] is None
    assert mime["References"] is None


async def test_a_reply_carries_the_threading_headers() -> None:
    transport, fake = _transport()

    sent = await transport.send(
        to="seller@example.test",
        subject="Re: quote",
        body="Could you do RM900?",
        thread_id="thread-99",
        in_reply_to="<root@cinema.test>",
        references="<root@cinema.test> <prev@example.test>",
    )

    assert fake.sent[0]["threadId"] == "thread-99"
    assert sent.thread_id == "thread-99"
    mime = _sent_mime(fake)
    assert mime["In-Reply-To"] == "<root@cinema.test>"
    assert mime["References"] == "<root@cinema.test> <prev@example.test>"


async def test_the_body_survives_encoding_intact() -> None:
    """Role B sends what the brain wrote, byte for byte."""
    transport, fake = _transport()
    body = "Hi Ah Seng,\n\nCould you do RM900 for the mirror?\n\nThanks."

    _ = await transport.send(to="s@example.test", subject="Quote", body=body)

    mime = _sent_mime(fake)
    payload = mime.get_payload(decode=True)
    assert isinstance(payload, bytes)
    assert payload.decode().strip() == body.strip()


async def test_the_from_header_carries_the_display_name() -> None:
    transport, fake = _transport()

    _ = await transport.send(to="s@example.test", subject="Quote", body="hi")

    assert _sent_mime(fake)["From"] == "Agentic Cinema <agent@cinema.test>"


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #


async def test_poll_reads_the_message_and_its_headers() -> None:
    transport, fake = _transport()
    fake.inbox.append(_inbound())

    received = await transport.poll()

    assert len(received) == 1
    message = received[0]
    assert message.message_id == "in-1"
    assert message.thread_id == "t-1"
    assert message.from_email == "seller@example.test"
    assert message.rfc822_message_id == "<abc@example.test>"
    assert message.body == "RM1,250 per day."


async def test_poll_clears_unread_so_the_next_tick_does_not_re_read_it() -> None:
    transport, fake = _transport()
    fake.inbox.append(_inbound())

    _ = await transport.poll()

    assert fake.modified == [("in-1", {"removeLabelIds": ["UNREAD"]})]


async def test_poll_excludes_our_own_sent_mail() -> None:
    transport, fake = _transport()

    _ = await transport.poll()

    assert "-from:me" in fake.queries[0]


async def test_poll_returns_oldest_first() -> None:
    """Gmail lists newest first; filing them that way inverts the timeline."""
    transport, fake = _transport()
    fake.inbox.extend(
        [
            _inbound(message_id="newer"),
            _inbound(message_id="older"),
        ]
    )

    received = await transport.poll()

    assert [m.message_id for m in received] == ["older", "newer"]


async def test_an_attachment_is_reported_so_the_brain_can_escalate() -> None:
    """The PDF-quote case. The brain must know there was one to refuse to guess."""
    transport, fake = _transport()
    fake.inbox.append(
        _inbound(
            payload={
                "mimeType": "multipart/mixed",
                "headers": [{"name": "From", "value": "s@example.test"}],
                "body": {},
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _b64("Please see attached.")},
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "quote.pdf",
                        "body": {"attachmentId": "att-1"},
                    },
                ],
            }
        )
    )

    message = (await transport.poll())[0]

    assert message.has_attachments
    assert message.attachment_filenames == ["quote.pdf"]
    assert message.body == "Please see attached."


async def test_plain_text_is_preferred_over_html() -> None:
    transport, fake = _transport()
    fake.inbox.append(
        _inbound(
            payload={
                "mimeType": "multipart/alternative",
                "headers": [{"name": "From", "value": "s@example.test"}],
                "body": {},
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("RM900 flat.")}},
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>RM900 flat.</p>")},
                    },
                ],
            }
        )
    )

    assert (await transport.poll())[0].body == "RM900 flat."


async def test_html_only_mail_still_yields_a_body() -> None:
    """A quote inside clumsy HTML beats no quote at all."""
    transport, fake = _transport()
    fake.inbox.append(
        _inbound(
            payload={
                "mimeType": "multipart/alternative",
                "headers": [{"name": "From", "value": "s@example.test"}],
                "body": {},
                "parts": [
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>RM900 flat.</p>")},
                    }
                ],
            }
        )
    )

    assert "RM900" in (await transport.poll())[0].body


async def test_headers_are_matched_case_insensitively() -> None:
    """Gmail is inconsistent about casing and a miss would blank the sender."""
    transport, fake = _transport()
    fake.inbox.append(
        _inbound(
            headers=[
                {"name": "from", "value": "s@example.test"},
                {"name": "MESSAGE-ID", "value": "<x@example.test>"},
            ]
        )
    )

    message = (await transport.poll())[0]

    assert message.from_email == "s@example.test"
    assert message.rfc822_message_id == "<x@example.test>"


async def test_an_empty_mailbox_is_not_an_error() -> None:
    transport, _ = _transport()
    assert await transport.poll() == []


# --------------------------------------------------------------------------- #
# Where the refresh token lives
# --------------------------------------------------------------------------- #


def test_a_stored_token_round_trips(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "tok.json")
    store.write("1//refresh-me")
    assert store.read() == "1//refresh-me"


def test_a_token_file_is_not_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    FileTokenStore(path).write("1//refresh-me")
    assert path.stat().st_mode & 0o077 == 0, (
        "credentials must not be group/world readable"
    )


def test_a_missing_token_says_how_to_get_one(tmp_path: Path) -> None:
    """This is the first error a new machine hits. It should be actionable."""
    with pytest.raises(FileNotFoundError) as caught:
        _ = FileTokenStore(tmp_path / "nope.json").read()
    assert "oauth_bootstrap" in str(caught.value)


def test_the_default_backend_touches_no_cloud_client() -> None:
    """There is no GCP project yet; picking the wrong store would fail at boot."""
    assert isinstance(token_store_for(SETTINGS), FileTokenStore)


def test_the_cloud_backend_is_selected_by_configuration_alone() -> None:
    cloud = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        token_backend=TokenBackend.SECRET_MANAGER,
    )
    assert isinstance(token_store_for(cloud), SecretManagerTokenStore)
