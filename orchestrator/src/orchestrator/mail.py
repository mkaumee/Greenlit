"""The mail seam.

``MailTransport`` is what the tick loop talks to. The Gmail implementation and
the in-memory one both satisfy it, which is what lets the whole loop run end to
end today, before any OAuth consent screen exists.

Note what a transport does *not* do: it does not know what time it is. Inbound
messages come back without a timestamp, and the tick loop stamps them with
``clock.now()`` on arrival. A transport that read a real clock would put
wall-clock times into the timeline and desynchronise it from every ``due_at``
in Firestore the moment demo mode ran.

Threading rules, which live here because getting them wrong is silent:

- Every reply carries ``In-Reply-To`` and ``References``. Without them Gmail
  starts a new thread and the conversation fragments across the supplier's
  inbox, which makes us look like a broken mailing list rather than a buyer.
- Inbound mail is matched by thread ID and never by subject. Suppliers rewrite
  subjects — "Re: quote" becomes "quotation for your shoot" — and a subject
  match files their reply against the wrong negotiation.
"""

from typing import ClassVar, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RawInbound(BaseModel):
    """A received message, before the system knows when it arrived.

    Deliberately has no timestamp. See the module docstring.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    message_id: str
    """The transport's own handle. Gmail's API id — our idempotency key."""

    rfc822_message_id: str = ""
    """The ``Message-ID`` header. What a reply must quote in ``In-Reply-To``."""

    thread_id: str
    from_email: str
    subject: str
    body: str
    has_attachments: bool = False
    attachment_filenames: list[str] = Field(default_factory=list)


class SentMessage(BaseModel):
    """What came back from handing a message to the transport.

    Two ids, because they do different jobs and are not interchangeable.

    ``message_id`` is the transport's handle — for Gmail, an API id like
    ``18f2a1c0d4e5``. It is what we key stored messages on, so redelivery after
    a killed tick is a no-op.

    ``rfc822_message_id`` is the ``Message-ID`` *header* — ``<uuid@host>``. It
    is the only thing a mail client will match an ``In-Reply-To`` against.
    Putting the API id there instead produces a header nothing recognises, and
    every reply forks a new thread in the supplier's inbox. We would not notice
    from inside, because our own routing is by ``thread_id`` and keeps working.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    message_id: str
    rfc822_message_id: str
    thread_id: str


class MailTransport(Protocol):
    """Send mail, and collect what has arrived since the last poll."""

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> SentMessage:
        """Send a message, starting a thread or continuing one.

        ``in_reply_to`` and ``references`` are **RFC-822 header values**, not
        transport ids — see ``SentMessage``. Implementations set the headers
        verbatim and must also mint a ``Message-ID`` of their own to return.
        """
        ...

    async def poll(self) -> list[RawInbound]:
        """Everything unread, oldest first, marked read as a side effect.

        Polled rather than pushed. Pub/Sub push needs a verified domain and buys
        nothing at this scale.
        """
        ...


class InMemoryMailbox:
    """A transport with no network behind it.

    Used by the tick-loop tests and by ``make e2e``. Also the thing that makes
    the daily end-to-end habit possible on a laptop with no credentials: the
    loop exercised here is the same loop, with one object swapped.

    A ``responder`` may be attached to answer outgoing mail, which is how the
    e2e run gets a supplier to negotiate against without standing up the
    simulator service.
    """

    sent: list[dict[str, str]]
    _inbox: list[RawInbound]
    _counter: int

    def __init__(self) -> None:
        self.sent = []
        self._inbox = []
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
    ) -> SentMessage:
        message_id = self._next_id("msg")
        # Shaped like a real header so tests catch anyone conflating the two ids.
        rfc822 = f"<{uuid4()}@local.invalid>"
        resolved_thread = thread_id or self._next_id("thread")
        self.sent.append(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "thread_id": resolved_thread,
                "in_reply_to": in_reply_to,
                "references": references,
                "message_id": message_id,
                "rfc822_message_id": rfc822,
            }
        )
        return SentMessage(
            message_id=message_id,
            rfc822_message_id=rfc822,
            thread_id=resolved_thread,
        )

    async def poll(self) -> list[RawInbound]:
        delivered = self._inbox
        self._inbox = []
        return delivered

    # -- test-side helpers, not part of MailTransport ---------------------- #

    def deliver(
        self,
        *,
        thread_id: str,
        body: str,
        from_email: str = "supplier@example.invalid",
        subject: str = "Re: quote",
        has_attachments: bool = False,
        attachment_filenames: list[str] | None = None,
    ) -> RawInbound:
        """Queue an inbound reply for the next poll."""
        message = RawInbound(
            message_id=self._next_id("in"),
            rfc822_message_id=f"<{uuid4()}@supplier.invalid>",
            thread_id=thread_id,
            from_email=from_email,
            subject=subject,
            body=body,
            has_attachments=has_attachments,
            attachment_filenames=attachment_filenames or [],
        )
        self._inbox.append(message)
        return message

    def last_sent(self) -> dict[str, str] | None:
        return self.sent[-1] if self.sent else None
