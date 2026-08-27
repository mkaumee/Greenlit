# The fake below mirrors googleapiclient's own untyped resource shapes, so
# dict[str, Any] is the honest annotation rather than a shortcut.
# The unused userId/format parameters are deliberate: the fake must accept the
# same call signature the real client is invoked with, or the tests would not
# be exercising the calls we actually make.
# pyright: reportExplicitAny=false, reportAny=false, reportUnusedParameter=false
# googleapiclient ships no stubs, and HttpError is imported here to build the
# 404 a deleted thread raises. Same suppression gmail.py carries.
# pyright: reportMissingTypeStubs=false
"""Gmail transport tests, against a faked API resource.

No network and no credentials, so these run anywhere — which matters because
the live round-trip needs two mailboxes and a consent screen that do not exist
yet. Everything the transport actually decides (headers, threading, ordering,
attachment detection, marking read) is decided here.
"""

import base64
import json
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Any, final

import httplib2
import pytest
from googleapiclient.errors import HttpError
from orchestrator.gmail import (
    FileTokenStore,
    GmailTransport,
    SecretManagerTokenStore,
    client_credentials,
    token_store_for,
)
from orchestrator.settings import Settings, TokenBackend

SETTINGS = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    agent_email="agent@cinema.test",
    agent_display_name="Greenlit",
)


# --------------------------------------------------------------------------- #
# A stand-in for googleapiclient's resource object
# --------------------------------------------------------------------------- #


def _not_found() -> HttpError:
    """What googleapiclient raises for a thread that is gone."""
    return HttpError(resp=httplib2.Response({"status": 404}), content=b"not found")


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

    def list(
        self,
        *,
        userId: str,  # noqa: N803
        q: str,
        maxResults: int = 100,  # noqa: N803
        includeSpamTrash: bool = False,  # noqa: N803
    ) -> _Executable:
        """Newest first, as Gmail returns them, and spam-blind by default.

        Both of those are Gmail's real behaviour and both have bitten us: the
        ordering because a reversed conversation reads as the wrong person
        speaking last, and the default because a filtered reply sits somewhere
        no listing we ran could see.
        """
        self._fake.queries.append(q)
        self._fake.spam_trash_included.append(includeSpamTrash)
        visible = [
            m
            for m in self._fake.inbox
            if includeSpamTrash or not {"SPAM", "TRASH"} & set(m.get("labelIds") or [])
        ]
        page = list(reversed(visible))[:maxResults]
        return _Executable({"messages": [{"id": m["id"]} for m in page]})

    def get(self, *, userId: str, id: str, format: str) -> _Executable:  # noqa: A002, N803
        for message in self._fake.inbox:
            if message["id"] == id:
                return _Executable(message)
        raise AssertionError(f"no such message {id}")

    def modify(self, *, userId: str, id: str, body: dict[str, Any]) -> _Executable:  # noqa: A002, N803
        self._fake.modified.append((id, body))
        return _Executable({})


@final
class _Threads:
    """Gmail's threads resource, which is what the loop actually reads.

    The mailbox is never listed for the loop's benefit: threads are fetched by
    id, so a busy inbox cannot page a supplier's reply out of view.
    """

    _fake: FakeGmail

    def __init__(self, fake: FakeGmail) -> None:
        self._fake = fake

    def get(self, *, userId: str, id: str, format: str) -> _Executable:  # noqa: A002, N803
        messages = [m for m in self._fake.inbox if m.get("threadId") == id]
        if not messages:
            raise _not_found()
        return _Executable({"id": id, "messages": messages})


@final
class _Users:
    _messages: _Messages
    _threads: _Threads

    def __init__(self, fake: FakeGmail) -> None:
        self._messages = _Messages(fake)
        self._threads = _Threads(fake)

    def messages(self) -> _Messages:
        return self._messages

    def threads(self) -> _Threads:
        return self._threads


