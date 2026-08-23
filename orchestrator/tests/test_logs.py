# Log payloads are heterogeneous by nature; see the same note in logs.py.
# pyright: reportExplicitAny=false, reportAny=false
"""The log line is what is left when nobody was watching.

No emulator needed: a formatter is a pure function from a record to a string,
and these feed it records directly.
"""

import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from orchestrator.logs import JsonFormatter, configure_logging
from orchestrator.settings import LogFormat, Settings


def _record(
    message: str = "hello",
    *,
    level: int = logging.INFO,
    extra: dict[str, Any] | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="orchestrator.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def _emit(record: logging.LogRecord) -> dict[str, Any]:
    return json.loads(JsonFormatter().format(record))


# --------------------------------------------------------------------------- #
# The shape Cloud Logging reads
# --------------------------------------------------------------------------- #


def test_a_line_is_one_json_object() -> None:
    line = JsonFormatter().format(_record())

    assert "\n" not in line, "a multi-line record is several log entries"
    assert json.loads(line)["message"] == "hello"


def test_the_level_is_called_severity() -> None:
    """Cloud Logging reads ``severity`` and ignores ``level`` completely.

    Get this wrong and every line arrives as INFO — including the stack traces —
    so the console's severity filter, the first thing anyone reaches for, shows
    nothing.
    """
    assert _emit(_record(level=logging.WARNING))["severity"] == "WARNING"
    assert _emit(_record(level=logging.ERROR))["severity"] == "ERROR"
    assert "level" not in _emit(_record())


def test_extra_fields_survive() -> None:
    """The whole point. Before this, every ``extra=`` in the codebase was
    silently discarded by the default handler."""
    payload = _emit(_record(extra={"project_id": "projA", "messages_sent": 3}))

    assert payload["project_id"] == "projA"
    assert payload["messages_sent"] == 3


def test_logging_internals_do_not_leak_into_the_payload() -> None:
    """Filtered by subtraction, so a new ``extra`` key needs no registration —
    but that means the builtin list has to actually be complete."""
    payload = _emit(_record())

    assert not {"msg", "args", "levelno", "pathname", "thread"} & set(payload)


def test_an_exception_carries_its_traceback() -> None:
    try:
        raise RuntimeError("supplier record vanished")
    except RuntimeError:
        record = _record("tick failed", level=logging.ERROR)
        record.exc_info = sys.exc_info()

    payload = _emit(record)

    assert "RuntimeError: supplier record vanished" in payload["exception"]
    assert "\n" not in JsonFormatter().format(record), "still one line"


def test_a_value_json_cannot_encode_does_not_kill_the_log() -> None:
    """A log line is what you have left when everything else has gone wrong.

    It must not be the thing that throws — a formatter that raises on an
    unexpected datetime would lose the very entry explaining the failure.
    """
    payload = _emit(_record(extra={"clock": object()}))

    assert "clock" in payload
    assert isinstance(payload["clock"], str)


def test_format_arguments_are_interpolated() -> None:
    record = logging.LogRecord(
        name="orchestrator.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="sent %d emails",
        args=(4,),
        exc_info=None,
    )

    assert json.loads(JsonFormatter().format(record))["message"] == "sent 4 emails"


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """configure_logging replaces the root handlers, which is pytest's too."""
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = saved, level


def _settings(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)  # pyright: ignore[reportCallIssue]


def test_json_is_the_default() -> None:
    """Deliberately, even though it is worse to read locally. A format only
    exercised in production is a format nobody has tested."""
    assert _settings().log_format is LogFormat.JSON


def test_configuring_installs_exactly_one_handler() -> None:
    configure_logging(_settings())
    configure_logging(_settings())

    assert len(logging.getLogger().handlers) == 1, "idempotent, not cumulative"
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


def test_text_mode_is_available_for_working_at_a_terminal() -> None:
    configure_logging(_settings(log_format=LogFormat.TEXT))

    formatter = logging.getLogger().handlers[0].formatter
    assert not isinstance(formatter, JsonFormatter)


def test_uvicorns_own_handlers_are_taken_away() -> None:
    """Otherwise the access log — the highest-volume stream — is the one
    unstructured thing in the output."""
    access = logging.getLogger("uvicorn.access")
    access.handlers = [logging.StreamHandler()]
    access.propagate = False

    configure_logging(_settings())

    assert access.handlers == []
    assert access.propagate
