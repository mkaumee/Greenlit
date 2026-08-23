# A log payload is heterogeneous by nature — whatever a caller put in `extra`.
# Typing it as anything narrower than Any would be a lie about what arrives.
# pyright: reportExplicitAny=false, reportAny=false
"""One JSON object per line, because that is what Cloud Logging reads.

Until this existed there was no logging configuration in the repository at all.
Every ``log.info("...", extra={...})` in ``app.py``, ``approvals.py`` and
``auth.py`` was already carrying structured fields, and every one of them was
being dropped on the floor by the default handler.

That is survivable while a human is watching a terminal. It stops being
survivable the moment Cloud Scheduler is ticking every minute and the only
record of what happened at 3am is the log.

## The two keys that actually matter

**``severity``, not ``level``.** Cloud Logging looks for ``severity`` in a
structured payload and ignores ``level`` entirely. Get this wrong and every
line — including the stack traces — arrives as INFO, so the console's severity
filter, which is the first thing anyone reaches for, silently shows nothing.

**``message``.** The one field the log viewer renders in the collapsed row.
Anything else is only visible after expanding the entry.

Everything else on the record is emitted alongside them as a top-level field,
which is what makes ``jq 'select(.messages_sent > 0)'`` work on a day's worth of
ticks.

## Not here yet

Trace correlation (``logging.googleapis.com/trace``) would group a request's
lines together in the console. It needs a deployed service and an incoming
``X-Cloud-Trace-Context`` to be worth anything, so it waits for the deploy.
"""

import json
import logging
from typing import Any, override

from orchestrator.settings import LogFormat, Settings

_SEVERITY: dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}
"""Python's level names map onto Cloud Logging's almost exactly. ``WARN`` and
``FATAL`` are the two that do not, and neither is a name this code uses."""

_BUILTIN: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)
"""Attributes ``logging`` puts on every record.

Anything on a record that is *not* in this set arrived through ``extra=`` and is
therefore something a caller deliberately wanted in the log. Filtering by
subtraction rather than by an allow-list means a new ``extra`` key shows up
without anyone having to remember to register it here.
"""


class JsonFormatter(logging.Formatter):
    """Renders a record as a single line of JSON."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": _SEVERITY.get(record.levelno, record.levelname),
            "message": record.getMessage(),
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "logger": record.name,
            **{
                key: value
                for key, value in record.__dict__.items()
                if key not in _BUILTIN and not key.startswith("_")
            },
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # ``default=str`` rather than letting a stray datetime or enum raise.
        # A log line is the thing you have left when everything else has gone
        # wrong, and it must not be the thing that throws.
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Point every logger in the process at one handler. Idempotent.

    Including uvicorn's. It installs its own handlers on import, so without
    clearing them the access log is the one unstructured thing in the stream —
    and it is also the highest-volume one.

    Called from both services' lifespans rather than at import, so that
    importing ``orchestrator.app`` in a test does not reconfigure the root
    logger out from under pytest's capture.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if settings.log_format is LogFormat.JSON
        else logging.Formatter("%(levelname)-8s %(name)s  %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
