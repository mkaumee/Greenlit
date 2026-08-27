#!/usr/bin/env python
"""Send one real email, then read the reply. The last unproven claim.

``GmailTransport`` has been complete and green for weeks — against a fake. This
is the first thing that hands a message to Google, and it exists separately
from the tick on purpose: if a scope, a header or a label is wrong, you find
out from one email you sent deliberately, rather than from four the deployed
loop sent to suppliers while you were not watching.

It uses the *same* transport the tick uses. A parallel implementation here
would prove nothing about the product.

    uv run python scripts/gmail_smoke.py --to seller@example.com
    # reply by hand from that mailbox, then:
    uv run python scripts/gmail_smoke.py --poll

Needs a bootstrapped refresh token — see docs/oauth-runbook.md. That bootstrap
has to run on a machine with a browser; it will not work in Cloud Shell.
"""

# argparse Namespace attributes are Any by nature.
# pyright: reportAny=false

import argparse
import asyncio
import json
import sys
from pathlib import Path

from orchestrator.gmail import (
    GmailTransport,
    build_credentials,
    client_credentials,
    token_store_for,
)
from orchestrator.settings import Settings, TokenBackend

SUBJECT = "Greenlit — transport check"
BODY = """Hello,

This is an automated check that our procurement agent can send and read mail.
No reply is needed for it to count as delivered, but replying to this message
is what proves the other half.

— Greenlit
"""

TRACE = Path(".secrets/last_smoke.json")
"""Where the sent ids go so --poll can recognise the reply.

Gitignored. It is a scratch file for a hand-run check, not state the product
depends on — the tick keeps its ids in Firestore like everything else.
"""


def _transport(settings: Settings) -> GmailTransport:
    client_id, client_secret = client_credentials(settings)
    credentials = build_credentials(token_store_for(settings), client_id, client_secret)
    return GmailTransport.from_credentials(credentials, settings)


def _project_for_hint(settings: Settings) -> str:
    """What to put after ``CINEMA_GCP_PROJECT=`` in a suggested command.

    ``gcp_project`` defaults to ``demo-cinema``, which is the emulator's name
    and belongs to nobody. Echoing it back produces a command that fails with
    PROJECT_NOT_FOUND — the same shape of unhelpful advice that has already
    cost an afternoon once in this repo. When it is still the default, hand
    over a shell substitution that resolves to whatever gcloud is pointed at
    instead of a value we are only guessing.
    """
    if settings.gcp_project and settings.gcp_project != "demo-cinema":
        return settings.gcp_project
    return "$(gcloud config get-value project)"


def _preflight(settings: Settings, *, polling: bool) -> str:
    """Why this cannot run, or "" if it can."""
    if not all(client_credentials(settings)):
        return (
            "no OAuth client id and secret. They are read from "
            f"{settings.oauth_client_secrets} if it is there, or from "
            "CINEMA_OAUTH_CLIENT_ID / CINEMA_OAUTH_CLIENT_SECRET. Without them "
            "the refresh token cannot be exchanged for an access token."
        )
    if (
        settings.token_backend is TokenBackend.FILE
        and not settings.refresh_token_path.exists()
    ):
        # Two causes, and this cannot tell them apart — so it must not pick
        # one. Saying "run the bootstrap" to somebody who has already done it
        # sends them back through the whole OAuth consent flow for nothing.
        # The suggested command repeats what was actually asked for. Printing
        # `TO=...` at somebody who ran `POLL=1` makes them read the fix as
        # advice for a different problem.
        again = "POLL=1" if polling else "TO=seller@example.com"
        return (
            f"no refresh token at {settings.refresh_token_path}, and "
            "CINEMA_TOKEN_BACKEND is 'file' so that is the only place looked.\n\n"
            "  If you bootstrapped into Secret Manager, point this at it:\n"
            "    CINEMA_TOKEN_BACKEND=secret-manager "
            f"CINEMA_GCP_PROJECT={_project_for_hint(settings)} "
            f"make gmail-smoke {again}\n\n"
            "  If the bootstrap genuinely has not run yet:\n"
            "    uv run python scripts/oauth_bootstrap.py"
        )
    return ""


