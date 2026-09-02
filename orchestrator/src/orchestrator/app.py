# Starlette's app.state is an untyped attribute bag, so services_of has to
# reach through Any to get at what lifespan put there.
# pyright: reportAny=false
"""The HTTP surface. What Cloud Scheduler calls, and what the UI posts to.

    POST /projects                       create a production
    POST /projects/{id}/script           read a screenplay for props
    POST /projects/{id}/items/confirm    a producer signs the list off
    POST /tick                           advance the world by one pass
    GET  /health

The upload and confirm steps are separate on purpose. Reading a script produces
DRAFT items and nothing else happens; confirming is what starts research and,
eventually, email. That gap is where a hallucinated prop gets caught before it
becomes a message to a real seller. Everything the loop needs
is assembled once in the lifespan and handed to the handler — nothing is stored
in a module global, and nothing survives between requests except what is in
Firestore. That is Hard Rule 3, and it is what lets Cloud Run reap this process
mid-tick without consequence.

``POST /tick`` with no body ticks every project, because that is how Cloud
Scheduler will call it: one schedule, no arguments. Pass ``project_id`` to tick
one, which is what you want when poking it by hand.

**Not yet, deliberately.** The tick endpoint is unauthenticated. In Phase 3 it
sits behind Cloud Run with a Scheduler OIDC token and no public ingress. It is
noted here rather than half-built, because a home-grown shared secret would
look like protection without being any.
"""

import importlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from cinema_contracts import AgentBrain, Money, ScriptSource
from cinema_contracts.testing import ScriptedBrain
from fastapi import FastAPI, HTTPException, Request
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1 import AsyncClient
from pydantic import BaseModel, Field

from orchestrator import intake
from orchestrator.clock import SimClock
from orchestrator.gmail import GmailTransport, build_credentials, token_store_for
from orchestrator.logs import configure_logging
from orchestrator.mail import InMemoryMailbox, MailTransport
from orchestrator.mailboxes import (
    MailboxProvider,
    ProducerMailboxes,
    SingleMailbox,
)
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import BrainBackend, MailBackend, Settings
from orchestrator.tick import TickLoop, TickReport

log = logging.getLogger("orchestrator")

NEVER = datetime.min.replace(tzinfo=UTC)
"""Stand-in instant for a report about a tick that failed before it started.

Not a clock read — the tick never got as far as advancing one, and inventing a
plausible timestamp here would be a lie about simulated time.
"""


@dataclass(frozen=True, slots=True)
class Services:
    """Everything a request needs, built once at startup."""

    settings: Settings
    client: AsyncClient
    repo: FirestoreRepository
    clock: SimClock
    brain: AgentBrain
    mail: MailTransport
    loop: TickLoop


def build_mail(settings: Settings) -> MailTransport:
    """The single shared transport. Memory unless someone asked for the real one.

    Still here because the smoke check and the in-memory path both want one
    mailbox, and because ``SingleMailbox`` wraps whatever this returns. What
    changed is that the tick loop no longer receives it directly — see
    ``build_mailboxes``.
    """
    if settings.mail_backend is MailBackend.GMAIL:
        credentials = build_credentials(
            token_store_for(settings),
            settings.oauth_client_id,
            settings.oauth_client_secret,
        )
        return GmailTransport.from_credentials(credentials, settings)
    return InMemoryMailbox()


def build_mailboxes(
    settings: Settings, repo: FirestoreRepository, fallback: MailTransport
) -> MailboxProvider:
    """Which mailbox each project sends from.

    With real mail on, every project sends from the Gmail its own producer
    connected — that is the product, and it is why a supplier hears from a
    person at a production rather than from a robot at a vendor.

    With mail off, one in-memory mailbox serves everything, so ``make e2e`` and
    every test run the same code path the real provider does.

    This function existing at all is the fix for a real bug: ``ProducerMailboxes``
    was written, tested, and never wired in, so the whole per-producer feature
    could not affect a single tick. Built code that looks finished and does
    nothing is worse than code that is obviously missing.
    """
    if settings.mail_backend is MailBackend.GMAIL:
        return ProducerMailboxes(repo, settings)
    return SingleMailbox(fallback)


def gemini_credentials_route() -> str:
    """Which route Gemini authenticates by, for reporting on /health.

    Public because both services report it: the tick and the producer-facing
    api. One implementation, so the two cannot disagree about how the same
    process is authenticating.
    """
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"true", "1"}:
        return "vertex"
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return "api-key"
    return ""


