# The `with_client` fixture is requested for its patching, not its value, so
# every test that needs credentials present takes a parameter it never reads.
# pyright: reportUnusedParameter=false
"""The refusal messages on the hand-run Gmail check.

Only the refusals. Sending is the one thing in this repo that hands a message
to Google, so it has no test and never will — but what it prints when it
*declines* to run is read by a person at a terminal who is already stuck, and
that has been wrong twice.

Both cases below shipped. Neither raised anything, neither failed a check, and
both cost real time: a command that cannot work looks exactly like a command
that can.
"""

from collections.abc import Iterator
from unittest.mock import patch

import gmail_smoke
import pytest
from gmail_smoke import _preflight  # pyright: ignore[reportPrivateUsage]
from orchestrator.settings import Settings, TokenBackend


def _present(_: Settings) -> tuple[str, str]:
    return ("id", "secret")


def _absent(_: Settings) -> tuple[str, str]:
    return ("", "")


@pytest.fixture
def with_client() -> Iterator[None]:
    """Past the first gate, so the token check is what we are reading."""
    with patch.object(gmail_smoke, "client_credentials", _present):
        yield


def test_the_suggested_project_is_never_the_emulators_placeholder(
    with_client: None,
) -> None:
    """``demo-cinema`` is the emulator's name and belongs to nobody.

    Printing it back produced a command that fails with PROJECT_NOT_FOUND,
    which is a worse outcome than printing nothing: it reads as instruction
    rather than as a guess.
    """
    message = _preflight(Settings(gcp_project="demo-cinema"), polling=False)

    assert "CINEMA_GCP_PROJECT=demo-cinema" not in message
    assert "CINEMA_GCP_PROJECT=$(gcloud config get-value project)" in message


def test_a_real_project_is_named_outright(with_client: None) -> None:
    """When we know it, guessing would be the worse answer."""
    message = _preflight(
        Settings(gcp_project="encoded-phalanx-505503-v8"), polling=True
    )

    assert "CINEMA_GCP_PROJECT=encoded-phalanx-505503-v8" in message


@pytest.mark.parametrize(
    ("polling", "expected", "forbidden"),
    [(True, "POLL=1", "TO="), (False, "TO=seller@example.com", "POLL=1")],
)
def test_the_fix_repeats_the_command_that_was_actually_run(
    with_client: None, polling: bool, expected: str, forbidden: str
) -> None:
    """Answering `POLL=1` with `TO=...` reads as advice for another problem."""
    message = _preflight(Settings(), polling=polling)

    assert f"make gmail-smoke {expected}" in message
    assert forbidden not in message


def test_a_secret_manager_run_is_not_blocked_by_a_missing_local_file(
    with_client: None,
) -> None:
    """The token check is about the file backend only. Guarding this because
    the message it would print is advice to go and fetch a token you have."""
    settings = Settings(token_backend=TokenBackend.SECRET_MANAGER)

    assert _preflight(settings, polling=False) == ""


def test_missing_oauth_credentials_are_reported_before_the_token() -> None:
    """Order matters: without a client id the token cannot be exchanged, so
    naming the token would send someone to re-bootstrap for nothing."""
    with patch.object(gmail_smoke, "client_credentials", _absent):
        message = _preflight(Settings(), polling=False)

    assert "CINEMA_OAUTH_CLIENT_ID" in message
    assert "oauth_bootstrap.py" not in message, "the token is not the problem"