@final
class FakeGmail:
    """Records what the transport asked the API to do."""

    sent: list[dict[str, Any]]
    inbox: list[dict[str, Any]]
    modified: list[tuple[str, dict[str, Any]]]
    queries: list[str]
    spam_trash_included: list[bool]
    _users: _Users

    def __init__(self) -> None:
        self.sent = []
        self.inbox = []
        self.modified = []
        self.queries = []
        self.spam_trash_included = []
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
    labels: list[str] | None = None,
) -> dict[str, Any]:
    base_headers = headers or [
        {"name": "From", "value": "seller@example.test"},
        {"name": "Subject", "value": "Re: quote"},
        {"name": "Message-ID", "value": "<abc@example.test>"},
    ]
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": ["INBOX", "UNREAD"] if labels is None else labels,
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

    assert _sent_mime(fake)["From"] == "Greenlit <agent@cinema.test>"


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

    _ = await transport.poll(threads=frozenset({"t-1"}))

    assert fake.modified == [("in-1", {"removeLabelIds": ["UNREAD"]})]


async def test_poll_excludes_our_own_sent_mail() -> None:
    transport, fake = _transport()

    _ = await transport.poll()

    assert "-from:me" in fake.queries[0]


async def test_poll_returns_oldest_first() -> None:
    """Gmail lists newest first; filing them that way inverts the timeline.

    ``inbox`` is in the order the mailbox received them and the fake hands them
    back newest first, exactly as Gmail does. Un-reversing that is the
    transport's job — drop it and this comes out backwards.
    """
    transport, fake = _transport()
    fake.inbox.extend(
        [
            _inbound(message_id="older"),
            _inbound(message_id="newer"),
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


# --------------------------------------------------------------------------- #
# Where the OAuth client id and secret come from
# --------------------------------------------------------------------------- #
#
# The bootstrap already consented with a client file, so requiring the same two
# strings again as environment variables was friction and a place to paste the
# wrong thing. The fallback has to be exact: a resolver that quietly returns
# the wrong credential fails at Google with invalid_client, which reads like a
# revoked token rather than a lookup that went to the wrong place.


def _client_file(tmp_path: Path, shape: str, ident: str) -> Path:
    path = tmp_path / "client_secret.json"
    _ = path.write_text(
        json.dumps({shape: {"client_id": ident, "client_secret": f"{ident}-secret"}})
    )
    return path


def test_explicit_settings_beat_the_client_file(tmp_path: Path) -> None:
    """Cloud Run has no client JSON, so configuration must always win."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        oauth_client_id="from-env",
        oauth_client_secret="from-env-secret",
        oauth_client_secrets=_client_file(tmp_path, "installed", "from-file"),
    )

    assert client_credentials(settings) == ("from-env", "from-env-secret")


def test_a_web_client_file_is_read(tmp_path: Path) -> None:
    """The shape the Cloud Shell consent flow requires — a Web application."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        oauth_client_secrets=_client_file(tmp_path, "web", "web-client"),
    )

    assert client_credentials(settings) == ("web-client", "web-client-secret")


def test_a_desktop_client_file_is_read(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        oauth_client_secrets=_client_file(tmp_path, "installed", "desktop"),
    )

    assert client_credentials(settings) == ("desktop", "desktop-secret")


def test_nothing_anywhere_returns_nothing(tmp_path: Path) -> None:
    """Not a partial credential. Callers keep their own refusal message."""
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        oauth_client_secrets=tmp_path / "absent.json",
    )

    assert client_credentials(settings) == ("", "")


# --------------------------------------------------------------------------- #
# Whose mail this is
# --------------------------------------------------------------------------- #
#
# Found the hard way on a real mailbox. poll() marked every unread message read
# before the tick loop decided whether it belonged to a negotiation; the loop
# then counted the rest as unmatched and dropped them — after the transport had
# already consumed them. On an account that was also somebody's personal inbox
# that cleared a hundred unrelated messages in one pass, and clearing UNREAD
# cannot be undone.


