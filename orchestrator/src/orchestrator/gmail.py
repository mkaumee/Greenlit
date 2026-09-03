# googleapiclient builds its resource objects at runtime from a discovery
# document, so users()/messages()/execute() are Unknown to a type checker and
# nothing can be done about that here. Suppressing the unknown-type family in
# this one module keeps the strict settings everywhere else, exactly as
# repository.py does for the Firestore client.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportExplicitAny=false
# pyright: reportAny=false, reportMissingTypeStubs=false
"""The real Gmail transport, plus somewhere to keep a refresh token.

Satisfies the same ``MailTransport`` protocol as ``InMemoryMailbox``, so the
tick loop cannot tell them apart and the end-to-end run keeps working with no
credentials.

Three things here are easy to get wrong and expensive to notice late.

**The two message ids.** Gmail's ``send`` returns an API handle. The
``Message-ID`` *header* is a different string entirely, and it is the only one a
mail client will match ``In-Reply-To`` against. We mint our own header before
sending so we know it, rather than fetching the sent message back to read it.

**Marking mail read.** Polling by unread query and not clearing the label means
every tick re-reads the same replies forever. That is why the scopes include
``gmail.modify``.

**Blocking calls.** ``googleapiclient`` is synchronous. Every call goes through
``asyncio.to_thread`` so one slow request cannot stall the loop.

This module reads no clock. ``RawInbound`` comes back without a timestamp and
the tick stamps it with ``clock.now()`` — see Hard Rule 2 in CLAUDE.md.
"""

import asyncio
import base64
import json
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from orchestrator.mail import RawInbound, SentMessage
from orchestrator.settings import GMAIL_SCOPES, Settings, TokenBackend


class TokenStore(Protocol):
    """Where the OAuth refresh token lives."""

    def read(self) -> str: ...

    def write(self, refresh_token: str) -> None: ...


