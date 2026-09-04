# Sample screenplays

For testing the real thing: drag one onto the chat and watch the agent read it.

## `kopitiam-nights.txt`

Two pages, about 380 words. Short enough that a tick is quick and the tokens
are cheap, long enough to be a fair test.

It is written to be **checkable**. Every item in it is a real object you could
put a price on, and three things in it are deliberately *not*:

| Line | Why it is a trap |
| --- | --- |
| "He remembers his father's watch" | Remembered, never on screen. No watch is needed. |
| "The room smells of rain and old smoke" | Atmosphere. Nothing to buy. |
| "A motorcycle passes somewhere off-screen, unseen" | Explicitly not seen. |

`extract_props` in `contracts/protocols.py` says to extract what a scene needs
to *exist*, not what the prose happens to mention. Those three lines are how you
find out whether it does.

## Where it started

Not a guess — this is what the deployed brain actually returned on 2026-09-03,
when it was asked only for props. Kept because the shape of what it missed is
the argument for the change that followed.

**13 found:** brass kerosene lamp (counter), glasses of teh tarik, wooden
birdcage, leather ledger, fountain pen, enamel plates, sacks of coffee beans,
brass kerosene lamp (storeroom), songkok, cigarette, wall mirror, rattan broom,
tin dustpan.

What was right about it:

- **All three traps avoided.** No watch, no rain, no motorcycle.
- **No hallucinations.** All 13 are in the text, each with an accurate quote.
- **The twin lamps were split** into counter and storeroom line items, off the
  hint *"twin to the one out front"* — two lamps to buy, not one mentioned
  twice.

What was wrong with it:

- **Missed everything nobody picks up:** the marble-topped tables and rattan
  chairs (line 8), the hand-painted signboard (line 91), the ceiling fans
  (line 8), and Razak's batik shirt (line 12). Note the shape of it — the
  songkok is *lifted and turned over in his hands* and was found; the shirt is
  only worn and was not. That is a real distinction between a prop and set
  dressing, correctly applied and useless to a production that still has to
  buy the chairs.
- **The cigarette came back consumable** while the line it quoted says *"a
  cigarette he does not light"* — a flag contradicting its own evidence.

## The current baseline

Then `breakdown/parser.py` was widened to ask for everything a production must
source — set dressing, scenic elements and costume alongside hand props — and
the same screenplay came back with **20**, measured 2026-09-04. This is the
list to compare the next change against.

**Added by the widening:** ceiling fans, marble-topped tables, mismatched
rattan chairs, the faded batik shirt, the awning, the shutters, and the
hand-painted signboard.

**Still right:** all three traps avoided, no hallucinations, the twin lamps
still split, and the "same four cups" in Razak's dialogue on line 19 still
refused — it is a line about the glasses, not a fifth object.

**Fixed:** the cigarette is no longer flagged destroyed on camera, while the
thrown glass and the shattered mirror still are. And the glasses come back as
**qty 4**, from *"four glasses of teh tarik"* — the model takes a count the
script states rather than defaulting to one.

Two physical things in the script are still not on the list, and that is a
decision rather than a defect: the **counter** (lines 9, 31, 64, 68) and the
**shelves** (lines 41, 44). Both come with a real kopitiam location.

The birdcage comes back as *"Wooden Birdcage With Merbok"*, bundling a live
zebra dove into a props line item. Left alone on purpose: the confirm gate is
where a producer edits the list, and nothing is researched or emailed until
they press it.

A consumable item needs one per take, which is why the final quantity is yours
to set at confirmation and not the agent's.

## A calibration point

Run this script through the *scripted* brain — the keyword matcher, the one
`CINEMA_BRAIN_BACKEND=scripted` uses — and it reports:

    chair, cigarette, cup, glass*, lamp, mirror*, plate, table, watch
    (* consumable)

It gets the two consumables right, and it **falls for the watch**, because it
matches nouns and cannot tell a prop from a memory. It also invents a "cup" that
is only in dialogue and misses the birdcage, the songkok, the ledger and the
signboard entirely.

That is the bar. If the real brain returns the same list, it is not reading.

## Using it

Cloud Shell can hand the file to your laptop:

```
cloudshell download samples/kopitiam-nights.txt
```

Then in the panel: pick a production, press **Script** (or drop the file on the
thread), check the list against the baseline above, and confirm.
