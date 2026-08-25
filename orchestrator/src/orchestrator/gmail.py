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
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

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
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        parent = f"projects/{self._project}/secrets/{self._secret}"
        _ = client.add_secret_version(
            request={"parent": parent, "payload": {"data": refresh_token.encode()}}
        )


def token_store_for(settings: Settings) -> TokenStore:
    """Pick a store from configuration. The whole local-to-cloud switch."""
    if settings.token_backend is TokenBackend.SECRET_MANAGER:
        return SecretManagerTokenStore(
            settings.gcp_project, settings.refresh_token_secret
        )
    return FileTokenStore(settings.refresh_token_path)


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


class GmailTransport:
    """Send and poll one mailbox over the Gmail API."""

    _service: Any
    _settings: Settings

    def __init__(self, service: Any, settings: Settings) -> None:
        self._service = service
        self._settings = settings

    @classmethod
    def from_credentials(
        cls, credentials: Credentials, settings: Settings
    ) -> GmailTransport:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(service, settings)

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
        message["From"] = (
            f"{self._settings.agent_display_name} <{self._settings.agent_email}>"
        )
        message["Subject"] = subject
        rfc822_id = make_msgid(domain=self._settings.agent_email.split("@")[-1])
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

    async def poll(self) -> list[RawInbound]:
        """Read unread mail, then clear the label so it is not read twice.

        Returns oldest first. Gmail lists newest first, so the result is
        reversed — otherwise a supplier who sent two messages between ticks
        would have them filed in the wrong order in the timeline.
        """
        listing = await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .list(userId="me", q=self._settings.poll_query)
                .execute()
            )
        )

        received: list[RawInbound] = []
        for stub in reversed(listing.get("messages") or []):
            message_id = str(stub["id"])
            full = await self._fetch(message_id)

            payload: dict[str, Any] = full.get("payload") or {}
            headers: list[dict[str, str]] = payload.get("headers") or []
            body, attachments = _walk_parts(payload)

            received.append(
                RawInbound(
                    message_id=message_id,
                    rfc822_message_id=_header(headers, "Message-ID"),
                    thread_id=str(full.get("threadId") or ""),
                    from_email=_header(headers, "From"),
                    subject=_header(headers, "Subject"),
                    body=body,
                    has_attachments=bool(attachments),
                    attachment_filenames=attachments,
                )
            )

            await self._mark_read(message_id)

        return received

    async def _fetch(self, message_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            lambda: (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
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
