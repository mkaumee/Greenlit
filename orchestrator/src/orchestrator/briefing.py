"""Turning a digest into something a producer can read.

Deterministic, and honestly so. Every number below is read from Firestore
rather than reasoned about — which is the *right* answer for "what is going
on", not a placeholder for one. A model asked to count how many negotiations
are at round three can get it wrong; this cannot.

What it deliberately does not do is guess. A question it does not recognise
gets a list of what it can answer, not an improvised reply. That is the same
principle as escalating an ambiguous supplier quote to ``READY_FOR_HUMAN``
instead of inventing a number: a system that fabricates when it is unsure is
worse than one that says it is unsure, and much worse here, because the
fabrication would be about somebody's money.

Free-form advice — "should I push Skyline harder?" — is reasoning, belongs to
Role A's brain, and routes there once the fifth contract method lands. This
module keeps the facts half either way, because the brain will be handed the
same digest.
"""

from cinema_contracts import Money

from orchestrator.digest import NegotiationLine, ProjectDigest

Referenced = tuple[str, str, str]
"""``(kind, id, label)`` — what the panel renders as a link."""

_DEAD = {"DEAD", "ORDERED"}


def _money(amount: Money | None) -> str:
    return "—" if amount is None else f"{amount.currency} {amount.amount:,}"


def _cheapest(negotiations: list[NegotiationLine]) -> NegotiationLine | None:
    live = [n for n in negotiations if n.latest_quote is not None]
    if not live:
        return None
    return min(live, key=lambda n: n.latest_quote.amount if n.latest_quote else 0)


def summarise(digest: ProjectDigest, question: str) -> tuple[str, list[Referenced]]:
    """Answer what can be answered from stored facts.

    The question steers which facts lead, not whether they are true. Matching
    is deliberately shallow — a handful of words a producer actually uses —
    because a clever matcher that is wrong is worse than a plain one that says
    so.
    """
    asked = question.lower()

    if any(word in asked for word in ("need", "waiting", "approve", "decide")):
        return _waiting(digest)
    if any(word in asked for word in ("quiet", "silent", "chas", "stuck", "slow")):
        return _quiet(digest)
    if any(word in asked for word in ("cost", "price", "spend", "budget", "saving")):
        return _money_answer(digest)
    if any(word in asked for word in ("prop", "item", "breakdown", "script")):
        return _items(digest)
    return _overview(digest)


def _waiting(digest: ProjectDigest) -> tuple[str, list[Referenced]]:
    waiting = [n for n in digest.negotiations if n.waiting_on_human]
    if not waiting:
        return ("Nothing needs you right now.", [])

    lines = [f"{len(waiting)} decision(s) waiting on you:"]
    refs: list[Referenced] = []
    for n in waiting:
        why = n.escalation_reason or "the agent stopped here"
        lines.append(
            f"  · {n.item_name} — {n.supplier} at {_money(n.latest_quote)} "
            f"after {n.rounds_used} round(s). {why}."
        )
        refs.append(("negotiation", n.negotiation_id, n.item_name))
    lines.append("\nNothing is bought until you approve it.")
    return ("\n".join(lines), refs)


def _quiet(digest: ProjectDigest) -> tuple[str, list[Referenced]]:
    """Who has not answered.

    Worth its own answer because silence is the failure this system is most
    often accused of and least able to see: a supplier who never replies looks
    exactly like a transport that stopped working.
    """
    chasing = [n for n in digest.negotiations if n.state == "CHASING"]
    dead = [n for n in digest.negotiations if n.state == "DEAD"]
    if not chasing and not dead:
        return ("Every supplier who was written to has answered.", [])

    lines: list[str] = []
    refs: list[Referenced] = []
    if chasing:
        lines.append(f"{len(chasing)} being chased:")
        for n in chasing:
            lines.append(f"  · {n.supplier} about {n.item_name}")
            refs.append(("negotiation", n.negotiation_id, n.item_name))
    if dead:
        lines.append(f"{len(dead)} gave up on after no reply:")
        for n in dead:
            lines.append(f"  · {n.supplier} about {n.item_name}")
            refs.append(("negotiation", n.negotiation_id, n.item_name))
    return ("\n".join(lines), refs)


def _money_answer(digest: ProjectDigest) -> tuple[str, list[Referenced]]:
    quoted = [n for n in digest.negotiations if n.latest_quote is not None]
    if not quoted:
        return ("No supplier has quoted a price yet.", [])

    lines: list[str] = []
    refs: list[Referenced] = []
    moved = [
        n
        for n in quoted
        if n.first_quote is not None
        and n.latest_quote is not None
        and n.latest_quote.amount < n.first_quote.amount
    ]
    if moved:
        lines.append(f"{len(moved)} supplier(s) have come down from their opening:")
        for n in moved:
            lines.append(
                f"  · {n.item_name} — {n.supplier}: "
                f"{_money(n.first_quote)} → {_money(n.latest_quote)}"
            )
            refs.append(("negotiation", n.negotiation_id, n.item_name))
    else:
        lines.append("Nobody has moved off their opening price yet.")

    best = _cheapest([n for n in quoted if n.state not in _DEAD])
    if best is not None:
        lines.append(
            f"\nCheapest live quote: {best.item_name} from {best.supplier} "
            f"at {_money(best.latest_quote)}."
        )
        refs.append(("negotiation", best.negotiation_id, best.item_name))
    lines.append("These are quotes, not purchases. Nothing has been bought.")
    return ("\n".join(lines), refs)


def _items(digest: ProjectDigest) -> tuple[str, list[Referenced]]:
    if not digest.items:
        return ("No screenplay has been read yet, so there are no props.", [])

    lines = [f"{len(digest.items)} prop(s) from the script:"]
    refs: list[Referenced] = []
    for item in digest.items:
        found = f'  found in: "{item.script_line}"' if item.script_line else ""
        lines.append(f"  · {item.name} x{item.qty} — {item.status}{found}")
        refs.append(("item", item.item_id, item.name))
    return ("\n".join(lines), refs)


def _overview(digest: ProjectDigest) -> tuple[str, list[Referenced]]:
    """The default, and what an unrecognised question gets.

    It says what is true and then what it can be asked, rather than improvising
    an answer to a question it did not understand.
    """
    live = [n for n in digest.negotiations if n.state not in _DEAD]
    lines = [
        (
            f"{digest.title}: {len(digest.items)} prop(s), "
            f"{len(live)} live negotiation(s), "
            f"{digest.waiting_count} waiting on you."
        )
    ]
    refs: list[Referenced] = []

    best = _cheapest(live)
    if best is not None:
        lines.append(
            f"Cheapest live quote is {best.item_name} from {best.supplier} "
            f"at {_money(best.latest_quote)}."
        )
        refs.append(("negotiation", best.negotiation_id, best.item_name))

    lines.append(
        "\nAsk me what needs you, who has gone quiet, what things cost, "
        "or about the props themselves."
    )
    return ("\n".join(lines), refs)