class FileTokenStore:
    """A gitignored file on disk. The default while there is no GCP project.

    Not a long-term answer, but the alternative is blocking every bit of Gmail
    work on cloud billing. The path is under ``.secrets/``, which `.gitignore`
    already excludes, so nothing credential-shaped can be committed by accident.
    """

    _path: Path

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> str:
        if not self._path.exists():
            raise FileNotFoundError(
                f"No refresh token at {self._path}. "
                f"Run `uv run python scripts/oauth_bootstrap.py` first — "
                f"see docs/oauth-runbook.md."
            )
        payload: dict[str, str] = json.loads(self._path.read_text(encoding="utf-8"))
        return payload["refresh_token"]

    def write(self, refresh_token: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _ = self._path.write_text(
            json.dumps({"refresh_token": refresh_token}, indent=2) + "\n",
            encoding="utf-8",
        )
        self._path.chmod(0o600)


class SecretManagerTokenStore:
    """Google Secret Manager. Used once ``CINEMA_TOKEN_BACKEND=secret-manager``.

    The import is deliberately inside the methods: constructing a Secret Manager
    client requires credentials, and today there is no project to authenticate
    against. Nothing in this class runs until it is selected.
    """

    _project: str
    _secret: str

    def __init__(self, project: str, secret: str) -> None:
        self._project = project
        self._secret = secret

    def read(self) -> str:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{self._project}/secrets/{self._secret}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")

    def write(self, refresh_token: str) -> None:
        """Add a version, creating the secret the first time.

        Secret Manager separates the container from its contents, and
        ``add_secret_version`` on a container that does not exist is a plain
        ``NotFound``. That was fine while there was one bootstrapped secret and
        a human had made it by hand; it stopped being fine the moment every
        producer got their own, because the first thing a new producer does is
        write to a name nobody has created. It surfaced as a 500 on the OAuth
        callback *after* Google had already granted consent — the worst place
        for it, since the producer has done everything right and the grant is
        spent.

        Add-then-create rather than get-then-add: reconnecting is the common
        case and costs one call, and the check-then-act version has a race
        that this does not. Two producers connecting at the same instant both
        create; one gets ``AlreadyExists`` and carries on to add its version.
        """
        from google.api_core import exceptions
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{self._project}/secrets/{self._secret}"
        payload = {"data": refresh_token.encode()}

        try:
            _ = client.add_secret_version(
                request={"parent": parent, "payload": payload}
            )
            return
        except exceptions.NotFound:
            pass

        with suppress(exceptions.AlreadyExists):
            _ = client.create_secret(
                request={
                    "parent": f"projects/{self._project}",
                    "secret_id": self._secret,
                    # Automatic replication: this is a credential, not data with
                    # a residency requirement, and pinning regions here would be
                    # a decision nothing else in the deploy makes.
                    "secret": {"replication": {"automatic": {}}},
                }
            )

        _ = client.add_secret_version(request={"parent": parent, "payload": payload})


def token_store_for(settings: Settings) -> TokenStore:
    """Pick a store from configuration. The whole local-to-cloud switch."""
    if settings.token_backend is TokenBackend.SECRET_MANAGER:
        return SecretManagerTokenStore(
            settings.gcp_project, settings.refresh_token_secret
        )
    return FileTokenStore(settings.refresh_token_path)


def producer_token_store(settings: Settings, uid: str) -> TokenStore:
    """Where one producer's refresh token lives.

    One secret per producer rather than one shared secret holding a map of
    them. A map would need only a single Secret Manager binding, which is
    tempting, but Secret Manager has no compare-and-swap: two producers
    connecting at once would read the same version and one would overwrite the
    other's token. Losing a credential that way is silent, and the producer who
    lost it finds out days later when their negotiations have not moved.

    Falls back to the file store when that is the configured backend, so a
    laptop with no GCP project can still exercise the flow.
    """
    if settings.token_backend is TokenBackend.SECRET_MANAGER:
        return SecretManagerTokenStore(
            settings.gcp_project, f"{settings.refresh_token_secret}-{uid}"
        )
    return FileTokenStore(settings.token_dir / f"gmail_refresh_token_{uid}.json")


def client_credentials(settings: Settings) -> tuple[str, str]:
    """The OAuth client id and secret, from configuration or the client file.

    Explicit settings win: that is what Cloud Run has, since no client JSON is
    baked into the image.

    Otherwise they are read out of ``oauth_client_secrets`` — the same file the
    bootstrap consented with, whose path is already configuration. Asking a
    person to copy two strings out of a file we know how to find is friction
    and a place to paste the wrong thing.

    Handles both top-level shapes: ``installed`` for a Desktop client and
    ``web`` for a Web-application one. Both are legitimate here — the Web shape
    is what the Cloud Shell consent flow requires.

    Returns ``("", "")`` when neither source has them, so callers keep their own
    refusal message rather than getting a partial credential.
    """
    if settings.oauth_client_id and settings.oauth_client_secret:
        return settings.oauth_client_id, settings.oauth_client_secret

    try:
        blob: dict[str, Any] = json.loads(settings.oauth_client_secrets.read_text())
    except OSError, ValueError:
        return "", ""

    for shape in ("installed", "web"):
        section: Any = blob.get(shape)
        if isinstance(section, dict):
            client_id = str(section.get("client_id") or "")
            client_secret = str(section.get("client_secret") or "")
            if client_id and client_secret:
                return client_id, client_secret
    return "", ""


def build_credentials(
    store: TokenStore, client_id: str, client_secret: str
) -> Credentials:
    """A refreshable credential from a stored refresh token."""
    return Credentials(
        token=None,
        refresh_token=store.read(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(GMAIL_SCOPES),
    )


def _header(headers: list[dict[str, str]], name: str) -> str:
    """Case-insensitive header lookup. Gmail is inconsistent about casing."""
    wanted = name.lower()
    for header in headers:
        if header.get("name", "").lower() == wanted:
            return header.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")


def _walk_parts(payload: dict[str, Any]) -> tuple[str, list[str]]:
    """Pull the plain-text body and any attachment filenames out of a payload.

    Prefers ``text/plain``; falls back to ``text/html`` only if there is nothing
    else, because a supplier's mail client may well send HTML only and a quote
    inside it still beats no quote at all.

    Attachment filenames matter beyond bookkeeping: the brain escalates rather
    than guessing when the price is inside a PDF, and it can only do that if we
    tell it there was one.
    """
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        filename = str(part.get("filename") or "")
        mime = str(part.get("mimeType") or "")
        body: dict[str, Any] = part.get("body") or {}
        data = body.get("data")

        if filename:
            attachments.append(filename)
        elif mime == "text/plain" and isinstance(data, str):
            plain.append(_decode(data))
        elif mime == "text/html" and isinstance(data, str):
            html.append(_decode(data))

        for child in part.get("parts") or []:
            visit(child)

    visit(payload)
    body_text = "\n".join(plain) if plain else "\n".join(html)
    return body_text.strip(), attachments


@dataclass(frozen=True)
class ThreadMessage:
    """One message in a conversation, with the labels Gmail files it under.

    Only ``inspect_thread`` returns these. The loop deliberately never sees a
    label: it is handed ``RawInbound`` and cannot tell a read message from an
    unread one, because deciding that is the transport's job and doing it in
    two places is how a message gets filed twice.
    """

    inbound: RawInbound
    labels: frozenset[str]

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.labels

    @property
    def is_ours(self) -> bool:
        """Sent by us. Gmail keeps our own outbound in the same thread."""
        return "SENT" in self.labels


@dataclass(frozen=True)
class Sender:
    """Whose name and address go in the From line.

    Was a pair of global settings, when there was one mailbox. Now that a
    producer connects their own Gmail it belongs to the transport, because the
    address a supplier sees has to be the address that actually sent the
    message — Gmail rewrites a From header that does not match the
    authenticated account, so getting this wrong does not fail loudly, it just
    quietly sends as somebody else.
    """

    email: str
    display_name: str

    @property
    def header(self) -> str:
        return f"{self.display_name} <{self.email}>"


class GmailTransport:
    """Send and poll one mailbox over the Gmail API."""

    _service: Any
    _settings: Settings
    _sender: Sender

    def __init__(
        self, service: Any, settings: Settings, sender: Sender | None = None
    ) -> None:
        self._service = service
        self._settings = settings
        # Falls back to the configured identity, which is what the single
        # shared mailbox used and what the smoke check still runs on.
        self._sender = sender or Sender(
            email=settings.agent_email, display_name=settings.agent_display_name
        )

    @classmethod
    def from_credentials(
        cls,
        credentials: Credentials,
        settings: Settings,
        sender: Sender | None = None,
    ) -> GmailTransport:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(service, settings, sender)

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
        """Send one message, minting our own ``Message-ID`` so we know it.

        Gmail will happily generate a ``Message-ID`` itself, but then we would
        have to fetch the sent message back to learn it before we could thread
        the next reply. Setting it ourselves costs one line and removes a round
        trip and a failure mode.
        """
        message = EmailMessage()
        message["To"] = to
        message["From"] = self._sender.header
        message["Subject"] = subject
        rfc822_id = make_msgid(domain=self._sender.email.split("@")[-1])
        message["Message-ID"] = rfc822_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        message.set_content(body)

        request: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        }
        if thread_id:
            request["threadId"] = thread_id

        sent = await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .send(userId="me", body=request)
                .execute()
            )
        )

        return SentMessage(
            message_id=str(sent["id"]),
            rfc822_message_id=rfc822_id,
            thread_id=str(sent.get("threadId") or thread_id),
        )

    async def poll(self, *, threads: frozenset[str] | None = None) -> list[RawInbound]:
        """Read unread replies in the conversations we started.

        ``threads`` is those conversations, and each is fetched **by id**. The
        mailbox is never listed, so nothing can be paged out of view and
        nothing outside our own threads is touched. Only messages returned have
        UNREAD cleared.

        Two bugs live here, both found on a real mailbox, and both silent:

        The transport used to mark every unread message read before the tick
        loop decided whether it belonged to a negotiation. On an account that
        was also somebody's personal inbox that consumed a hundred unrelated
        messages in one pass, and clearing UNREAD cannot be undone.

        Worse, it found those messages with ``messages.list``, which returns at
        most a hundred per page and was called without paging. A supplier's
        reply on a busy mailbox simply fell off page one and was never read —
        the negotiation then ran out its rounds and closed as though nobody had
        answered. Nothing errored and nothing logged. Fetching known threads
        directly makes the cost scale with live negotiations rather than with
        the size of somebody's inbox, and makes that failure impossible rather
        than unlikely.

        Returns oldest first within each thread, which is the order Gmail
        stores them — a supplier who sends twice between ticks must not have
        them filed backwards in the timeline.
        """
        if threads is None:
            return await self._inspect()

        received: list[RawInbound] = []
        for thread_id in sorted(threads):
            thread = await self._fetch_thread(thread_id)
            if thread is None:
                # Deleted, or never ours. One missing thread must not stop a
                # tick that has other negotiations to advance.
                continue

            for message in thread.get("messages") or []:
                labels: list[str] = message.get("labelIds") or []
                if "UNREAD" not in labels:
                    continue  # already filed on an earlier tick
                if "SENT" in labels:
                    continue  # our own outbound, echoed back in the thread

                received.append(self._to_inbound(message, thread_id))
                await self._mark_read(str(message["id"]))

        return received

    async def inspect_thread(self, thread_id: str) -> list[ThreadMessage]:
        """Every message in one conversation, read or unread, changing nothing.

        ``poll`` answers "what is new", and can only ever say "nothing" — which
        is the same word for a reply that has not arrived and a reply that was
        opened in Gmail before we got to it. Opening a message clears UNREAD,
        so a person checking their own mailbox destroys the only evidence poll
        works from.

        For the product that distinction does not arise: the tick reads a
        mailbox nobody else is looking at. For a person verifying by hand it is
        the whole question, so this answers it directly rather than by
        inference. Marks nothing, so asking is free.

        An absent thread is an empty list, not an error — it is a question, and
        "there is nothing there" is a real answer to it.
        """
        thread = await self._fetch_thread(thread_id)
        if thread is None:
            return []
        return [
            ThreadMessage(
                inbound=self._to_inbound(message, thread_id),
                labels=frozenset(message.get("labelIds") or []),
            )
            for message in thread.get("messages") or []
        ]

    async def connected_address(self) -> str:
        """Which mailbox this credential actually opens.

        Asked at connect time rather than assumed from the Firebase account: a
        producer may well sign in with one Google account and authorise a
        different one, and it is this address that ends up in a supplier's From
        line. Guessing it would mean the panel telling a producer they are
        sending as an address they are not.
        """
        profile = await asyncio.to_thread(
            lambda: self._service.users().getProfile(userId="me").execute()
        )
        return str(profile.get("emailAddress") or "")

    async def restore_unread(self, thread_id: str) -> list[str]:
        """Put UNREAD back on the replies in one thread. Returns what changed.

        The exact mirror of ``_mark_read``, and the only write in this class
        outside sending. It exists because the live round-trip check had no way
        to re-arm itself: proving ``poll`` returns a reply needs an unread
        reply, and once one has been read the only way back was to ask a person
        to send another and then not look at their own inbox. That failed three
        times, which is a fair sign it was the wrong instruction to be giving.

        Scoped to a single named thread on purpose. It cannot re-arm a mailbox,
        only a conversation we started, and it is not on ``MailTransport`` — the
        tick loop has no way to reach it and no reason to. Reversible by
        definition: the next poll clears the label again, which is the point.

        ``SENT`` messages are skipped. Poll ignores our own outbound anyway, so
        marking it unread would put nothing but noise in somebody's inbox.
        """
        thread = await self._fetch_thread(thread_id)
        if thread is None:
            return []

        rearmed: list[str] = []
        for message in thread.get("messages") or []:
            labels: list[str] = message.get("labelIds") or []
            if "SENT" in labels:
                continue
            message_id = str(message["id"])
            await self._mark_unread(message_id)
            rearmed.append(message_id)
        return rearmed

    async def search(
        self,
        query: str,
        *,
        limit: int = 25,
        include_spam_trash: bool = False,
    ) -> list[ThreadMessage]:
        """Run one Gmail search and read what it finds. Marks nothing.

        For looking at a mailbox by hand. Deliberately not what the loop uses:
        capped and unpaged, so it is a sample and never a complete view — which
        is precisely the property that made ``messages.list`` the wrong thing
        to build the loop on.

        ``include_spam_trash`` is the reason this takes an argument at all.
        Gmail defaults it to false, so every listing this system has ever run
        has been blind to SPAM and TRASH. A supplier's reply that gets filtered
        is sitting somewhere we have never once looked, and from the panel that
        is indistinguishable from a supplier who never wrote back. Diagnostics
        need to see it; nothing in the loop passes it.

        Returns oldest first — Gmail lists newest first, and reading a
        conversation backwards is how you misjudge who said what last.
        """
        listing = await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=limit,
                    includeSpamTrash=include_spam_trash,
                )
                .execute()
            )
        )

        found: list[ThreadMessage] = []
        for stub in reversed(listing.get("messages") or []):
            full = await self._fetch(str(stub["id"]))
            found.append(
                ThreadMessage(
                    inbound=self._to_inbound(full, str(full.get("threadId") or "")),
                    labels=frozenset(full.get("labelIds") or []),
                )
            )
        return found

    async def _inspect(self) -> list[RawInbound]:
        """The read-only sample behind ``poll()`` with no threads named.

        Unwraps to ``RawInbound`` so ``MailTransport`` keeps one shape and the
        loop cannot tell the two mailbox implementations apart.
        """
        found = await self.search(self._settings.poll_query)
        return [message.inbound for message in found]

    def _to_inbound(self, message: dict[str, Any], thread_id: str) -> RawInbound:
        payload: dict[str, Any] = message.get("payload") or {}
        headers: list[dict[str, str]] = payload.get("headers") or []
        body, attachments = _walk_parts(payload)
        return RawInbound(
            message_id=str(message["id"]),
            rfc822_message_id=_header(headers, "Message-ID"),
            thread_id=thread_id,
            from_email=_header(headers, "From"),
            subject=_header(headers, "Subject"),
            body=body,
            has_attachments=bool(attachments),
            attachment_filenames=attachments,
        )

    async def _fetch_thread(self, thread_id: str) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(
                lambda: (
                    self._service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="full")
                    .execute()
                )
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None
            raise

    async def _fetch(self, message_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        )

    async def _mark_unread(self, message_id: str) -> None:
        """Set UNREAD. Only ``restore_unread`` calls this."""
        _ = await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .modify(
                    userId="me",
                    id=message_id,
                    body={"addLabelIds": ["UNREAD"]},
                )
                .execute()
            )
        )

    async def _mark_read(self, message_id: str) -> None:
        """Clear UNREAD. Without this, every tick re-reads the same replies."""
        _ = await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .modify(
                    userId="me",
                    id=message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                )
                .execute()
            )
        )
