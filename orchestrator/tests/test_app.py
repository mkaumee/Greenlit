# httpx .json() and Starlette's app.state are both untyped by nature, so the
# Any-flavoured warnings here are about those libraries rather than this code.
# pyright: reportAny=false, reportUnknownMemberType=false
"""HTTP surface tests, driven through ASGI against the emulator.

No server is started and no port is bound — httpx talks to the app object
directly, so these are as fast as unit tests while exercising the real routing,
validation and response models.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cinema_contracts import ClockMode, NegotiationState
from cinema_contracts.testing import ScriptedBrain
from fastapi import HTTPException
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.app import Services, app, build_brain, build_mail, services_of
from orchestrator.clock import ClockState, FrozenRealTime, SimClock
from orchestrator.mail import InMemoryMailbox
from orchestrator.records import (
    ItemRecord,
    NegotiationRecord,
    ProjectRecord,
    SupplierRecord,
)
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import BrainBackend, MailBackend, Settings
from orchestrator.tick import TickLoop

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
REAL0 = datetime(2026, 8, 12, 14, 0, tzinfo=UTC)

SETTINGS = Settings(_env_file=None, gcp_project="demo-cinema")  # pyright: ignore[reportCallIssue]


def _services(client: AsyncClient) -> Services:
    """The real composition root, with the emulator client injected."""
    repo = FirestoreRepository(client)
    clock = SimClock(repo, FrozenRealTime(REAL0))
    brain = ScriptedBrain()
    mail = InMemoryMailbox()
    return Services(
        settings=SETTINGS,
        client=client,
        repo=repo,
        clock=clock,
        brain=brain,
        mail=mail,
        loop=TickLoop(repo, clock, brain, mail),
    )


async def _seed(repo: FirestoreRepository, project_id: str) -> None:
    await repo.create_project(
        project_id,
        ProjectRecord(
            title=project_id,
            clock=ClockState(
                sim_now=T0, real_anchor=REAL0, speed=0.0, mode=ClockMode.FROZEN
            ),
            created_at=T0,
        ),
    )
    await repo.save_item(
        project_id, "item1", ItemRecord(name="Mirror", category="prop")
    )
    await repo.save_supplier(
        project_id,
        "sup1",
        SupplierRecord(name="Glass Co", email="glass@example.invalid", verified=True),
    )
    await repo.save_negotiation(
        project_id,
        "neg1",
        NegotiationRecord(
            item_id="item1",
            supplier_id="sup1",
            state=NegotiationState.DRAFTED,
            next_action_due_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )


@pytest.fixture
async def api(firestore: AsyncClient) -> httpx.AsyncClient:
    app.state.services = _services(firestore)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


async def test_health_says_which_transports_are_wired(api: httpx.AsyncClient) -> None:
    """ "Alive" is not the useful question — "about to email a real seller" is."""
    body = (await api.get("/health")).json()

    assert body["status"] == "ok"
    assert body["brain_backend"] == "scripted"
    assert body["mail_backend"] == "memory"
    assert body["token_backend"] == "file"


# --------------------------------------------------------------------------- #
# Tick
# --------------------------------------------------------------------------- #


async def test_tick_with_no_body_covers_every_project(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """How Cloud Scheduler calls it: one schedule, no arguments."""
    repo = FirestoreRepository(firestore)
    await _seed(repo, "projA")
    await _seed(repo, "projB")

    body = (await api.post("/tick")).json()

    assert {p["project_id"] for p in body["projects"]} == {"projA", "projB"}
    assert sum(p["messages_sent"] for p in body["projects"]) == 2


async def test_tick_can_be_aimed_at_one_project(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    repo = FirestoreRepository(firestore)
    await _seed(repo, "projA")
    await _seed(repo, "projB")

    body = (await api.post("/tick", params={"project_id": "projA"})).json()

    assert [p["project_id"] for p in body["projects"]] == ["projA"]


async def test_tick_actually_advances_the_negotiation(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    repo = FirestoreRepository(firestore)
    await _seed(repo, "projA")

    _ = await api.post("/tick")

    record = await repo.get_negotiation("projA", "neg1")
    assert record is not None
    assert record.state is NegotiationState.AWAITING_REPLY


async def test_one_broken_project_does_not_stop_the_others(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """A project that silently stops being ticked is a negotiation that dies.

    ``broken`` has no clock document, so advancing it raises. The healthy
    project must still get its tick, and the failure must be reported rather
    than swallowed.
    """
    repo = FirestoreRepository(firestore)
    await _seed(repo, "healthy")
    _ = (
        await firestore.collection("projects")
        .document("broken")
        .set({"title": "no clock"})
    )

    body = (await api.post("/tick")).json()
    by_id = {p["project_id"]: p for p in body["projects"]}

    assert by_id["broken"]["errors"], "the failure should be reported"
    assert by_id["healthy"]["messages_sent"] == 1
    assert not by_id["healthy"]["errors"]


async def test_ticking_nothing_is_not_an_error(api: httpx.AsyncClient) -> None:
    body = (await api.post("/tick")).json()
    assert body["projects"] == []


async def test_the_report_carries_simulated_time_not_real_time(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    repo = FirestoreRepository(firestore)
    await _seed(repo, "projA")

    body = (await api.post("/tick")).json()

    assert body["projects"][0]["sim_now"].startswith("2026-03-01")


# --------------------------------------------------------------------------- #
# Composition
# --------------------------------------------------------------------------- #


def test_mail_defaults_to_memory_so_nothing_emails_a_real_seller() -> None:
    assert isinstance(build_mail(SETTINGS), InMemoryMailbox)


def test_the_brain_defaults_to_the_fake_and_says_so() -> None:
    """Role A's brain is on another branch, so the fake is the only option.

    It is reported on /health rather than assumed, because a keyword matcher
    writing negotiation emails looks like a working system right up until
    somebody reads one.
    """
    assert isinstance(build_brain(SETTINGS), ScriptedBrain)


def test_selecting_the_real_brain_before_it_exists_fails_loudly() -> None:
    """Not a silent fallback. Shipping the fake by accident is the failure mode."""
    real = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        brain_backend=BrainBackend.MAIN_AGENT,
    )
    with pytest.raises(RuntimeError, match="main_agent is not importable"):
        _ = build_brain(real)


def test_choosing_gmail_is_explicit_and_needs_a_token(tmp_path: Path) -> None:
    """Selecting the real transport with no bootstrapped token fails loudly."""
    live = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        mail_backend=MailBackend.GMAIL,
        token_dir=tmp_path / "absent",
    )
    with pytest.raises(FileNotFoundError):
        _ = build_mail(live)


async def test_services_are_built_once_and_shared(api: httpx.AsyncClient) -> None:
    """Nothing per-request, nothing cached between ticks beyond Firestore."""
    _ = await api.get("/health")
    first = app.state.services
    _ = await api.get("/health")
    assert app.state.services is first


# --------------------------------------------------------------------------- #
# The separation that makes Hard Rule 5 true
# --------------------------------------------------------------------------- #


def test_the_tick_service_exposes_no_way_to_spend_money() -> None:
    """The cheapest guard in the repo, against the easiest mistake.

    Approval lives in ``orchestrator.approvals``, deployed as its own Cloud Run
    service under the one account that has an IAM binding on the ``orders``
    database. Moving a route onto this app would mean granting that binding
    here, and "the agent cannot spend money" would stop being true while every
    other test still passed.
    """
    paths = [getattr(route, "path", "") for route in app.routes]

    assert not [p for p in paths if "approve" in p or "purchase" in p], paths


def test_the_tick_service_holds_no_client_for_the_orders_database() -> None:
    """Belt and braces on the same rule, one layer down.

    Even with a wrong IAM binding there is nothing in this service's
    composition root that could reach purchase orders — no orders client, and a
    repository class with no method that writes one.
    """
    fields = set(Services.__dataclass_fields__)

    assert "orders" not in fields
    assert "orders_client" not in fields
    assert not [m for m in dir(FirestoreRepository) if "purchase_order" in m]


def test_a_request_without_startup_is_refused_rather_than_crashing() -> None:
    bare = type("R", (), {"app": type("A", (), {"state": type("S", (), {})()})()})()
    with pytest.raises(HTTPException) as caught:
        _ = services_of(bare)  # pyright: ignore[reportArgumentType]
    assert caught.value.status_code == 503


async def test_a_tick_with_no_projects_still_logs(
    api: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Silence and "the scheduler stopped calling" must not look the same.

    Found on the real deployment: Cloud Scheduler was firing every minute,
    `lastAttemptTime` proved it, and Cloud Logging held not one line — because
    the log statement lived inside the per-project loop and no screenplay had
    been uploaded yet. An empty tick is a fact worth recording.
    """
    with caplog.at_level(logging.INFO, logger="orchestrator"):
        body = (await api.post("/tick")).json()

    assert body["projects"] == []
    ticks = [r for r in caplog.records if r.getMessage() == "tick"]
    assert len(ticks) == 1, "an empty tick must still produce a line"
    assert getattr(ticks[0], "projects", None) == 0