def build_brain(settings: Settings, *, needs_research: bool = True) -> AgentBrain:
    """The reasoning half. Role A's, or the fake standing in for it.

    ``needs_research`` says whether this service will call ``research_item``,
    which is the only capability that searches the web. The tick does; the
    producer-facing api reads screenplays and briefs producers and does not, so
    it is built without a research key rather than handed one it has no use
    for. Defaulted to True so a new caller has to say it does not need
    research, rather than silently losing the check by forgetting a flag.

    ``main-agent`` is merged; the default is still the deterministic fake,
    because reasoning that emails real sellers should be switched on
    deliberately. The import stays late and stays loud: an image built without
    the ``COPY main-agent/`` lines fails at startup with an explanation rather
    than silently falling back to a keyword matcher that would look like it was
    working.
    """
    if settings.brain_backend is BrainBackend.MAIN_AGENT:
        # Resolved by name rather than imported statically: the package is on
        # Role A's branch and genuinely is not here, so a static import would
        # be a permanent lie to the type checker. The protocol check below is
        # what actually guarantees it fits.
        try:
            module = importlib.import_module("main_agent")
        except ImportError as exc:
            raise RuntimeError(
                "CINEMA_BRAIN_BACKEND=main-agent, but main_agent is not "
                "importable. It is a workspace member and a dependency of "
                "orchestrator, so this almost certainly means an image built "
                "without the `COPY main-agent/` lines in the Dockerfile."
            ) from exc

        # Read from the environment rather than Settings because the name is
        # the Parallel SDK's, not ours — `AsyncParallel()` infers it, and
        # renaming it behind CINEMA_ would mean setting it twice.
        #
        # Checked at startup because the failure is otherwise invisible and
        # expensive. Without a key the search call raises, research/researcher.py
        # catches it, and the model is told "web search failed" — so it answers
        # anyway, from memory. The result is a reference price band and supplier
        # URLs with nothing behind them, in a system whose entire claim is that
        # it keeps the URLs it got its numbers from. Nothing errors and nothing
        # logs; a producer just gets invented evidence.
        # Gemini reaches Vertex through the service account, or an API key,
        # and google-genai reads these names itself. Neither present means
        # every reasoning call fails at request time rather than at startup —
        # so a tick would claim rows, fail, and park them for the lease, over
        # and over, looking like a slow system rather than an unconfigured one.
        vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {
            "true",
            "1",
        }
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not vertex and not api_key:
            raise RuntimeError(
                "CINEMA_BRAIN_BACKEND=main-agent, but Gemini has no "
                "credentials. On Cloud Run set GOOGLE_GENAI_USE_VERTEXAI=true "
                "with GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION, and give "
                "the service account roles/aiplatform.user — scripts/deploy.sh "
                "does all three. GOOGLE_API_KEY also works and is worse: a key "
                "to store and rotate where the account already authenticates."
            )
        if vertex and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
            raise RuntimeError(
                "GOOGLE_GENAI_USE_VERTEXAI is set but GOOGLE_CLOUD_PROJECT is "
                "not. Vertex cannot infer the project, so every reasoning call "
                "would fail — at request time, one negotiation at a time."
            )

        if needs_research and not os.environ.get("PARALLEL_API_KEY"):
            raise RuntimeError(
                "CINEMA_BRAIN_BACKEND=main-agent, but PARALLEL_API_KEY is not "
                "set. Item research would still answer — without any web "
                "search behind it — so the reference bands and supplier URLs "
                "it produced would be invented rather than sourced. Refusing "
                "to start instead, because that failure is silent."
            )

        brain: object = module.GeminiAgentBrain(model=settings.gemini_model)
        if not isinstance(brain, AgentBrain):
            raise RuntimeError(
                "main_agent.GeminiAgentBrain does not satisfy the AgentBrain "
                "protocol. Checked at startup rather than on the first tick, "
                "because a missing method would otherwise surface days into a "
                "negotiation."
            )
        log.info(
            "brain", extra={"backend": "main-agent", "model": settings.gemini_model}
        )
        return brain

    log.warning(
        "running on the SCRIPTED brain — a regex and a word list, not the "
        "product. Set CINEMA_BRAIN_BACKEND=main-agent once role_a is merged."
    )
    return ScriptedBrain()


