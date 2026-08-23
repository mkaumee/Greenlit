"""Simulation time. The only place in the system allowed to read a real clock.

Every timestamp the product stores, compares, or displays comes from here. That
is Hard Rule 2, and this module is the single carve-out: ``SystemRealTime`` below
is the one sanctioned ``datetime.now`` call in the repository, which is why this
file carries a lint exemption and nothing else does.

## How simulated time is derived

The clock is not a counter that something has to remember to increment. It is a
function of three stored values plus the real elapsed time since they were
written::

    now = sim_now + (real_now - real_anchor) * speed

That matters more than it looks. If the tick loop misses a run, or Cloud Run
reaps the instance mid-tick, simulated time is still correct on the next read —
there is no accumulated counter to lose. Nothing needs to be replayed.

``/tick`` re-anchors as it runs: it writes the freshly computed ``sim_now`` and
resets ``real_anchor``. So the stored value stays meaningful for anyone reading
Firestore directly, while the derived form covers the gaps between ticks.

## Modes

``LIVE`` runs at 1:1 and is what a real production would use.
``DEMO`` runs at 21600x — six simulated hours per real second — so five days of
negotiation replay in about twenty seconds for a judge.
``FROZEN`` does not advance on its own; tests move it explicitly.

Same code path in all three. One field differs.
"""

from datetime import UTC, datetime, timedelta
from typing import ClassVar, Protocol

from cinema_contracts import ClockMode
from pydantic import BaseModel, ConfigDict, Field

DEMO_SPEED = 21_600.0
"""Six simulated hours per real second, as a multiplier on elapsed seconds."""

LIVE_SPEED = 1.0

MAX_CATCHUP = timedelta(days=30)
"""Ceiling on how much simulated time a single read may add.

Without this, a project left in DEMO mode overnight would wake up having
advanced simulated time by decades, blowing past every scheduled negotiation at
once.

The number has to clear the longest demo comfortably: sixty real seconds at
DEMO_SPEED is fifteen simulated days, so anything below that would clamp the
thing the demo exists to show. Thirty days gives that headroom while still
bounding an idle demo project to a recoverable state — every negotiation
becomes due at once, and the tick loop works through them.
"""


class RealTime(Protocol):
    """A source of real, wall-clock instants. Injected so tests stay deterministic."""

    def utc_now(self) -> datetime: ...


class SystemRealTime:
    """The real system clock.

    The single sanctioned wall-clock read in the codebase. If you are about to
    add a second one, you want ``SimClock.now()`` instead.
    """

    def utc_now(self) -> datetime:
        return datetime.now(UTC)  # noqa: TID251 — the one sanctioned wall-clock read


class FrozenRealTime:
    """A real-time source that only moves when a test moves it."""

    _at: datetime

    def __init__(self, at: datetime) -> None:
        self._at = at

    def utc_now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at = self._at + delta


