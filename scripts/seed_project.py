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
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from _fixture import FLOOR, SCREENPLAY

REGION = "us-central1"
TICK_SERVICE = "cinema-tick"

SIM_START = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
"""Simulated time the project starts at. Matches the daily check, so a
screenshot of one is legible next to the other."""


def _outside_venv() -> dict[str, str]:
    """The environment with this venv removed.

    gcloud is itself a Python program and finds its interpreter from the
    environment. Run from inside `uv run`, it picks up the workspace venv —
    Python 3.14, which gcloud does not support — and fails to start. The
    symptom is not a Python error but a gcloud that simply exits non-zero, so
    from the caller it looks exactly like "that service does not exist".

    Everything that shells out to gcloud from Python needs this. The shell
    scripts do not, which is why `verify_deploy.sh` found the service in the
    same terminal where this script could not.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", "")
    _ = env.pop("PYTHONHOME", None)
    _ = env.pop("PYTHONPATH", None)
    if venv:
        bin_dir = str(Path(venv) / "bin")
        env["PATH"] = os.pathsep.join(
            part for part in env.get("PATH", "").split(os.pathsep) if part != bin_dir
        )
    return env


def _gcloud(*args: str) -> tuple[str, str]:
    """Run gcloud. Returns (stdout, error) — exactly one of them is non-empty.

    The error is *returned* rather than swallowed. An earlier version of this
    helper returned "" on any failure, so a gcloud that could not even start
    was reported to the user as "could not find the cinema-tick service" —
    confidently, and about a service that was running. This project has now
    made that same mistake three times (a discarded `addfirebase` stderr, a
    `curl: (43)` from a token merged with a warning, and this): a helper that
    hides why something failed produces guessing, and the guess reads as fact.

    stderr is captured separately rather than merged into stdout, so a value
    used later — a URL, a token — can never carry a warning line into it.
    """
    done = subprocess.run(
        ["gcloud", *args],
        capture_output=True,
        text=True,
        check=False,
        env=_outside_venv(),
    )
    if done.returncode == 0:
        return done.stdout.strip(), ""
    detail = done.stderr.strip() or f"gcloud exited {done.returncode}"
    return "", detail


def discover_url(project_id: str) -> tuple[str, str]:
    return _gcloud(
        "run",
        "services",
        "describe",
        TICK_SERVICE,
        f"--region={REGION}",
        f"--project={project_id}",
        "--format=value(status.url)",
    )


def identity_token() -> tuple[str, str]:
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


def seed(
    client: httpx.Client, target: str, pid: str, title: str, owner_uid: str
) -> int:
    created = client.post(
        f"{target}/projects",
        json={
            "project_id": pid,
            "title": title,
            "sim_start": SIM_START.isoformat(),
            "owner_uid": owner_uid,
        },
    )
    if created.status_code != 201:
        print(f"  could not create project: {created.status_code} {created.text[:300]}")
        print()
        print(f"  If '{pid}' already exists, seeding it again would layer a second")
        print("  screenplay onto it. Pick another: --project-name demo-2")
        return 1
    print(f"  created project {pid}")
    if not owner_uid:
        print("  ! no --owner-uid, so no browser will be able to see it.")
        print("    firestore.rules matches projects on owner_uid, so an")
        print("    unowned project is unreachable rather than public — which")
        print("    is the safe default and probably not what you wanted here.")
        print("    Your uid is on the Users tab of Firebase Authentication.")

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
        "--owner-uid",
        default="",
        help="Firebase uid of the producer who will see this in the panel",
    )
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

    target: str = args.target
    if not target:
        target, why = discover_url(args.project_id)
        if not target:
            print(f"could not reach the {TICK_SERVICE} service in {args.project_id}.")
            print(f"  gcloud said: {why}")
            print(
                f"  check it exists: gcloud run services list --project={args.project_id}"
            )
            print("  or skip discovery entirely: --target https://...")
            return 2

    print(f"\nTarget    {target}")
    print(f"Project   {args.project_name}  ({args.title})")
    print(f"Floor     {FLOOR['currency']} {FLOOR['amount']} per item\n")

    if args.dry_run:
        print("--dry-run: nothing was written.")
        print("Would create the project, upload the screenplay, confirm every prop.")
        return 0

    token, why = identity_token()
    if not token:
        print("could not mint an identity token — the tick service is private.")
        print(f"  gcloud said: {why}")
        print("  gcloud auth login")
        return 2

    with httpx.Client(
        headers={"Authorization": f"Bearer {token}"}, timeout=60.0
    ) as client:
        failed = preflight(client, target, bool(args.allow_live_mail))
        if failed:
            return failed
        print()
        if seed(
            client,
            target,
            str(args.project_name),
            str(args.title),
            str(args.owner_uid),
        ):
            return 1

    print()
    print("Seeded. Cloud Scheduler ticks every minute, so the loop will research")
    print("these items and open negotiations on its own — the panel fills in")
    print("with nobody touching the browser. Nothing is bought without a human.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
