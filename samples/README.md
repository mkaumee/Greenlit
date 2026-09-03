# Sample screenplays

For testing the real thing: drag one onto the chat and watch the agent read it.

## `kopitiam-nights.txt`

Two pages, about 380 words. Short enough that a tick is quick and the tokens
are cheap, long enough to be a fair test.

It is written to be **checkable**. Every prop in it is a real object you could
put a price on, and three things in it are deliberately *not* props:

| Line | Why it is a trap |
| --- | --- |
| "He remembers his father's watch" | Remembered, never on screen. No watch is needed. |
| "The room smells of rain and old smoke" | Atmosphere. Nothing to buy. |
| "A motorcycle passes somewhere off-screen, unseen" | Explicitly not seen. |

`extract_props` in `contracts/protocols.py` says to extract what a scene needs
to *exist*, not what the prose happens to mention. Those three lines are how you
find out whether it does.

What it should find, roughly: teh tarik glasses (four, and one is thrown),
rattan chairs, a brass kerosene lamp (two — one lit out front, one dusty in the
storeroom), a wooden birdcage, a batik shirt, a songkok, a leather ledger, a
fountain pen, enamel plates, a rattan broom, a tin dustpan, a hand-painted
signboard, and a wall mirror.

Two of those break on camera and should come back **consumable**: the glass
that is thrown and the mirror it hits. A consumable prop needs one per take,
which is why the quantity is yours to set at confirmation and not the agent's.

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
thread), check the list against the table above, and confirm.
