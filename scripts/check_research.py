#!/usr/bin/env python3
"""Does the research key work? One real search, and the answer.

    PARALLEL_API_KEY=... uv run python scripts/check_research.py
    uv run python scripts/check_research.py --from-deployment --project-id my-project

The deploy preflight already makes this call, and only as part of ``make
deploy`` — which builds an image and rolls three services to answer one
question. Everything else is indirect: ``verify_deploy.sh`` reports whether the
key is a *placeholder*, not whether it *works*, and the panel shows a band's
sources only after a script, a confirmation and a tick. This is the direct ask.

## Why --from-deployment exists

The key in your shell and the key the agent holds are different things, and
this deployment has already been burnt by the difference: it ran for days with
the literal word ``your-key`` while every local check passed. ``--from-deployment``
reads what ``cinema-tick`` actually has, so a pass here is a statement about the
running system rather than about your terminal.

## Three answers, not two

A refused key and an unreachable API are different problems with different
fixes, and collapsing them into "failed" is how an outage gets mistaken for a
bad credential. Authentication errors exit 1. Anything else exits 2 and says it
could not tell.

The key itself is never printed. A terminal may be on a screen share.
"""

# argparse Namespace attributes are Any by nature; the values are str()'d where
# they are used.
# pyright: reportAny=false
import argparse
import json
import os
import subprocess
import sys

from parallel import (
    AuthenticationError,
    Parallel,
    ParallelError,
    PermissionDeniedError,
)


def _first_line(exc: Exception) -> str:
    """The exception's first line, or its class when it has no message.

    ``str(exc).splitlines()[0]`` raises IndexError on an empty message — which
    a transport error can absolutely have, and which would crash the one script
    whose entire job is reporting a failure clearly.
    """
    lines = str(exc).strip().splitlines()
    return lines[0][:200] if lines else type(exc).__name__


OBJECTIVE = "current market price of a prop mirror for a film shoot"
QUERIES = ["prop mirror price", "film prop mirror cost"]

REFUSED = 1
"""The key is wrong. A different problem from the API being unreachable."""

UNKNOWN = 2
"""Could not tell. Not the same as a failure, and must not read as one."""


def key_from_deployment(project_id: str, service: str, region: str) -> str:
    """The key the agent actually holds, off the deployed service.

    Read as JSON and parsed rather than grepped. An earlier attempt at this
    used ``--format='value(...env)'`` piped through ``tr ',' '\\n'``, and since
    the env is a list of dicts that split every record in half and reported
    only that the variable existed.
    """
    result = subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            f"--region={region}",
            f"--project={project_id}",
            "--format=json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Could not read {service} in {project_id}:")
        print(f"  {result.stderr.strip().splitlines()[-1] if result.stderr else ''}")
        return ""

    try:
        spec = json.loads(result.stdout)["spec"]["template"]["spec"]["containers"][0]
        for entry in spec.get("env", []):
            if entry.get("name") == "PARALLEL_API_KEY":
                return str(entry.get("value", ""))
    except KeyError, IndexError, ValueError, TypeError:
        print(f"{service} did not describe the way this expects.")
        return ""

    print(f"{service} has no PARALLEL_API_KEY set at all.")
    return ""


def check(api_key: str) -> int:
    """Make the call the researcher makes. Returns an exit code."""
    try:
        client = Parallel(api_key=api_key)
        response = client.search(
            objective=OBJECTIVE,
            search_queries=QUERIES,
            mode="basic",
            max_chars_total=2000,
        )
    except (AuthenticationError, PermissionDeniedError) as exc:
        print("REFUSED — Parallel would not accept that key.")
        print(f"  {type(exc).__name__}: {_first_line(exc)}")
        print()
        print("  Every research call fails this way, and none of them say so on")
        print("  screen: the model is told 'web search failed' and answers from")
        print("  memory, so its price bands and supplier URLs are invented.")
        return REFUSED
    except ParallelError as exc:
        # Deliberately not a failure. A timeout or an outage is somebody else's
        # problem and will pass on its own; treating it as a bad key sends you
        # looking for a new one.
        print("COULD NOT TELL — the call did not come back.")
        print(f"  {type(exc).__name__}: {_first_line(exc)}")
        print()
        print("  Not proof the key is wrong. Try again in a minute.")
        return UNKNOWN

    results = list(response.results)
    print(f"WORKS — Parallel returned {len(results)} result(s).")
    for item in results[:5]:
        print(f"  {item.url}")
    if not results:
        # A key that authenticates and finds nothing is still a working key,
        # and saying so beats an empty list that reads like a failure.
        print("  (No results for that query, which is still a working key.)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--from-deployment",
        action="store_true",
        help="test the key the deployed tick service holds, not your shell's",
    )
    _ = parser.add_argument("--project-id", default="", help="for --from-deployment")
    _ = parser.add_argument("--service", default="cinema-tick")
    _ = parser.add_argument("--region", default="us-central1")
    args = parser.parse_args(argv)

    if args.from_deployment:
        project_id = str(args.project_id) or os.environ.get("CINEMA_GCP_PROJECT", "")
        if not project_id:
            print("--from-deployment needs --project-id.")
            return 2
        print(f"Reading the key {args.service} holds in {project_id}…")
        api_key = key_from_deployment(project_id, str(args.service), str(args.region))
    else:
        api_key = os.environ.get("PARALLEL_API_KEY", "")
        if not api_key:
            print("PARALLEL_API_KEY is not set in this shell.")
            print("  Test the deployed one instead:")
            print(f"    {sys.argv[0]} --from-deployment --project-id your-project")
            return 2

    if not api_key:
        return UNKNOWN
    return check(api_key)


if __name__ == "__main__":
    sys.exit(main())
