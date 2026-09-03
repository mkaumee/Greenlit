"""Which mailbox a project's mail goes through.

Until now the answer was "the one". A single refresh token was read at startup,
one ``GmailTransport`` was built from it, and every project shared it — so
every supplier heard from the same address regardless of whose production it
was.

That is the wrong product. A producer connects their own Gmail and the agent
negotiates *as them*, because a seller replying to a real person at a real
production behaves differently from one replying to a bot, and that difference
is most of what makes this system worth building.

So the loop no longer holds a transport. It holds one of these and asks, per
project, per tick.

**Why ``None`` is a real answer.** A project whose producer has not connected a
mailbox, or whose refresh token has expired — seven days, in a consent screen
that stays in Testing — has nowhere to send. The loop must be able to say so
and carry on with the rest of its work. Returning ``None`` rather than raising
is what makes "this project is stalled, and here is why" reportable instead of
either a crash or, far worse, silence that looks exactly like nothing being
due.

**Nothing is cached.** A transport is built for one tick and dropped. Hard Rule
3 is not negotiable here just because building one costs a Secret Manager read:
a cached credential is in-memory state between requests, and the process
holding it will be reaped mid-negotiation.
"""

import logging
from datetime import datetime
from typing import Protocol, final

from google.auth.exceptions import RefreshError

from orchestrator.gmail import (
    GmailTransport,
    Sender,
    build_credentials,
    client_credentials,
    producer_token_store,
)
from orchestrator.mail import MailTransport
from orchestrator.records import MailboxStatus
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import Settings

log = logging.getLogger("orchestrator.mailboxes")


class MailboxProvider(Protocol):
    """Answers "which mailbox does this project send from"."""

    async def for_project(self, project_id: str) -> MailTransport | None:
        """The project's mailbox, or ``None`` if it has no working one."""
        ...


@final
class SingleMailbox:
    """One mailbox for everything. What the system did before producers had
    their own, and still what the in-memory path and every test run on.

    Kept as a named implementation rather than a special case inside the loop,
    so ``make e2e`` exercises the same code path the real provider uses and the
    per-project plumbing cannot rot while the default stays green.
    """

    _transport: MailTransport

    def __init__(self, transport: MailTransport) -> None:
        self._transport = transport

    async def for_project(self, project_id: str) -> MailTransport | None:
        _ = project_id  # every project gets the same one; that is the point
        return self._transport


@final
class ProducerMailboxes:
    """Each project sends from the mailbox its producer connected.

    Three lookups per tick per project: the project for its owner, Firestore
    for the connected address, Secret Manager for the refresh token. Built
    fresh every time and held across nothing, because a credential kept between
    ticks is in-memory state and the process holding it gets reaped
    mid-negotiation.

    Every way this can fail returns ``None`` rather than raising, and every one
    of them is a state a producer can see and fix — no owner, no mailbox, an
    expired token. Raising would take down a tick that has other projects to
    advance, and swallowing silently would leave a project looking idle. So it
    logs, records, and returns nothing to send with.
    """

    _repo: FirestoreRepository
    _settings: Settings

    def __init__(self, repo: FirestoreRepository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    async def for_project(self, project_id: str) -> MailTransport | None:
        project = await self._repo.get_project(project_id)
        if project is None or not project.owner_uid:
            # An unowned project is not an error. It is what a project created
            # before mailboxes existed looks like, and what a seed without
            # --owner-uid produces.
            return None

        mailbox = await self._repo.get_mailbox(project.owner_uid)
        if mailbox is None:
            return None
        if mailbox.status is not MailboxStatus.CONNECTED:
            # Already known to be dead. Say nothing further and do not spend a
            # Secret Manager read discovering it again every minute.
            return None

        client_id, client_secret = client_credentials(self._settings)
        if not client_id or not client_secret:
            log.warning(
                "no OAuth client configured, so no producer mailbox can be opened",
                extra={"project_id": project_id},
            )
            return None

        try:
            credentials = build_credentials(
                producer_token_store(self._settings, project.owner_uid),
                client_id,
                client_secret,
            )
        except Exception:
            # The token is gone from Secret Manager — revoked, or a secret
            # deleted by hand. Same outcome as expiry from the loop's side.
            log.warning(
                "no refresh token for this producer",
                extra={"project_id": project_id, "uid": project.owner_uid},
            )
            return None

        return GmailTransport.from_credentials(
            credentials,
            self._settings,
            Sender(
                email=mailbox.email,
                display_name=mailbox.display_name or self._settings.agent_display_name,
            ),
        )

    async def mark_expired(self, project_id: str, now: datetime) -> None:
        """Record that a producer's token stopped working.

        Called when a send or poll is refused, which is where expiry actually
        surfaces — the credential object is built lazily and does not contact
        Google until it is used, so ``for_project`` cannot tell a live token
        from a dead one.

        Seven days is the lifetime of a refresh token from a consent screen in
        Testing, so this is a state a working deployment enters every week. It
        has to be visible in the panel rather than inferred from a project that
        stopped moving.
        """
        project = await self._repo.get_project(project_id)
        if project is None or not project.owner_uid:
            return
        await self._repo.mark_mailbox(project.owner_uid, MailboxStatus.EXPIRED, now)
        log.warning(
            "mailbox expired; the producer must reconnect",
            extra={"project_id": project_id, "uid": project.owner_uid},
        )


def is_expired_credential(error: BaseException) -> bool:
    """Whether a failure means "reconnect", as opposed to "try again".

    google-auth raises ``RefreshError`` for a refresh token Google will not
    honour any more — revoked, or past the seven days a Testing consent screen
    grants. Distinguished from every other failure because the answer differs:
    a network blip is retried on the next tick, an expired token never is, and
    treating the second as the first is how a project sits still for days with
    nothing on screen to say why.
    """
    return isinstance(error, RefreshError)
