"""The three answers, kept apart.

A refused key and an unreachable API need different things done about them, and
collapsing both into "failed" is how an outage gets mistaken for a bad
credential — you go looking for a new key while the old one is fine.

The distinction is the whole reason this script exists rather than a one-line
curl, so it is what these tests hold.
"""

from typing import final

import check_research
import pytest
from parallel import AuthenticationError, ParallelError


@final
class _Result:
    url: str

    def __init__(self, url: str) -> None:
        self.url = url


@final
class _Response:
    results: list[_Result]

    def __init__(self, urls: list[str]) -> None:
        self.results = [_Result(u) for u in urls]


def _client_raising(error: Exception):
    @final
    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def search(self, **_kwargs: object) -> object:
            raise error

    return _Client


def _client_returning(urls: list[str]):
    @final
    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def search(self, **_kwargs: object) -> _Response:
            return _Response(urls)

    return _Client


def _fake_auth_error() -> AuthenticationError:
    """AuthenticationError needs a response to construct; this is the shape."""
    return AuthenticationError.__new__(AuthenticationError)


def test_a_refused_key_is_reported_as_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _fake_auth_error()
    monkeypatch.setattr(check_research, "Parallel", _client_raising(error))

    code = check_research.check("some-key")

    assert code == check_research.REFUSED
    out = capsys.readouterr().out
    assert "REFUSED" in out
    # And says what it costs, because the failure is otherwise invisible.
    assert "invented" in out


def test_an_unreachable_api_is_not_a_bad_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The distinction that matters. A timeout will pass on its own; treating
    it as a bad credential sends somebody hunting for a new key."""
    monkeypatch.setattr(
        check_research, "Parallel", _client_raising(ParallelError("timed out"))
    )

    code = check_research.check("some-key")

    assert code == check_research.UNKNOWN
    assert code != check_research.REFUSED
    out = capsys.readouterr().out
    assert "COULD NOT TELL" in out
    assert "Not proof the key is wrong" in out


def test_a_working_key_prints_what_came_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proof by output rather than a tick. A green tick beside an unverified
    string is what hid the Vertex model for a day."""
    monkeypatch.setattr(
        check_research,
        "Parallel",
        _client_returning(["https://example.com/mirrors", "https://shop.example/m"]),
    )

    code = check_research.check("some-key")

    assert code == 0
    out = capsys.readouterr().out
    assert "WORKS" in out
    assert "https://example.com/mirrors" in out


def test_a_key_that_works_but_finds_nothing_still_works(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty result list authenticates fine. Reading it as a failure would
    send somebody replacing a key that was never the problem."""
    monkeypatch.setattr(check_research, "Parallel", _client_returning([]))

    code = check_research.check("some-key")

    assert code == 0
    assert "still a working key" in capsys.readouterr().out


def test_the_key_is_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A terminal may be on a screen share."""
    secret = "pk-live-do-not-print-me"
    monkeypatch.setattr(check_research, "Parallel", _client_raising(_fake_auth_error()))

    _ = check_research.check(secret)

    assert secret not in capsys.readouterr().out


def test_no_key_anywhere_asks_for_one_rather_than_calling(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)

    code = check_research.main([])

    assert code == 2
    assert "not set in this shell" in capsys.readouterr().out
