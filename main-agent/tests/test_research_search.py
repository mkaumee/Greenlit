"""A web search that fails has to say so somewhere.

The failure this covers is the quietest one in the system. `search_web`
catches ParallelError and hands the model "web search failed, retry" — which is
right, it is the only thing a model can act on. But it returned that and logged
nothing, so an unusable key produced a model answering from memory: a reference
price band and supplier URLs with nothing behind them, indistinguishable on
screen from researched ones.

Nothing errored. Nothing logged. In a system whose whole claim is that it keeps
the URLs it got its numbers from.
"""

import logging
from typing import final

import pytest
from parallel import ParallelError

from main_agent.research import researcher


@final
class _Failing:
    """An AsyncParallel whose search always refuses, as a bad key would."""

    async def __aenter__(self) -> _Failing:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, **_kwargs: object) -> object:
        raise ParallelError("401 unauthorized")


def _failing_client(*_args: object, **_kwargs: object) -> _Failing:
    return _Failing()


async def test_a_failed_search_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(researcher, "AsyncParallel", _failing_client)

    with caplog.at_level(logging.WARNING, logger="main_agent.research"):
        result = await researcher.search_web(
            objective="what a prop mirror costs in Malaysia",
            search_queries=["prop mirror price"],
        )

    assert any("parallel search failed" in r.getMessage() for r in caplog.records)
    # And the traceback, so the reason is in the record rather than just the fact.
    assert any(r.exc_info for r in caplog.records)
    # The model still gets something it can act on.
    assert "error" in result


async def test_the_model_is_still_told_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two audiences, two messages. Logging the operator's version must not
    change what the model is handed."""
    monkeypatch.setattr(researcher, "AsyncParallel", _failing_client)

    result = await researcher.search_web(
        objective="what a prop mirror costs",
        search_queries=["prop mirror price"],
    )

    assert "Retry" in str(result["error"])


async def test_an_empty_objective_is_refused_without_calling_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guards above the try block. A search with nothing to search for is
    a bug in the caller, not a network failure, and must not be logged as one."""
    called = False

    def _never(*_a: object, **_k: object) -> object:
        nonlocal called
        called = True
        return _Failing()

    monkeypatch.setattr(researcher, "AsyncParallel", _never)

    result = await researcher.search_web(objective="   ", search_queries=["x"])

    assert "error" in result
    assert not called
