# httpx .json() and Starlette's app.state are both untyped by nature, so the
# Any-flavoured warnings here are about those libraries rather than this code.
# pyright: reportAny=false, reportUnknownMemberType=false
"""HTTP surface tests, driven through ASGI against the emulator.

No server is started and no port is bound — httpx talks to the app object
directly, so these are as fast as unit tests while exercising the real routing,
validation and response models.
"""

import logging
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cinema_contracts import AgentBrain, ClockMode, NegotiationState
from cinema_contracts.testing import ScriptedBrain
from fastapi import HTTPException
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.app import (
    Services,
    app,
    build_brain,
    build_mail,
    build_mailboxes,
    services_of,
)
from orchestrator.clock import ClockState, FrozenRealTime, SimClock
from orchestrator.mail import InMemoryMailbox
from orchestrator.mailboxes import ProducerMailboxes, SingleMailbox
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
        loop=TickLoop(repo, clock, brain, SingleMailbox(mail)),
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
    # The scripted brain has no model and no web search. Reported as such
    # rather than omitted, so "which of these two is running" is answerable
    # from outside the container.
    assert body["gemini_model"] == ""
    assert body["gemini_credentials"] == ""
    assert body["research_key_present"] is False


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
    """The real brain is merged now, and the default is still the fake.

    Deliberately. Turning on reasoning that emails real sellers is an explicit
    choice, the same as turning on mail — and it is reported on /health rather
    than assumed, because a keyword matcher writing negotiation emails looks
    like a working system right up until somebody reads one.
    """
    assert isinstance(build_brain(SETTINGS), ScriptedBrain)


def test_selecting_the_real_brain_without_the_package_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a silent fallback. Shipping the fake by accident is the failure mode.

    ``main_agent`` is merged and installed, so the import cannot fail by
    itself any more — the way it fails now is a deployed image built without
    the ``COPY main-agent/`` lines. That is worth keeping a guard on precisely
    because everything else about such an image looks fine.
    """

    def refuse(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("orchestrator.app.importlib.import_module", refuse)
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


def test_the_real_brain_is_built_with_the_configured_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring to Role A's brain, pinned against a fake module.

    Still a fake rather than the real class: constructing ``GeminiAgentBrain``
    builds five ADK agents, which is not something a unit test should do.

    ``GeminiAgentBrain`` takes ``model`` as a required keyword deliberately —
    Role A's docstring says the application wiring chooses the deployed model
    rather than the brain reading ambient configuration. That contract is easy
    to get wrong in a way nothing catches: pass no model and it is a TypeError
    at startup; pass the wrong attribute name and it is an AttributeError days
    later. A fake module standing in for the package pins both.
    """
    seen: dict[str, object] = {}

    class FakeBrain:
        def __init__(self, *, model: str) -> None:
            seen["model"] = model

        # Underscored: the protocol needs the methods to exist with the right
        # names, not to do anything. isinstance() against a runtime_checkable
        # Protocol checks presence only.
        async def extract_props(self, _source: object) -> list[object]:
            return []

        async def research_item(self, _brief: object) -> object: ...
        async def extract_quote(self, _message: object) -> object: ...
        async def next_move(self, _ctx: object) -> object: ...
        async def brief_producer(self, _question: object) -> object: ...

    module = types.ModuleType("main_agent")
    module.GeminiAgentBrain = FakeBrain  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "main_agent", module)
    _vertex_configured(monkeypatch)

    brain = build_brain(
        Settings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            brain_backend=BrainBackend.MAIN_AGENT,
            gemini_model="gemini-3.7-pro",
        )
    )

    assert isinstance(brain, FakeBrain)
    assert seen["model"] == "gemini-3.7-pro", "the configured model must reach it"