class ClockState(BaseModel):
    """The persisted clock, living at ``projects/{pid}.clock``."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    sim_now: datetime = Field(description="Simulated instant as of real_anchor.")
    real_anchor: datetime = Field(description="Real instant when sim_now was written.")
    speed: float = Field(ge=0.0, default=LIVE_SPEED)
    mode: ClockMode = ClockMode.LIVE

    def projected(self, real_now: datetime) -> datetime:
        """Simulated time at a given real instant, without touching storage."""
        if self.mode is ClockMode.FROZEN or self.speed == 0.0:
            return self.sim_now
        elapsed = real_now - self.real_anchor
        if elapsed < timedelta(0):
            # Clock skew between instances, or an anchor written in the future.
            # Refusing to go backwards is safer than trusting either value.
            return self.sim_now
        advanced = timedelta(seconds=elapsed.total_seconds() * self.speed)
        return self.sim_now + min(advanced, MAX_CATCHUP)


class ClockStore(Protocol):
    """Where a project's clock is persisted.

    A protocol so the clock can be unit-tested without an emulator, and so the
    Firestore implementation lives with the rest of the repository layer.
    """

    async def read(self, project_id: str) -> ClockState: ...

    async def write(self, project_id: str, state: ClockState) -> None: ...


class InMemoryClockStore:
    """A clock store backed by a dict. Tests and the loop runner only."""

    _states: dict[str, ClockState]

    def __init__(self, states: dict[str, ClockState] | None = None) -> None:
        self._states = dict(states or {})

    async def read(self, project_id: str) -> ClockState:
        try:
            return self._states[project_id]
        except KeyError:
            raise KeyError(f"No clock for project {project_id!r}") from None

    async def write(self, project_id: str, state: ClockState) -> None:
        self._states[project_id] = state


class SimClock:
    """Reads and advances one project's simulated time.

    Holds no state of its own between calls — Hard Rule 3. Every method reads
    the clock document, computes, and writes back if it changed.
    """

    _store: ClockStore
    _real: RealTime

    def __init__(self, store: ClockStore, real: RealTime | None = None) -> None:
        self._store = store
        self._real = real if real is not None else SystemRealTime()

    def real_now(self) -> datetime:
        """Real wall-clock time. The only caller is project creation.

        A brand-new project has no clock document to read, so there is nothing
        to derive simulated time from — the anchor has to come from somewhere.
        Exposed here rather than letting `app.py` reach for `datetime.now()`,
        which would put a second wall-clock read in the codebase and trip the
        guard.
        """
        return self._real.utc_now()

    async def now(self, project_id: str) -> datetime:
        """Current simulated time. Does not write.

        This is the function every other module calls. It is safe to call as
        often as you like; it is a read plus arithmetic.
        """
        state = await self._store.read(project_id)
        return state.projected(self._real.utc_now())

    async def advance(self, project_id: str) -> datetime:
        """Re-anchor the stored clock to now, and return the new simulated time.

        Called at the top of every tick. Idempotent in the sense that matters:
        calling it twice in a row advances by the real time between the two
        calls and nothing more, so a retried tick does not skip a day.
        """
        state = await self._store.read(project_id)
        real_now = self._real.utc_now()
        advanced = state.projected(real_now)
        await self._store.write(
            project_id,
            state.model_copy(update={"sim_now": advanced, "real_anchor": real_now}),
        )
        return advanced

    async def set_mode(
        self, project_id: str, mode: ClockMode, *, speed: float | None = None
    ) -> ClockState:
        """Switch between live, demo and frozen without losing the current instant.

        Re-anchors first, so the simulated time already elapsed under the old
        speed is banked before the new speed applies.
        """
        state = await self._store.read(project_id)
        real_now = self._real.utc_now()
        banked = state.projected(real_now)

        if speed is not None:
            resolved = speed
        elif mode is ClockMode.DEMO:
            resolved = DEMO_SPEED
        elif mode is ClockMode.LIVE:
            resolved = LIVE_SPEED
        else:
            resolved = 0.0

        updated = ClockState(
            sim_now=banked, real_anchor=real_now, speed=resolved, mode=mode
        )
        await self._store.write(project_id, updated)
        return updated

    async def set_sim_now(self, project_id: str, sim_now: datetime) -> ClockState:
        """Force simulated time to a specific instant. Seeding and tests only.

        Never called from a request handler. Judge mode uses it once, to place a
        seeded project mid-negotiation before anyone presses anything.
        """
        state = await self._store.read(project_id)
        updated = state.model_copy(
            update={"sim_now": sim_now, "real_anchor": self._real.utc_now()}
        )
        await self._store.write(project_id, updated)
        return updated


def initial_state(sim_start: datetime, real_now: datetime) -> ClockState:
    """The clock a freshly created project starts with: live, 1:1, anchored now."""
    return ClockState(
        sim_now=sim_start, real_anchor=real_now, speed=LIVE_SPEED, mode=ClockMode.LIVE
    )
