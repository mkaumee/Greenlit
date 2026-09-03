#!/usr/bin/env python3
"""Say who owns a production, so a browser can see it.

    uv run python scripts/claim_project.py --project-id my-gcp-project \
        --pid demo --owner-uid 4MPabWIYYEWtStOZyJU7AiATFl63

``firestore.rules`` matches projects on ``owner_uid``, and ``useProjects()``
queries for exactly that. A project with no owner is therefore unreachable from
every browser rather than visible to all of them — the safe default, and a
completely silent one: the panel renders an empty state that looks identical to
a deployment with nothing in it.

That is where the deployed demo project ended up, because ``seed_project.py``
takes ``--owner-uid`` and it was not passed. This fixes it in place rather than
by reseeding, which would layer a second screenplay onto a project that already
has negotiations against real suppliers.

## Why a script and not a route

Handing over a production is not something the agent should be able to do. This
runs under a *person's* application-default credentials — the project owner in
Cloud Shell — not the agent service account, which is conditioned to the
``(default)`` database and has no business reassigning who a production belongs
to. When ``POST /projects`` lands on ``cinema-api`` a producer will own what
they create and this becomes a repair tool, which is all it is now.

## Against the emulator

``FIRESTORE_EMULATOR_HOST=127.0.0.1:8080`` and it talks to the emulator. Same
code, and worth doing once before running it against a deployment.
"""

# argparse Namespace attributes are Any by nature; the values are str()'d at
# the one place they are used, which is the check that matters. The Firestore
# client ships no types for `update`, same as in repository.py.
# pyright: reportAny=false, reportUnknownMemberType=false
import argparse
import asyncio
import os
import sys

from google.api_core import exceptions as gcloud_exceptions
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.repository import FirestoreRepository


async def claim(
    client: AsyncClient, pid: str, owner_uid: str, *, force: bool
) -> tuple[int, str]:
    """Returns an exit code and what to say about it."""
    repo = FirestoreRepository(client)
    record = await repo.get_project(pid)
    if record is None:
        return 1, f"No project '{pid}'. Check the id — nothing was written."

    if record.owner_uid == owner_uid:
        return 0, f"'{pid}' ({record.title}) is already owned by {owner_uid}."

    # Reassigning somebody else's production is the one damaging thing this can
    # do, and it does it in one line with no undo. An empty owner is the case
    # this script exists for and needs no ceremony; a different non-empty owner
    # has to be said out loud.
    if record.owner_uid and not force:
        return 2, (
            f"'{pid}' is already owned by {record.owner_uid}.\n"
            f"  Handing it to {owner_uid} would take it away from them and\n"
            "  they would see an empty panel with no explanation.\n"
            "  If that is genuinely what you want:  --force"
        )

    _ = await (
        client.collection("projects").document(pid).update({"owner_uid": owner_uid})
    )
    return 0, (
        f"'{pid}' ({record.title}) now belongs to {owner_uid}.\n"
        "  That account sees it on next snapshot — no reload needed."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--project-id", default="", help="the Google Cloud project holding Firestore"
    )
    _ = parser.add_argument("--pid", required=True, help="the production's document id")
    _ = parser.add_argument(
        "--owner-uid", required=True, help="the Firebase uid to hand it to"
    )
    _ = parser.add_argument(
        "--force", action="store_true", help="take it from its current owner"
    )
    args = parser.parse_args(argv)

    emulated = bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
    project: str = str(args.project_id) or os.environ.get("CINEMA_GCP_PROJECT", "")
    if not project and not emulated:
        print("--project-id is required against a real deployment.")
        print("  Otherwise the client guesses one from your gcloud config, and")
        print("  writing a stranger's Firestore is not a guess worth making.")
        return 2

    client = AsyncClient(project=project or "demo-cinema")
    try:
        code, message = asyncio.run(
            claim(client, str(args.pid), str(args.owner_uid), force=bool(args.force))
        )
    except gcloud_exceptions.GoogleAPIError as cause:
        # Wrong project, no ADC, Firestore never provisioned — one error class
        # for all of them, and a traceback that never names the project.
        print(f"Firestore refused the write in '{project}': {cause}")
        print("  · no credentials?  gcloud auth application-default login")
        print("  · wrong project?   --project-id your-project-id")
        return 1
    finally:
        client.close()

    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