def build_services(settings: Settings | None = None) -> Services:
    resolved = settings or Settings()
    client = AsyncClient(project=resolved.gcp_project)
    repo = FirestoreRepository(client)
    clock = SimClock(repo)
    brain = build_brain(resolved)
    mail = build_mail(resolved)
    return Services(
        settings=resolved,
        client=client,
        repo=repo,
        clock=clock,
        brain=brain,
        mail=mail,
        loop=TickLoop(repo, clock, brain, build_mailboxes(resolved, repo, mail)),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Logging first, then the services. Composition emits the loudest warning
    # in the system — that the fake brain is live — and it would otherwise go
    # out through the default handler, unstructured, and be the one line in the
    # stream a severity filter cannot find.
    settings = Settings()
    configure_logging(settings)
    services = build_services(settings)
    app.state.services = services
    log.info(
        "orchestrator up",
        extra={
            "mail_backend": services.settings.mail_backend.value,
            "token_backend": services.settings.token_backend.value,
            "project": services.settings.gcp_project,
        },
    )
    try:
        yield
    finally:
        services.client.close()


app = FastAPI(title="Greenlit orchestrator", lifespan=lifespan)


def services_of(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - only if lifespan was skipped
        raise HTTPException(status_code=503, detail="services not initialised")
    return services


class Health(BaseModel):
    status: str
    brain_backend: str
    gemini_model: str
    """Which model is reasoning. Empty on the scripted brain, which has none."""
    gemini_credentials: str
    """How Gemini authenticates: "vertex", "api-key", or "" on the fake.

    Vertex means the service account signs its own calls and there is no key
    anywhere in the deployment. Worth reporting rather than assuming, because
    the two look identical from the outside until one of them leaks."""
    research_web_search: bool
    """Whether item research can actually search the web.

    False means it still answers, from the model's memory, with price bands and
    supplier URLs that were invented rather than sourced. Startup refuses this
    combination, so a running service reporting false would mean the check was
    removed — which is worth being able to see from outside."""
    mail_backend: str
    token_backend: str
    project: str


class TickResult(BaseModel):
    """One project's worth of work, flattened for JSON."""

    project_id: str
    sim_now: str
    items_examined: int
    items_researched: int
    negotiations_opened: int
    replies_filed: int
    replies_skipped: int
    replies_after_stop: int
    unmatched_replies: int
    negotiations_examined: int
    claims_lost: int
    messages_sent: int
    escalated: int
    errors: list[str]

    @classmethod
    def of(cls, project_id: str, report: TickReport) -> TickResult:
        return cls(
            project_id=project_id,
            sim_now=report.sim_now.isoformat(),
            items_examined=report.items_examined,
            items_researched=report.items_researched,
            negotiations_opened=report.negotiations_opened,
            replies_filed=report.replies_filed,
            replies_skipped=report.replies_skipped,
            replies_after_stop=report.replies_after_stop,
            unmatched_replies=report.unmatched_replies,
            negotiations_examined=report.negotiations_examined,
            claims_lost=report.claims_lost,
            messages_sent=report.messages_sent,
            escalated=report.escalated,
            errors=report.errors,
        )


class TickResponse(BaseModel):
    projects: list[TickResult]


@app.get("/health")
async def health(request: Request) -> Health:
    """Says what is actually wired, not just that the process is alive.

    Named ``/health`` and not ``/healthz``, which is the reflex. On Cloud Run,
    ``/healthz`` never reaches the container: Google's front end answers it
    with its own HTML 404 while every neighbouring path is served normally.
    Confirmed on the deployed service — ``/openapi.json`` returned 200 and
    listed ``/healthz`` as a route, an unknown path returned our own JSON 404,
    and ``/healthz`` alone returned Google's. Nothing in the app or in
    ``deploy.sh`` can fix that; the path is simply not ours to use.

    "Up" is not the interesting question. "Is this about to email a real
    seller" and "is this the real brain or the fake" are.
    """
    settings = services_of(request).settings
    real_brain = settings.brain_backend is BrainBackend.MAIN_AGENT
    return Health(
        status="ok",
        brain_backend=settings.brain_backend.value,
        gemini_model=settings.gemini_model if real_brain else "",
        gemini_credentials=gemini_credentials_route() if real_brain else "",
        research_web_search=real_brain and bool(os.environ.get("PARALLEL_API_KEY")),
        mail_backend=settings.mail_backend.value,
        token_backend=settings.token_backend.value,
        project=settings.gcp_project,
    )


class CreateProject(BaseModel):
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    title: str
    owner_uid: str = ""
    """Whose production this is, and whose mailbox it negotiates from.

    Empty means nobody owns it, which makes it invisible to every browser —
    `firestore.rules` matches on this field. That is the right default: a
    project created without an owner should be unreachable rather than
    readable by whoever signs in next, which is precisely the bug this field
    exists to close.
    """
    budget_baseline: Money | None = None
    sim_start: datetime | None = None
    """Where simulated time starts. Defaults to real now, which is what live
    mode means. Tests and seeded demos set it explicitly."""


class ProjectCreated(BaseModel):
    project_id: str
    sim_now: str


class UploadScript(BaseModel):
    filename: str = "script.txt"
    mime_type: str = "text/plain"
    text_content: str = Field(min_length=1)


class FoundProp(BaseModel):
    """One prop, as offered back to the producer for confirmation."""

    item_id: str
    name: str
    category: str
    qty: int
    consumable: bool
    confidence: float
    scenes: list[str]
    lines: list[str]
    """The script lines it was found in. The receipt."""


class ScriptRead(BaseModel):
    props: list[FoundProp]


class ConfirmedItem(BaseModel):
    item_id: str
    qty: int = Field(ge=1, default=1)
    include: bool = True
    floor_price: Money | None = None


class ConfirmItems(BaseModel):
    items: list[ConfirmedItem]


class ItemsConfirmed(BaseModel):
    confirmed: list[str]
    abandoned: list[str]


@app.post("/projects", status_code=201)
async def create_project(request: Request, body: CreateProject) -> ProjectCreated:
    services = services_of(request)
    try:
        sim_start = await intake.create_project(
            services.repo,
            services.clock,
            project_id=body.project_id,
            title=body.title,
            owner_uid=body.owner_uid,
            sim_start=body.sim_start,
            budget_baseline=body.budget_baseline,
        )
    except AlreadyExists as exc:
        raise HTTPException(
            status_code=409, detail=f"project {body.project_id} already exists"
        ) from exc

    return ProjectCreated(project_id=body.project_id, sim_now=sim_start.isoformat())


@app.post("/projects/{project_id}/script")
async def upload_script(
    request: Request, project_id: str, body: UploadScript
) -> ScriptRead:
    """Read a screenplay and offer back the physical things the scenes need.

    The work is in ``intake.read_script``, shared with the producer-facing
    service, so the DRAFT gate that catches a hallucinated prop exists once
    rather than twice.
    """
    services = services_of(request)
    if await services.repo.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail=f"no project {project_id}")

    found = await intake.read_script(
        services.repo,
        services.clock,
        services.brain,
        project_id=project_id,
        source=ScriptSource(
            filename=body.filename,
            mime_type=body.mime_type,
            text_content=body.text_content,
        ),
    )
    return ScriptRead(props=[FoundProp(**asdict(prop)) for prop in found])


@app.post("/projects/{project_id}/items/confirm")
async def confirm_items(
    request: Request, project_id: str, body: ConfirmItems
) -> ItemsConfirmed:
    """The producer signs off the list. Only now does anything start moving."""
    services = services_of(request)
    try:
        result = await intake.confirm_items(
            services.repo,
            services.clock,
            project_id=project_id,
            choices=[
                intake.Choice(
                    item_id=c.item_id,
                    qty=c.qty,
                    include=c.include,
                    floor_price=c.floor_price,
                )
                for c in body.items
            ],
        )
    except intake.UnknownItemError as exc:
        raise HTTPException(status_code=404, detail=f"no item {exc.args[0]}") from exc

    return ItemsConfirmed(confirmed=result.confirmed, abandoned=result.abandoned)


@app.post("/tick")
async def tick(request: Request, project_id: str | None = None) -> TickResponse:
    """Advance the world by one pass.

    Every project unless one is named. Each is ticked independently and a
    failure in one is reported rather than raised, so a single broken project
    cannot stop the others from advancing — over a multi-day negotiation, a
    project that silently stops being ticked is a negotiation that dies.
    """
    services = services_of(request)
    project_ids = (
        [project_id]
        if project_id is not None
        else await services.repo.list_project_ids()
    )

    results: list[TickResult] = []
    for pid in project_ids:
        try:
            report = await services.loop.run_tick(
                pid, limit=services.settings.tick_limit
            )
        except Exception as exc:
            log.exception("tick failed", extra={"project_id": pid})
            results.append(
                TickResult.of(pid, TickReport(sim_now=NEVER, errors=[str(exc)]))
            )
            continue

        result = TickResult.of(pid, report)
        # The only record that this minute happened. Logged whether or not
        # anything moved: a run of empty ticks is how you tell "nothing was due"
        # apart from "the scheduler stopped calling", and those look identical
        # if only the interesting ticks are logged.
        log.info(
            "tick",
            extra=result.model_dump(exclude={"errors"})
            | {"error_count": len(report.errors)},
        )
        for message in report.errors:
            log.warning("tick error", extra={"project_id": pid, "detail": message})
        results.append(result)

    if not results:
        # A tick with no projects to tick used to log nothing at all, which made
        # the comment above a lie: a freshly deployed service and a Scheduler
        # that had stopped firing produced identical silence. Found on the real
        # deployment — Scheduler was running every minute and Cloud Logging had
        # not a single line, because nobody had uploaded a screenplay yet.
        log.info("tick", extra={"projects": 0, "note": "no projects exist yet"})

    return TickResponse(projects=results)