async def send(settings: Settings, to: str, assume_yes: bool) -> int:
    print(f"\n  from     {settings.agent_display_name} <{settings.agent_email}>")
    print(f"  to       {to}")
    print(f"  subject  {SUBJECT}")
    print(f"  backend  {settings.token_backend.value}\n")

    # This sends real mail to a real person's inbox. One confirmation is
    # cheap; an agent that surprises someone is not.
    #
    # No stdin — piped, or run from CI — is a refusal, not a crash and not an
    # assumed yes. The only way to send without a human present is to say so
    # with --yes.
    if not assume_yes:
        try:
            answer = input("  send it? [y/N] ").strip().lower()
        except EOFError:
            print("\n  nothing sent: no terminal to confirm at. Pass --yes.")
            return 1
        if answer not in {"y", "yes"}:
            print("  nothing sent.")
            return 1

    sent = await _transport(settings).send(to=to, subject=SUBJECT, body=BODY)

    print("\n  sent.")
    print(f"    message_id         {sent.message_id}")
    print(f"    rfc822_message_id  {sent.rfc822_message_id}")
    print(f"    thread_id          {sent.thread_id}")
    print(
        "\n  Those three are exactly what a negotiation record stores. Gmail's\n"
        "  id keys the stored message so a killed tick redelivering is a no-op;\n"
        "  the RFC-822 header is what a reply must quote to thread."
    )

    TRACE.parent.mkdir(parents=True, exist_ok=True)
    _ = TRACE.write_text(
        json.dumps(
            {
                "thread_id": sent.thread_id,
                "rfc822_message_id": sent.rfc822_message_id,
                "to": to,
            },
            indent=2,
        )
    )
    print(f"\n  Reply from {to}, then:  make gmail-smoke POLL=1")
    return 0


async def poll(settings: Settings) -> int:
    expected: dict[str, str] = {}
    if TRACE.exists():
        expected = json.loads(TRACE.read_text())

    # Only the conversation this script started. Without naming it, polling a
    # mailbox reads and consumes whatever else is unread in it — which is how
    # a hundred unrelated messages got marked read the first time this ran
    # against an account that was also somebody's personal inbox.
    ours = expected.get("thread_id", "")
    if not ours:
        print("\n  No record of a sent message, so there is no thread to check.")
        print("  Send one first:  make gmail-smoke TO=someone@example.com")
        return 1

    inbound = await _transport(settings).poll(threads=frozenset({ours}))
    if not inbound:
        print("\n  Nothing unread in the thread we sent.")
        print("  Reply to that message rather than composing a new one — a")
        print("  reply in a fresh thread cannot be filed against a negotiation.")
        print("  Note the poll marks mail read, so one already read once will")
        print("  not appear again.")
        return 1

    print(f"\n  {len(inbound)} message(s):\n")
    threaded = False
    for msg in inbound:
        print(f"    from       {msg.from_email}")
        print(f"    subject    {msg.subject}")
        print(f"    thread_id  {msg.thread_id}")
        if msg.has_attachments:
            print(f"    files      {', '.join(msg.attachment_filenames)}")
        print(f"    body       {msg.body.strip().splitlines()[:1]}")
        if expected and msg.thread_id == expected.get("thread_id"):
            threaded = True
            print("    ↳ threaded to the message we sent")
        print()

    if not threaded:
        # The failure worth naming. A reply that arrives but lands in its own
        # thread would be filed against no negotiation, and from the UI that is
        # indistinguishable from a supplier who never answered.
        print("  WARNING: nothing threaded to the message we sent.")
        print(f"    ours:  {expected.get('thread_id')}")
        print("    A reply in a new thread cannot be filed against a")
        print("    negotiation — it looks exactly like silence.")
        return 1

    print("  Round-trip proven: sent, delivered, replied, read back, threaded.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--to", default="", help="one recipient address")
    _ = parser.add_argument("--poll", action="store_true", help="read replies instead")
    _ = parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args()

    settings = Settings()
    if blocked := _preflight(settings, polling=bool(args.poll)):
        print(f"\n  Cannot run: {blocked}")
        return 2

    if args.poll:
        return asyncio.run(poll(settings))

    to: str = str(args.to).strip()
    if not to or "," in to or " " in to:
        parser.print_usage()
        print("\n  --to takes exactly one address. This sends real email.")
        return 2

    return asyncio.run(send(settings, to, bool(args.yes)))


if __name__ == "__main__":
    sys.exit(main())
