"""Which mailbox a project sends from.

Every case here is a way this returns nothing to send with, because that is
what the class mostly does. A producer who has not connected, a token that
expired on its seventh day, an OAuth client that was never configured — none of
them is an error, all of them stop a project sending, and each has a different
thing a person must do about it.

The one shape that must never appear is an exception escaping into the tick:
one project's dead mailbox cannot be allowed to stop every other project
advancing.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cinema_contracts import ClockMode
from google.auth.exceptions import RefreshError
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.clock import ClockState
from orchestrator.gmail import GmailTransport
from orchestrator.mail import InMemoryMailbox
from orchestrator.mailboxes import (
    ProducerMailboxes,
    SingleMailbox,
    is_expired_credential,
)
from orchestrator.records import MailboxRecord, MailboxStatus, ProjectRecord
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import Settings, TokenBackend

PID = "proj1"
UID = "producer-1"
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
REAL0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        token_backend=TokenBackend.FILE,
        token_dir=tmp_path,
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )


async def _project(repo: FirestoreRepository, owner: str) -> None:
    await repo.create_project(
        PID,
        ProjectRecord(
            title="Kopitiam",
            clock=ClockState(
                sim_now=T0, real_anchor=REAL0, speed=0.0, mode=ClockMode.FROZEN
            ),
            created_at=T0,
            owner_uid=owner,
        ),
    )


def _write_token(settings: Settings) -> None:
    """A bootstrapped refresh token for this producer.

    Written wherever a case needs everything *except* the thing it is testing
    to be in working order — without it a test can pass because the token was
    missing rather than because the guard it names did its job.
    """
    path = settings.token_dir / f"gmail_refresh_token_{UID}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text('{"refresh_token": "a-token"}')


async def _connected(repo: FirestoreRepository, status: MailboxStatus) -> None:
    await repo.save_mailbox(
        UID,
        MailboxRecord(
            email="producer@example.test",
            display_name="A Producer",
            status=status,
            connected_at=T0,
            updated_at=T0,
        ),
    )


# --------------------------------------------------------------------------- #
# The one mailbox everything else runs on
# --------------------------------------------------------------------------- #


async def test_a_single_mailbox_serves_every_project() -> None:
    """What make e2e and every other test use, so it has to stay honest."""
    inbox = InMemoryMailbox()
    provider = SingleMailbox(inbox)

    assert await provider.for_project("anything") is inbox
    assert await provider.for_project("anything-else") is inbox


# --------------------------------------------------------------------------- #
# Per-producer
# --------------------------------------------------------------------------- #


async def test_a_connected_producer_gets_a_transport_sending_as_them(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    """The From line is the producer's, not the configured agent's."""
    repo = FirestoreRepository(firestore)
    settings = _settings(tmp_path)
    await _project(repo, UID)
    await _connected(repo, MailboxStatus.CONNECTED)
    _write_token(settings)

    transport = await ProducerMailboxes(repo, settings).for_project(PID)

    assert isinstance(transport, GmailTransport)


async def test_a_project_with_no_owner_has_no_mailbox(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    """Not an error. It is what a project seeded without --owner-uid looks
    like, and what every project created before mailboxes existed looks like."""
    repo = FirestoreRepository(firestore)
    await _project(repo, "")

    assert await ProducerMailboxes(repo, _settings(tmp_path)).for_project(PID) is None


async def test_an_owner_who_never_connected_has_no_mailbox(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    repo = FirestoreRepository(firestore)
    await _project(repo, UID)

    assert await ProducerMailboxes(repo, _settings(tmp_path)).for_project(PID) is None


async def test_an_expired_mailbox_is_not_retried_every_minute(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    """Already known dead. Spending a Secret Manager read to rediscover that
    once a minute, for days, is waste with a bill attached."""
    repo = FirestoreRepository(firestore)
    settings = _settings(tmp_path)
    await _project(repo, UID)
    await _connected(repo, MailboxStatus.EXPIRED)
    # Everything else in working order, so the status is the only reason this
    # returns nothing. Without the token here the test would pass whether or
    # not the guard existed.
    _write_token(settings)

    assert await ProducerMailboxes(repo, settings).for_project(PID) is None


async def test_a_missing_token_is_reported_not_raised(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    """The mailbox says connected and the secret is gone — revoked, or deleted
    by hand. One project in that state must not stop the others ticking."""
    repo = FirestoreRepository(firestore)
    await _project(repo, UID)
    await _connected(repo, MailboxStatus.CONNECTED)

    assert await ProducerMailboxes(repo, _settings(tmp_path)).for_project(PID) is None


async def test_no_oauth_client_means_no_mailbox_rather_than_a_crash(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    repo = FirestoreRepository(firestore)
    await _project(repo, UID)
    await _connected(repo, MailboxStatus.CONNECTED)
    bare = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        token_backend=TokenBackend.FILE,
        token_dir=tmp_path,
        oauth_client_secrets=tmp_path / "absent.json",
    )
    _write_token(bare)

    assert await ProducerMailboxes(repo, bare).for_project(PID) is None


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #


async def test_marking_expired_is_visible_to_the_panel(
    firestore: AsyncClient, tmp_path: Path
) -> None:
    """Seven days is the lifetime of a Testing-mode refresh token, so this is a
    state a working deployment reaches every week. It has to be on screen, not
    inferred from a project that stopped moving."""
    repo = FirestoreRepository(firestore)
    settings = _settings(tmp_path)
    await _project(repo, UID)
    await _connected(repo, MailboxStatus.CONNECTED)

    await ProducerMailboxes(repo, settings).mark_expired(PID, T0)

    mailbox = await repo.get_mailbox(UID)
    assert mailbox is not None
    assert mailbox.status is MailboxStatus.EXPIRED
    assert mailbox.email == "producer@example.test", "the address is not lost"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RefreshError("invalid_grant"), True),
        (TimeoutError("network"), False),
        (RuntimeError("something else"), False),
    ],
)
def test_only_a_refused_refresh_means_reconnect(
    error: BaseException, expected: bool
) -> None:
    """A network blip is retried next tick; an expired token never will be.

    Treating the second as the first is how a project sits still for days with
    nothing on screen explaining why.
    """
    assert is_expired_credential(error) is expected
