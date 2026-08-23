# cinema-contracts

The interface between Role A (the brain) and Role B (the runtime). Both sides
import this package. Neither side imports the other.

## The four signatures

Role A implements `AgentBrain` in `main-agent`:

| Method | Direction | Purpose |
| --- | --- | --- |
| `extract_props(source) -> list[PropDraft]` | B calls A | Screenplay becomes a list of physical things the scenes need |
| `research_item(brief) -> ItemResearch` | B calls A | Reference price band with sources, plus supplier candidates |
| `extract_quote(message) -> QuoteExtraction` | B calls A | Read a supplier reply, or refuse to and escalate |
| `next_move(ctx) -> NextMove` | B calls A | Decide the next action and write any email it needs |

All four are `async`. All four are pure with respect to our systems: the brain
reads no Firestore, sends no mail, and holds no memory between calls.

## The five data shapes

1. **`Money`** — `{"amount": 880, "currency": "MYR"}`. Never `"RM880"`, never a
   bare number. Whole major units; `Money.from_major("880.50")` rounds half-up
   at the boundary.
2. **`ItemBrief`** — what B tells A about the thing being bought.
3. **`NegotiationContext`** — everything known about one negotiation, rebuilt
   from Firestore on every tick.
4. **`QuoteExtraction`** — what a supplier's email actually said, or a refusal
   to guess.
5. **`NextMove`** — the decision, plus the reasoning the producer reads on
   screen.

## Rules the models enforce at runtime

These are validation errors, not conventions, so a mistake surfaces on the next
end-to-end run rather than in a demo:

- A `QuoteExtraction` must carry a quote or set `needs_human` with a reason.
  A quietly empty extraction would stall a negotiation with nothing on screen.
- A `NextMove` that sends mail must include a body. Role B sends what the brain
  wrote and never composes text itself.
- A `COUNTER` must name a `target_price`, so the UI can show what the agent is
  pushing for.
- A `SceneMention` must quote a non-empty line. A prop with no line behind it is
  the cheapest possible tell that it was invented rather than found.
- `ACCEPT` and `ESCALATE` must give an `EscalationReason`. Use `GOOD_QUOTE` when
  the negotiation simply succeeded.
- Every model forbids unknown fields. If one side adds a field the other has not
  agreed to, the boundary fails loudly.

## Changing this package

A two-person decision, every time. Both services import it; a field added on one
side and not the other is exactly the divergence the daily `make e2e` run exists
to catch.
