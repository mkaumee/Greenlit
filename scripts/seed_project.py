#!/usr/bin/env python
"""Put a screenplay into a *deployed* project, so the panel has something on it.

``make e2e`` fills the emulator. Nothing filled the real project, so the hosted
instrument panel showed an empty table — correct, and useless to look at.

This drives the deployed service's own HTTP API rather than writing to
Firestore. Three endpoints, the same three a producer's browser will call in
Phase 6::

    POST /projects
    POST /projects/{id}/script
    POST /projects/{id}/items/confirm

Writing to Firestore directly would be a second way to create a project,
diverging from the real one the moment either changes, and it would prove
nothing about whether the deployment works. Going through the API means a
successful seed is also evidence the deployed service can do the front half of
its job.

Nothing here ticks. Cloud Scheduler is already calling ``/tick`` every minute,
so once the items exist the deployed loop researches them and opens
negotiations on its own — and the panel fills in with nobody touching it.

    uv run python scripts/seed_project.py --project-id your-project-id --dry-run
    uv run python scripts/seed_project.py --project-id your-project-id
"""

# argparse Namespace attributes and httpx .json() are Any by nature, so the
# Any-flavoured warnings here are about those libraries rather than this code.
# Same suppression, for the same reason, as grant_producer.py and run_e2e.py.
# pyright: reportAny=false

import argparse
import subprocess
import sys
from datetime import UTC, datetime

import httpx
from _fixture import FLOOR, SCREENPLAY

REGION = "us-central1"
TICK_SERVICE = "cinema-tick"

SIM_START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
"""Simulated time the project starts at. Matches the daily check, so a
screenshot of one is legible next to the other."""


def _gcloud(*args: str) -> str:
    """Run gcloud and return stdout, or "" if it failed.

    stderr is captured separately rather than merged. Folding gcloud's warnings
    into a value that ends up in an HTTP header is how this project previously
    produced `curl: (43)` and spent an afternoon reading it as a permissions
    problem.
    """
    done = subprocess.run(
        ["gcloud", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def discover_url(project_id: str) -> str:
    return _gcloud(
        "run",
        "services",
        "describe",
        TICK_SERVICE,
        f"--region={REGION}",
        f"--project={project_id}",
        "--format=value(status.url)",
    )


def identity_token() -> str:
    """The tick service is private. This is how verify_deploy.sh reaches it."""
    return _gcloud("auth", "print-identity-token")


def preflight(client: httpx.Client, target: str, allow_live_mail: bool) -> int:
    """Refuse to seed a deployment that would email real people.

    MAIL_BACKEND defaults to `memory`, so ticking posts into an in-memory
    mailbox. That default is the only thing standing between a seeded
    screenplay and the agent writing to whatever addresses research invents,
    and it is worth *checking* rather than assuming — /health reports it
    precisely so this question has an answer.
    """
    reply = client.get(f"{target}/health")
    if reply.status_code != 200:
        print(f"  /health returned {reply.status_code}: {reply.text[:200]}")
        return 1

    health: dict[str, object] = reply.json()
    mail = health.get("mail_backend")
    brain = health.get("brain_backend")
    print(f"  mail_backend  {mail}")
    print(f"  brain_backend {brain}")

    if brain == "scripted":
        # Not fatal, but not a detail to discover from a screenshot either. A
        # keyword matcher writing negotiation emails looks like a working
        # system right up until somebody reads one.
        print("    note: the fake brain. Real reasoning lands when role_a merges.")

    if mail != "memory" and not allow_live_mail:
        print()
        print(f"  REFUSING: mail_backend is '{mail}', not 'memory'.")
        print("  Seeding now would have the agent email addresses invented by")
        print("  research, from a screenplay, without anyone expecting it.")
        print("  If that is genuinely what you want: --allow-live-mail")
        return 2
    return 0


def seed(client: httpx.Client, target: str, pid: str, title: str) -> int:
    created = client.post(
        f"{target}/projects",
        json={
            "project_id": pid,
            "title": title,
            "sim_start": SIM_START.isoformat(),
        },
    )
    if created.status_code != 201:
        print(f"  could not create project: {created.status_code} {created.text[:300]}")
        print()
        print(f"  If '{pid}' already exists, seeding it again would layer a second")
        print("  screenplay onto it. Pick another: --project-name demo-2")
        return 1
    print(f"  created project {pid}")

    read = client.post(
        f"{target}/projects/{pid}/script", json={"text_content": SCREENPLAY}
    )
    if read.status_code != 200:
        print(f"  script upload failed: {read.status_code} {read.text[:300]}")
        return 1

    props: list[dict[str, object]] = read.json()["props"]
    print(f"  read the script -> {len(props)} props")
    for prop in props:
        flag = "  (destroyed on camera)" if prop["consumable"] else ""
        scenes: object = prop["scenes"]
        first = str(scenes[0]) if isinstance(scenes, list) and scenes else "?"  # pyright: ignore[reportUnknownArgumentType]
        print(f"    {prop['name']!s:<12} scene {first}{flag}")

    # A producer signing off. Consumables get several, because they break.
    confirmed = client.post(
        f"{target}/projects/{pid}/items/confirm",
        json={
            "items": [
                {
                    "item_id": p["item_id"],
                    "qty": 6 if p["consumable"] else 1,
                    "floor_price": FLOOR,
                }
                for p in props
            ]
        },
    )
    if confirmed.status_code != 200:
        print(f"  confirm failed: {confirmed.status_code} {confirmed.text[:300]}")
        return 1
    print(f"  producer confirmed {len(props)} items")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--project-id", required=True, help="the GCP project")
    _ = parser.add_argument(
        "--project-name", default="demo", help="id of the project to create"
    )
    _ = parser.add_argument("--title", default="Nasi Lemak Nights")
    _ = parser.add_argument(
        "--target", default="", help="tick service URL; discovered if absent"
    )
    _ = parser.add_argument("--dry-run", action="store_true")
    _ = parser.add_argument(
        "--allow-live-mail",
        action="store_true",
        help="seed even when the deployment sends real email",
    )
    args = parser.parse_args()

    target: str = args.target or discover_url(args.project_id)
    if not target:
        print(f"could not find the {TICK_SERVICE} service in {args.project_id}.")
        print(f"  gcloud run services list --project={args.project_id}")
        return 2

    print(f"\nTarget    {target}")
    print(f"Project   {args.project_name}  ({args.title})")
    print(f"Floor     {FLOOR['currency']} {FLOOR['amount']} per item\n")

    if args.dry_run:
        print("--dry-run: nothing was written.")
        print("Would create the project, upload the screenplay, confirm every prop.")
        return 0

    token = identity_token()
    if not token:
        print("could not mint an identity token — the tick service is private.")
        print("  gcloud auth login")
        return 2

    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"}, timeout=60.0
    ) as client:
        failed = preflight(client, target, bool(args.allow_live_mail))
        if failed:
            return failed
        print()
        if seed(client, target, str(args.project_name), str(args.title)):
            return 1

    print()
    print("Seeded. Cloud Scheduler ticks every minute, so the loop will research")
    print("these items and open negotiations on its own — the panel fills in")
    print("with nobody touching the browser. Nothing is bought without a human.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
