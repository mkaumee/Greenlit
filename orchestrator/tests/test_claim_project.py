"""Handing a production to the account that will look at it.

The bug this repairs was silent — an unowned project is hidden from every
browser by ``firestore.rules``, and a hidden project renders as an empty panel
that looks exactly like a deployment with nothing in it. So the tests here are
mostly about the refusals: taking a production away from somebody is the only
damaging thing this script can do, and it does it in one line with no undo.
"""

from datetime import UTC, datetime

from cinema_contracts import ClockMode
from claim_project import claim
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.clock import ClockState
from orchestrator.records import ProjectRecord
from orchestrator.repository import FirestoreRepository

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


async def _project(client: AsyncClient, pid: str, owner: str) -> None:
    await FirestoreRepository(client).create_project(
        pid,
        ProjectRecord(
            title="Kopitiam",
            clock=ClockState(
                sim_now=T0, real_anchor=T0, speed=0.0, mode=ClockMode.FROZEN
            ),
            created_at=T0,
            owner_uid=owner,
        ),
    )


async def test_an_unowned_project_is_claimed(firestore: AsyncClient) -> None:
    await _project(firestore, "demo", "")

    code, message = await claim(firestore, "demo", "uid-1", force=False)

    assert code == 0, message
    record = await FirestoreRepository(firestore).get_project("demo")
    assert record is not None
    assert record.owner_uid == "uid-1"


async def test_claiming_a_project_that_is_already_yours_changes_nothing(
    firestore: AsyncClient,
) -> None:
    """Idempotent, because the operator will not remember whether they ran it."""
    await _project(firestore, "demo", "uid-1")

    code, message = await claim(firestore, "demo", "uid-1", force=False)

    assert code == 0
    assert "already owned" in message


async def test_it_refuses_to_take_a_production_from_somebody_else(
    firestore: AsyncClient,
) -> None:
    """The one damaging thing here, and it has to be said out loud.

    Reassigning quietly would leave the previous owner staring at an empty
    panel with nothing on screen to explain it — the same silent failure this
    script exists to fix, caused by the fix.
    """
    await _project(firestore, "demo", "someone-else")

    code, message = await claim(firestore, "demo", "uid-1", force=False)

    assert code == 2
    assert "--force" in message
    record = await FirestoreRepository(firestore).get_project("demo")
    assert record is not None
    assert record.owner_uid == "someone-else"


async def test_force_takes_it(firestore: AsyncClient) -> None:
    await _project(firestore, "demo", "someone-else")

    code, _ = await claim(firestore, "demo", "uid-1", force=True)

    assert code == 0
    record = await FirestoreRepository(firestore).get_project("demo")
    assert record is not None
    assert record.owner_uid == "uid-1"


async def test_a_missing_project_is_reported_not_created(
    firestore: AsyncClient,
) -> None:
    """``update()`` on a missing document fails; ``set()`` would have created
    an ownerless project with no title and no clock, which the tick would then
    try to run."""
    code, message = await claim(firestore, "nope", "uid-1", force=False)

    assert code == 1
    assert "No project" in message
    assert await FirestoreRepository(firestore).get_project("nope") is None
