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

from typing import Protocol, final

from orchestrator.mail import MailTransport


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
