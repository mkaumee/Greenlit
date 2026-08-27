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


def _trace() -> dict[str, str]:
    if not TRACE.exists():
        return {}
    loaded: dict[str, str] = json.loads(TRACE.read_text())
    return loaded


def _sent_thread() -> str:
    """The conversation this script started, or "" if it has not sent one."""
    return _trace().get("thread_id", "")


async def rearm(settings: Settings) -> int:
    """Put UNREAD back on the replies in our thread, so POLL can prove itself.

    Proving ``poll`` returns a live reply needs an unread reply, and the two we
    have were consumed long ago. Asking for another one, three times, did not
    work — it depends on somebody replying inside the thread *and* not opening
    their own inbox afterwards, and opening it is the natural thing to do.

    So re-arm what we already own. Touches only the conversation this script
    started, never our own outbound, and the next poll clears the label again.
    """
    ours = _sent_thread()
    if not ours:
        print("\n  No record of a sent message, so there is nothing to re-arm.")
        print("  Send one first:  make gmail-smoke TO=someone@example.com")
        return 1

    rearmed = await _transport(settings).restore_unread(ours)
    if not rearmed:
        print(f"\n  Nothing to re-arm in thread {ours}.")
        print("  Either it holds only our own outbound, or it no longer exists.")
        return 1

    print(f"\n  Marked {len(rearmed)} message(s) unread in thread {ours}:")
    for message_id in rearmed:
        print(f"    {message_id}")
    print("\n  Now prove the last link:  make gmail-smoke POLL=1")
    print("  That will read them and clear the label again.")
    return 0


async def find(settings: Settings, address: str) -> int:
    """Everything from one sender, including spam and trash.

    The question neither ``inspect`` nor ``recent`` can answer. Gmail defaults
    ``includeSpamTrash`` to false, so a filtered reply is sitting somewhere no
    listing we run can see — and ``threads.get``, which is the loop's only read
    path, takes no such parameter at all. If a reply turns up here carrying
    SPAM, then the agent is structurally blind to filtered mail and that is a
    product bug rather than a mailbox accident.

    Reads nothing and marks nothing.
    """
    who = address or _trace().get("to", "")
    if not who:
        print("\n  No address to look for, and none recorded from a send.")
        print("  Name one:  make gmail-smoke FIND=seller@example.com")
        return 1

    ours = _sent_thread()
    found = await _transport(settings).search(
        f"from:{who}", limit=25, include_spam_trash=True
    )
    if not found:
        print(f"\n  Nothing at all from {who}, spam and trash included.")
        print("  It never reached this mailbox: check the reply went to")
        print(f"  {settings.agent_email}, and that it was actually sent.")
        return 1

    print(f"\n  {len(found)} message(s) from {who}, oldest first:\n")
    for message in found:
        print(f"    thread {message.inbound.thread_id}", end="")
        print("  << ours" if message.inbound.thread_id == ours else "")
        print(f"    labels {' '.join(sorted(message.labels)) or '(none)'}")
        print(f"           {message.inbound.subject}")
        print()

    filtered = [m for m in found if "SPAM" in m.labels or "TRASH" in m.labels]
    in_thread = [m for m in found if m.inbound.thread_id == ours]

    if filtered:
        print("  Some of that is in SPAM or TRASH. Gmail hides those from every")
        print("  listing by default, and threads.get takes no option to include")
        print("  them — so if a supplier gets filtered, the loop may never see")
        print("  the reply and the panel will show silence.")
    if not in_thread:
        print(f"  None of it is in our thread ({ours}), so the loop could not")
        print("  file it against a negotiation even if it could see it.")
    return 0 if in_thread and not filtered else 1


async def recent(settings: Settings) -> int:
    """Unread mail across the whole mailbox, with the thread each landed in.

    The question ``inspect`` cannot answer: a reply that is not in our thread
    is somewhere, and knowing *where* is the difference between "Gmail has not
    delivered it yet" and "it started its own conversation and the loop will
    never see it". The second is the failure that matters, because from the
    panel it is indistinguishable from a supplier who never wrote back.

    Uses the transport's read-only sample, so it marks nothing. It is a sample:
    one page, oldest first, and it says so rather than implying completeness.
    """
    ours = _sent_thread()
    unread = await _transport(settings).poll()
    if not unread:
        print("\n  Nothing unread anywhere in the mailbox.")
        print("  If you have just replied, Gmail may not have delivered it yet.")
        return 1

    print(f"\n  {len(unread)} unread message(s), one page — a sample.")
    print("  Oldest first, so the newest thing in the mailbox is the last line.\n")
    matched = False
    for message in unread:
        mine = message.thread_id == ours
        matched = matched or mine
        print(f"    {'>> OURS' if mine else '       '}  {message.from_email}")
        print(f"              {message.subject}")
        print(f"              thread {message.thread_id}")
        print()

    if matched:
        print("  A reply is unread in our thread. POLL=1 will return it.")
        return 0

    print(f"  Nothing unread is in our thread ({ours}).")
    print("  If one of the above is your reply, Gmail filed it under a")
    print("  different conversation — which is what composing a new message")
    print("  does, and what the loop can never recover from. Reply from")
    print("  inside the original message instead of writing a fresh one.")
    return 1