async def test_a_project_records_who_owns_it(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """firestore.rules matches on this field, so it is what makes a production
    visible to its producer and to nobody else."""
    created = await api.post(
        "/projects",
        json={"project_id": "owned", "title": "Kopitiam", "owner_uid": "uid-1"},
    )

    assert created.status_code == 201
    record = await FirestoreRepository(firestore).get_project("owned")
    assert record is not None
    assert record.owner_uid == "uid-1"


async def test_a_project_with_no_owner_is_created_unreachable(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Not an error — the safe default.

    An unowned project is invisible to every browser rather than readable by
    whoever signs in next, which is the exact bug owner_uid exists to close.
    """
    created = await api.post(
        "/projects", json={"project_id": "orphan", "title": "Nobody's"}
    )

    assert created.status_code == 201
    record = await FirestoreRepository(firestore).get_project("orphan")
    assert record is not None
    assert record.owner_uid == ""


# --------------------------------------------------------------------------- #
# Starting on the real brain
# --------------------------------------------------------------------------- #


def _vertex_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credentials a deployed service has. Set so each test below isolates
    the one thing it is about."""
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-cinema")
    monkeypatch.setenv("PARALLEL_API_KEY", "present")


def test_the_real_brain_refuses_to_start_without_gemini_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise every reasoning call fails at request time instead.

    A tick would claim its rows, fail on the brain, and park them for the
    lease — over and over, looking like a system that is merely slow rather
    than one that was never given credentials.
    """
    _vertex_configured(monkeypatch)
    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        brain_backend=BrainBackend.MAIN_AGENT,
    )

    with pytest.raises(RuntimeError, match="Gemini has no credentials"):
        _ = build_brain(settings)


def test_vertex_without_a_project_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-configured is the one that would look fine on the way out."""
    _vertex_configured(monkeypatch)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        brain_backend=BrainBackend.MAIN_AGENT,
    )

    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        _ = build_brain(settings)


def test_the_real_brain_refuses_to_start_without_a_research_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The silent one.

    Without PARALLEL_API_KEY the search call raises, the researcher catches it
    and tells the model "web search failed", and the model answers from memory
    anyway. What comes out is a reference price band and supplier URLs with
    nothing behind them — in a system whose whole claim is that it keeps the
    URLs it got its numbers from. Nothing errors and nothing logs.
    """
    _vertex_configured(monkeypatch)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        brain_backend=BrainBackend.MAIN_AGENT,
    )

    # Default rather than needs_research=True, so a caller that forgets the
    # flag entirely still gets the check.
    with pytest.raises(RuntimeError, match="PARALLEL_API_KEY"):
        _ = build_brain(settings)


def test_a_service_that_does_not_research_needs_no_research_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cinema-api` reads screenplays and briefs producers. It never calls
    `research_item`, which is the only capability that searches the web.

    This is a regression. `deploy.sh` gives that service
    CINEMA_BRAIN_BACKEND=main-agent and deliberately no PARALLEL_API_KEY —
    correctly, since a key there would be a credential in an environment with
    no use for it — while `build_brain` demanded one unconditionally. The
    result was a container that raised during startup, taking chat, mailbox
    connect and script upload down together on the next deploy.
    """
    _vertex_configured(monkeypatch)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        brain_backend=BrainBackend.MAIN_AGENT,
    )

    brain = build_brain(settings, needs_research=False)

    assert isinstance(brain, AgentBrain)


def test_the_scripted_brain_needs_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default has to keep working on a laptop with no credentials."""
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        brain_backend=BrainBackend.SCRIPTED,
    )

    assert isinstance(build_brain(settings), ScriptedBrain)


# --------------------------------------------------------------------------- #
# The wiring itself
# --------------------------------------------------------------------------- #


def test_real_mail_means_each_producer_sends_from_their_own_mailbox(
    firestore: AsyncClient,
) -> None:
    """Guards the bug this test was written after.

    ProducerMailboxes was built, tested and never wired in: build_services
    passed SingleMailbox unconditionally, so the whole per-producer feature
    could not affect a single tick. Every unit test still passed, because they
    all tested the class rather than its use. Code that looks finished and does
    nothing is worse than code that is obviously missing.
    """
    live = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        mail_backend=MailBackend.GMAIL,
    )

    provider = build_mailboxes(live, FirestoreRepository(firestore), InMemoryMailbox())

    assert isinstance(provider, ProducerMailboxes)


def test_memory_mail_keeps_one_mailbox_for_everything(
    firestore: AsyncClient,
) -> None:
    """So make e2e and every test run the path the real provider runs."""
    provider = build_mailboxes(
        SETTINGS, FirestoreRepository(firestore), InMemoryMailbox()
    )

    assert isinstance(provider, SingleMailbox)