async def test_mail_outside_our_threads_is_not_returned() -> None:
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="ours", thread_id="t-negotiation"),
        _inbound(message_id="theirs", thread_id="t-newsletter"),
    ]

    got = await transport.poll(threads=frozenset({"t-negotiation"}))

    assert [m.message_id for m in got] == ["ours"]


async def test_mail_outside_our_threads_is_never_marked_read() -> None:
    """The half that cannot be undone, and the one that did the damage."""
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="ours", thread_id="t-negotiation"),
        _inbound(message_id="bank-alert", thread_id="t-bank"),
        _inbound(message_id="newsletter", thread_id="t-shop"),
    ]

    _ = await transport.poll(threads=frozenset({"t-negotiation"}))

    touched = [message_id for message_id, _ in fake.modified]
    assert touched == ["ours"], "only our own conversation may be consumed"


async def test_polling_without_saying_what_you_own_changes_nothing() -> None:
    """Inspection is safe; consumption requires naming the threads.

    Destructive by default is how the original bug shipped. A caller that has
    not said what it owns gets to look and nothing else.
    """
    transport, fake = _transport()
    fake.inbox = [_inbound(message_id="whatever", thread_id="t-anything")]

    got = await transport.poll()

    assert [m.message_id for m in got] == ["whatever"]
    assert fake.modified == [], "nothing may be marked read"


# --------------------------------------------------------------------------- #
# Why the mailbox is never listed
# --------------------------------------------------------------------------- #
#
# messages.list returns at most a hundred per page, and was called unpaged. A
# supplier's reply on a busy mailbox fell off page one and was never read: the
# negotiation ran out its rounds and closed as though nobody had answered.
# Silence is what a supplier going quiet looks like, so nothing looked wrong.


async def test_a_reply_is_found_under_a_hundred_newer_unread_messages() -> None:
    """The regression that started this. Ours is last; it is still returned."""
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id=f"noise-{n}", thread_id=f"t-noise-{n}") for n in range(100)
    ]
    fake.inbox.append(_inbound(message_id="reply", thread_id="t-negotiation"))

    got = await transport.poll(threads=frozenset({"t-negotiation"}))

    assert [m.message_id for m in got] == ["reply"]
    assert fake.queries == [], "the loop must not list the mailbox at all"


async def test_our_own_sent_mail_in_the_thread_is_not_read_back_as_a_reply() -> None:
    """Gmail keeps our outbound in the same thread. Filing it would have the
    agent answering itself."""
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="ours-out", thread_id="t-1", labels=["SENT", "UNREAD"]),
        _inbound(message_id="theirs", thread_id="t-1"),
    ]

    got = await transport.poll(threads=frozenset({"t-1"}))

    assert [m.message_id for m in got] == ["theirs"]
    assert [message_id for message_id, _ in fake.modified] == ["theirs"]


async def test_a_message_already_filed_is_not_returned_a_second_time() -> None:
    """UNREAD is the only record of what a previous tick consumed."""
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="filed", thread_id="t-1", labels=["INBOX"]),
        _inbound(message_id="fresh", thread_id="t-1"),
    ]

    got = await transport.poll(threads=frozenset({"t-1"}))

    assert [m.message_id for m in got] == ["fresh"]


async def test_a_thread_that_no_longer_exists_does_not_stop_the_tick() -> None:
    """One deleted conversation must not strand every other negotiation."""
    transport, fake = _transport()
    fake.inbox = [_inbound(message_id="alive", thread_id="t-alive")]

    got = await transport.poll(threads=frozenset({"t-alive", "t-deleted"}))

    assert [m.message_id for m in got] == ["alive"]


# --------------------------------------------------------------------------- #
# Looking without consuming
# --------------------------------------------------------------------------- #
#
# poll() answers "what is new", and its only negative answer is "nothing" —
# which covers both a reply that never arrived and a reply somebody opened in
# Gmail before the poll ran. Opening a message clears UNREAD, so a person
# checking their own mailbox destroys the evidence poll works from. Inspection
# separates the two, and must never itself consume anything.