async def inspect(settings: Settings) -> int:
    """Show the thread as Gmail holds it. Reads nothing, consumes nothing."""
    ours = _sent_thread()
    if not ours:
        print("\n  No record of a sent message, so there is no thread to look at.")
        print("  Send one first:  make gmail-smoke TO=someone@example.com")
        return 1

    messages = await _transport(settings).inspect_thread(ours)
    if not messages:
        print(f"\n  Thread {ours} has nothing in it, or no longer exists.")
        return 1

    print(f"\n  thread {ours} — {len(messages)} message(s), oldest first:\n")
    for message in messages:
        who = "us" if message.is_ours else message.inbound.from_email
        state = "unread" if message.is_unread else "read"
        print(f"    [{state:6}] {who}")
        print(f"              {message.inbound.subject}")
        first = message.inbound.body.strip().splitlines()
        print(f"              {first[0] if first else ''}")
        print()

    replies = [m for m in messages if not m.is_ours]
    if not replies:
        print("  Nobody has replied in this thread yet.")
        print("  A new message composed from scratch starts its own thread and")
        print("  would not show up here — reply to ours instead.")
        return 1

    if not any(m.is_unread for m in replies):
        print("  The reply is here, but already read — so a poll will not")
        print("  return it. Opening it in Gmail is enough to do that, and so is")
        print("  a poll that already consumed it. The round trip did work.")
        print("  To see poll say so out loud, reply once more and do not open it.")
    return 0


async def poll(settings: Settings) -> int:
    # Only the conversation this script started. Without naming it, polling a
    # mailbox reads and consumes whatever else is unread in it — which is how
    # a hundred unrelated messages got marked read the first time this ran
    # against an account that was also somebody's personal inbox.
    ours = _sent_thread()
    if not ours:
        print("\n  No record of a sent message, so there is no thread to check.")
        print("  Send one first:  make gmail-smoke TO=someone@example.com")
        return 1

    inbound = await _transport(settings).poll(threads=frozenset({ours}))
    if not inbound:
        print("\n  Nothing unread in the thread we sent.")
        print("  Two different things look like this, and this cannot tell")
        print("  them apart — so ask, rather than guess:")
        print("    make gmail-smoke INSPECT=1")
        print()
        print("  It lists the whole thread, read or not, and changes nothing.")
        print("  If your reply is in there, it simply is not unread any more:")
        print("  opening it in Gmail clears UNREAD, and so does a poll that")
        print("  already read it once. If it is not, the reply never landed in")
        print("  this thread — which is what composing a new message does.")
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
        if msg.thread_id == ours:
            threaded = True
            print("    ↳ threaded to the message we sent")
        print()

    if not threaded:
        # The failure worth naming. A reply that arrives but lands in its own
        # thread would be filed against no negotiation, and from the UI that is
        # indistinguishable from a supplier who never answered.
        print("  WARNING: nothing threaded to the message we sent.")
        print(f"    ours:  {ours}")
        print("    A reply in a new thread cannot be filed against a")
        print("    negotiation — it looks exactly like silence.")
        return 1

    print("  Round-trip proven: sent, delivered, replied, read back, threaded.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--to", default="", help="one recipient address")
    _ = parser.add_argument("--poll", action="store_true", help="read replies instead")
    _ = parser.add_argument(
        "--inspect", action="store_true", help="show the whole thread, changing nothing"
    )
    _ = parser.add_argument(
        "--recent", action="store_true", help="unread mail anywhere, with thread ids"
    )
    _ = parser.add_argument(
        "--rearm",
        action="store_true",
        help="mark our thread's replies unread again, so --poll can be re-run",
    )
    _ = parser.add_argument(
        "--find",
        nargs="?",
        const="",
        default=None,
        help="all mail from one sender, spam and trash included",
    )
    _ = parser.add_argument("--yes", action="store_true", help="skip confirmation")
    args = parser.parse_args()

    settings = Settings()
    reading = (
        bool(args.poll)
        or bool(args.inspect)
        or bool(args.recent)
        or args.find is not None
        or bool(args.rearm)
    )
    if blocked := _preflight(settings, polling=reading):
        print(f"\n  Cannot run: {blocked}")
        return 2

    if args.rearm:
        return asyncio.run(rearm(settings))

    if args.find is not None:
        return asyncio.run(find(settings, str(args.find).strip()))

    if args.recent:
        return asyncio.run(recent(settings))

    if args.inspect:
        return asyncio.run(inspect(settings))

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
