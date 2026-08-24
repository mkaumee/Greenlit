"""The screenplay both the daily check and the seeder use.

One screenplay, deliberately. ``run_e2e.py`` fills the emulator and
``seed_project.py`` fills a deployed project, and if each carried its own copy
they would drift — the first symptom being a hosted panel showing different
props than the integration test everyone trusts.

Short on purpose. It has to contain a handful of unambiguous physical objects
and one that breaks on camera, and nothing else; a longer script would make the
daily run slower without testing anything the short one misses.
"""

SCREENPLAY = """INT. DIVE BAR - NIGHT

RAZAK nurses a drink at the counter. The BARMAN watches him.

RAZAK grabbed the cup and threw it towards the mirror.

Glass rains down behind the bar.

EXT. ALLEY - CONTINUOUS

Razak lights a cigarette with a shaking hand.
"""

FLOOR = {"amount": 750, "currency": "MYR"}
"""The producer's ceiling, set at confirmation and inherited by every seller
approached for that item — including ones opened later."""