async def test_inspection_shows_read_and_unread_alike() -> None:
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="old", thread_id="t-1", labels=["INBOX"]),
        _inbound(message_id="new", thread_id="t-1"),
    ]

    seen = await transport.inspect_thread("t-1")

    assert [m.inbound.message_id for m in seen] == ["old", "new"]
    assert [m.is_unread for m in seen] == [False, True]


async def test_inspection_marks_nothing_read() -> None:
    """The whole point: asking the question must not change the answer."""
    transport, fake = _transport()
    fake.inbox = [_inbound(message_id="reply", thread_id="t-1")]

    _ = await transport.inspect_thread("t-1")

    assert fake.modified == []


async def test_inspection_says_which_messages_are_ours() -> None:
    """Otherwise the sent copy reads as a reply and the check passes falsely."""
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="ours", thread_id="t-1", labels=["SENT"]),
        _inbound(message_id="theirs", thread_id="t-1"),
    ]

    seen = await transport.inspect_thread("t-1")

    assert [m.is_ours for m in seen] == [True, False]


async def test_inspecting_a_thread_that_is_gone_is_an_answer_not_an_error() -> None:
    transport, _ = _transport()

    assert await transport.inspect_thread("t-never-existed") == []


async def test_the_read_only_sample_is_bounded() -> None:
    """It exists for a person at a terminal, not for the loop.

    Unbounded, it would page a busy mailbox into the console and cost a
    request per message — and the sample is only ever a hint about where a
    reply landed, so completeness is not what it is for.
    """
    transport, fake = _transport()
    fake.inbox = [_inbound(message_id=f"m-{n}", thread_id=f"t-{n}") for n in range(200)]

    sampled = await transport.poll()

    assert len(sampled) == 25
    assert fake.modified == [], "a sample must never consume"


# --------------------------------------------------------------------------- #
# The blind spot
# --------------------------------------------------------------------------- #
#
# Gmail's messages.list defaults includeSpamTrash to false. Every listing this
# system has run has therefore been unable to see a filtered reply — and a
# supplier whose answer lands in spam is, from the panel, identical to one who
# never answered. Diagnostics have to be able to look there. Nothing in the
# loop passes it, because the loop does not list the mailbox at all.


async def test_a_filtered_reply_is_invisible_unless_asked_for() -> None:
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="clean", thread_id="t-1"),
        _inbound(message_id="filtered", thread_id="t-2", labels=["SPAM", "UNREAD"]),
    ]

    default = await transport.search("is:unread")
    widened = await transport.search("is:unread", include_spam_trash=True)

    assert [m.inbound.message_id for m in default] == ["clean"]
    assert [m.inbound.message_id for m in widened] == ["clean", "filtered"]


async def test_search_reports_the_labels_so_spam_can_be_named() -> None:
    """ "It arrived" and "it arrived in spam" are different answers."""
    transport, fake = _transport()
    fake.inbox = [_inbound(message_id="filtered", labels=["SPAM", "UNREAD"])]

    found = await transport.search("x", include_spam_trash=True)

    assert "SPAM" in found[0].labels


async def test_search_consumes_nothing_however_it_is_called() -> None:
    transport, fake = _transport()
    fake.inbox = [
        _inbound(message_id="a", thread_id="t-1"),
        _inbound(message_id="b", thread_id="t-2", labels=["SPAM", "UNREAD"]),
    ]

    _ = await transport.search("x")
    _ = await transport.search("x", include_spam_trash=True)

    assert fake.modified == []


async def test_search_is_capped() -> None:
    transport, fake = _transport()
    fake.inbox = [_inbound(message_id=f"m-{n}", thread_id=f"t-{n}") for n in range(60)]

    assert len(await transport.search("x", limit=5)) == 5


async def test_the_loop_never_widens_the_search_to_spam() -> None:
    """The sample is a debugging affordance. Spam is opt-in, per call."""
    transport, fake = _transport()
    fake.inbox = [_inbound()]

    _ = await transport.poll()

    assert fake.spam_trash_included == [False]
