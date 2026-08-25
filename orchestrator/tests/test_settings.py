"""Tests for configuration.

Mostly checking the defaults are the ones that let the system run with no cloud
project at all, since that is the situation today and getting it wrong means a
Secret Manager client is constructed against a project that does not exist.
"""

from pathlib import Path

import pytest
from orchestrator.settings import GMAIL_SCOPES, Settings, TokenBackend


def test_it_runs_with_no_cloud_project_by_default() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.token_backend is TokenBackend.FILE
    assert settings.token_dir == Path(".secrets")


def test_the_token_directory_is_gitignored() -> None:
    """The default must never be a path that could be committed."""
    ignored = Path(__file__).resolve().parents[2] / ".gitignore"
    assert "secrets/" in ignored.read_text(encoding="utf-8")


def test_environment_overrides_are_picked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CINEMA_GCP_PROJECT", "cinema-prod")
    monkeypatch.setenv("CINEMA_TOKEN_BACKEND", "secret-manager")
    monkeypatch.setenv("CINEMA_TICK_LIMIT", "5")

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.gcp_project == "cinema-prod"
    assert settings.token_backend is TokenBackend.SECRET_MANAGER
    assert settings.tick_limit == 5


def test_switching_to_the_cloud_is_one_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole local-to-GCP migration, as far as this module is concerned."""
    monkeypatch.setenv("CINEMA_TOKEN_BACKEND", "secret-manager")
    assert Settings(_env_file=None).token_backend is TokenBackend.SECRET_MANAGER  # pyright: ignore[reportCallIssue]


def test_scopes_can_mark_read_but_cannot_delete() -> None:
    """Polling must clear UNREAD, so readonly is not enough. Nothing more."""
    assert any(scope.endswith("gmail.modify") for scope in GMAIL_SCOPES)
    assert any(scope.endswith("gmail.send") for scope in GMAIL_SCOPES)
    assert not any("mail.google.com" in scope for scope in GMAIL_SCOPES)


def test_the_poll_query_ignores_our_own_sent_mail() -> None:
    assert "-from:me" in Settings(_env_file=None).poll_query  # pyright: ignore[reportCallIssue]


def test_refresh_token_path_sits_under_the_token_dir() -> None:
    settings = Settings(_env_file=None, token_dir=Path("/tmp/toks"))  # pyright: ignore[reportCallIssue]
    assert settings.refresh_token_path.parent == Path("/tmp/toks")
