# Starlette's app.state is an untyped attribute bag.
# pyright: reportAny=false
"""What a producer's browser talks to. Everything except money.

    POST /mailbox/start        begin connecting a Gmail account
    GET  /mailbox/callback     where Google sends them back
    GET  /mailbox              is a mailbox connected, and does it still work
    GET  /health

## Why this is a third service

The tick service is deployed ``--no-allow-unauthenticated`` and reachable only
by Cloud Scheduler's OIDC token, so a browser cannot call it at all. The
approvals service *is* browser-reachable, and adding these routes there was
the obvious move — one fewer service, one fewer URL, no new IAM.

It is the wrong move. The approvals account holds an unconditioned
``datastore.user`` because approving writes a purchase order, and that account
is the only identity in the system that can. Hanging script upload and mailbox
management off it would mean every one of those handlers running as the
identity that can spend money — not exploitable today, and exactly the kind of
erosion that makes a guardrail stop meaning anything.

So this account gets ``datastore.user`` conditioned on ``(default)``, like the
tick service. It cannot write an order; IAM refuses. One small service touches
money and does nothing else, which is the claim worth keeping true.

## The callback cannot be authenticated

``/mailbox/callback`` is a redirect from Google. The browser follows it with no
Authorization header, and there is nowhere to put one. The single-use ``state``
document is therefore the *only* thing binding an authorization code to a
producer, which is why it is minted server-side against a verified token,
deleted the moment it is used, and refused once it is older than ten minutes.

Get that wrong and somebody who can replay a state value attaches their mailbox
to another producer's account — or worse, has the agent send as them.
"""

import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from cinema_contracts import AgentBrain, Money, ScriptSource
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1 import AsyncClient
from pydantic import BaseModel, Field

from orchestrator import intake
from orchestrator.app import build_brain, gemini_credentials_route
from orchestrator.auth import Producer, init_firebase, require_producer
from orchestrator.briefing import summarise
from orchestrator.clock import SimClock
from orchestrator.digest import ProjectDigest, as_question, build_digest
from orchestrator.gmail import (
    GmailTransport,
    client_credentials,
    producer_token_store,
)
from orchestrator.logs import configure_logging
from orchestrator.records import MailboxRecord, MailboxStatus, ProjectRecord
from orchestrator.repository import FirestoreRepository
from orchestrator.scripts import (
    PDF_MIME,
    UnreadableScriptError,
    check_document,
    decode_upload,
    is_document,
)
from orchestrator.settings import GMAIL_SCOPES, BrainBackend, Settings

log = logging.getLogger("orchestrator.api")

CONSENT_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

STATE_TTL = timedelta(minutes=10)
"""How long a consent may sit half-finished.

Long enough for somebody to read a consent screen carefully, short enough that
a state value captured from a browser's history is worthless by the time
anyone finds it.
"""


@dataclass(frozen=True, slots=True)
class ApiServices:
    """One database, and deliberately no orders client."""

    settings: Settings
    client: AsyncClient
    repo: FirestoreRepository
    clock: SimClock
    brain: AgentBrain
    """Reading a screenplay is a reasoning call, and this is the service a
    producer's browser can reach — so the brain lives here as well as on the
    tick. It never negotiates and never researches from this service: those
    stay on the tick, which is why this one needs no PARALLEL_API_KEY.

    It does need `roles/aiplatform.user` on the api service account. Without
    it every upload fails at request time rather than at startup, which is why
    `scripts/deploy.sh` grants it to both accounts."""


def build_api_services(settings: Settings | None = None) -> ApiServices:
    resolved = settings or Settings()
    client = AsyncClient(project=resolved.gcp_project)
    repo = FirestoreRepository(client)
    return ApiServices(
        settings=resolved,
        client=client,
        repo=repo,
        clock=SimClock(repo),
        # No research key, and none needed: this service reads screenplays and
        # briefs producers. `research_item` is the only capability that
        # searches the web and it runs on the tick, so a PARALLEL_API_KEY here
        # would be a credential in an environment with no use for it.
        brain=build_brain(resolved, needs_research=False),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = Settings()
    configure_logging(settings)
    services = build_api_services(settings)
    init_firebase(services.settings)
    app.state.services = services
    ALLOWED_ORIGINS[:] = services.settings.origin_list
    log.info("api up", extra={"project": services.settings.gcp_project})
    try:
        yield
    finally:
        services.client.close()


app = FastAPI(title="Greenlit API", lifespan=lifespan)

# Same reasoning as the approvals service: the panel is on Firebase Hosting and
# this is on Cloud Run, so every call is cross-origin. Filled in by lifespan
# rather than read at import, because settings are built once at startup.
ALLOWED_ORIGINS: list[str] = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# FastAPI's own idiom is a call in the default, which ruff's B008 rightly
# flags in general and wrongly here. Bound once at module level so the rule
# stays on everywhere else.
Signed = Annotated[Producer, Depends(require_producer)]


def services_of(request: Request) -> ApiServices:
    services = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - only if lifespan was skipped
        raise HTTPException(status_code=503, detail="services not initialised")
    return services


class Health(BaseModel):
    status: str
    project: str
    brain_backend: str = ""
    gemini_model: str = ""
    gemini_credentials: str = ""


@app.get("/health")
async def health(request: Request) -> Health:
    """Says which brain is wired, not just that the process is alive.

    The tick's ``/health`` has reported this since Phase 3 and this one did
    not, which is backwards: the tick is private and this is the service a
    producer's browser talks to. Without it, "is the agent actually connected
    to the chat" is a question you answer by reading the prose and guessing,
    and a deployment still running the keyword matcher looks exactly like one
    that is not.

    Deliberately narrower than the tick's. No ``mail_backend`` and no
    ``research_web_search``: this service sends no mail and searches nothing,
    and a health endpoint reporting fields its service does not use is one
    people stop believing.
    """
    settings = services_of(request).settings
    real_brain = settings.brain_backend is BrainBackend.MAIN_AGENT
    return Health(
        status="ok",
        project=settings.gcp_project,
        brain_backend=settings.brain_backend.value,
        gemini_model=settings.gemini_model if real_brain else "",
        gemini_credentials=gemini_credentials_route() if real_brain else "",
    )


# --------------------------------------------------------------------------- #
# Connecting a mailbox
# --------------------------------------------------------------------------- #


class ConsentStarted(BaseModel):
    authorize_url: str
    """Opened in a popup. Deliberately not a redirect from this endpoint — the
    panel stays where it is, and the popup closing is what signals completion."""


class MailboxState(BaseModel):
    connected: bool
    email: str = ""
    status: str = ""


def _redirect_uri(settings: Settings) -> str:
    if not settings.oauth_redirect_uri:
        raise HTTPException(
            status_code=503,
            detail=(
                "CINEMA_OAUTH_REDIRECT_URI is not configured, so no consent "
                "URL can be built. It must match a URI registered on the Web "
                "OAuth client exactly — see scripts/oauth_redirect_uri.sh."
            ),
        )
    return settings.oauth_redirect_uri


@app.post("/mailbox/start")
async def start_mailbox(request: Request, producer: Signed) -> ConsentStarted:
    """Mint a one-time state and hand back Google's consent URL.

    ``access_type=offline`` with ``prompt=consent`` because we need a refresh
    token every time: without ``prompt=consent`` Google returns one only on a
    user's first authorization, so reconnecting after the seven-day expiry
    would hand back an access token that dies in an hour and no way to renew
    it — which looks like it worked, until the next tick.
    """
    services = services_of(request)
    settings = services.settings
    client_id, _ = client_credentials(settings)
    if not client_id:
        raise HTTPException(
            status_code=503, detail="no OAuth client is configured on this service"
        )

    state = secrets.token_urlsafe(32)
    await services.repo.create_oauth_state(
        state, producer.uid, services.clock.real_now()
    )

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _redirect_uri(settings),
            "response_type": "code",
            "scope": " ".join(GMAIL_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    log.info("mailbox consent started", extra={"uid": producer.uid})
    return ConsentStarted(authorize_url=f"{CONSENT_URL}?{query}")


@app.get("/mailbox")
async def mailbox(request: Request, producer: Signed) -> MailboxState:
    """For the first paint, before the Firestore snapshot arrives."""
    record = await services_of(request).repo.get_mailbox(producer.uid)
    if record is None:
        return MailboxState(connected=False)
    return MailboxState(
        connected=record.status is MailboxStatus.CONNECTED,
        email=record.email,
        status=record.status.value,
    )


_DONE_PAGE = """<!doctype html>
<title>Mailbox connected</title>
<style>
  body { font: 15px system-ui, sans-serif; margin: 4rem auto; max-width: 30rem;
         text-align: center; color: #111; }
  .sub { color: #666; margin-top: .5rem; }
</style>
<p><strong>{headline}</strong></p>
<p class="sub">{detail}</p>
<script>
  // The panel is already watching mailboxes/{uid} in Firestore, so the write
  // this callback just made is what updates the card. Nothing to post back —
  // closing is the whole job.
  setTimeout(function () { window.close(); }, 1200);
</script>
"""


def _page(headline: str, detail: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _DONE_PAGE.replace("{headline}", headline).replace("{detail}", detail),
        status_code=status,
    )


@app.get("/mailbox/callback")
async def mailbox_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> HTMLResponse:
    """Where Google sends the producer back. Unauthenticated by necessity.

    Everything here is a page rather than JSON, because the reader is a human
    looking at a popup and not a caller parsing a body. Failures say what went
    wrong in the terms of the person who hit them.
    """
    services = services_of(request)
    settings = services.settings

    if error:
        # `access_denied` means they pressed Cancel, which is not a fault.
        log.info("consent declined", extra={"reason": error})
        return _page(
            "Not connected",
            "You cancelled, or Google refused. Nothing has changed — close "
            "this and try again when you are ready.",
        )

    if not code or not state:
        return _page(
            "Something is missing",
            "Google did not send back a code. Start again from the panel.",
            status=400,
        )

    claimed = await services.repo.claim_oauth_state(
        state, services.clock.real_now(), STATE_TTL
    )
    if claimed is None:
        # Unknown, already used, or expired — deliberately not distinguished.
        # Telling a caller which of the three would help somebody probing for
        # a live state value, and helps a legitimate producer not at all.
        log.warning("rejected an OAuth state")
        return _page(
            "That link has expired",
            "A connection link works once and lasts ten minutes. Start again "
            "from the panel.",
            status=400,
        )

    client_id, client_secret = client_credentials(settings)
    tokens = await _exchange(code, client_id, client_secret, _redirect_uri(settings))
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        # Google withholds it when the account has already granted these scopes
        # and prompt=consent was not honoured. Without one there is nothing to
        # store: an access token dies in an hour and the agent works for days.
        log.warning("consent returned no refresh token", extra={"uid": claimed})
        return _page(
            "Connected, but not durably",
            "Google did not return a refresh token, so the agent could not "
            "keep sending on your behalf. Remove Greenlit from your Google "
            "account permissions and connect again.",
            status=400,
        )

    address = await _address_of(tokens, settings)
    producer_token_store(settings, claimed).write(refresh_token)

    now = services.clock.real_now()
    await services.repo.save_mailbox(
        claimed,
        MailboxRecord(
            email=address,
            display_name=settings.agent_display_name,
            status=MailboxStatus.CONNECTED,
            connected_at=now,
            updated_at=now,
        ),
    )
    log.info("mailbox connected", extra={"uid": claimed, "email": address})
    return _page(
        "Mailbox connected",
        f"Greenlit will negotiate as {address}. You can close this window.",
    )


async def _exchange(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> dict[str, object]:
    """Trade the one-time code for tokens.

    ``httpx`` rather than the google-auth flow helper: the helper wants to own
    the whole redirect dance including a local listener, which is the shape of
    the Cloud Shell bootstrap and not of a web callback.
    """
    import httpx

    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if response.status_code != 200:
        # Google's body names the cause — redirect_uri_mismatch, invalid_client
        # — and it is worth having in the log even though the producer sees a
        # generic page.
        log.warning(
            "token exchange refused",
            extra={"status": response.status_code, "body": response.text[:300]},
        )
        raise HTTPException(
            status_code=502, detail="Google refused the authorization code"
        )
    body: dict[str, object] = response.json()
    return body


async def _address_of(tokens: dict[str, object], settings: Settings) -> str:
    """Which mailbox this credential actually opens.

    Asked of Gmail rather than taken from the Firebase account: a producer may
    sign in with one Google account and authorise a different one, and it is
    this address a supplier sees in their From line.
    """
    from google.oauth2.credentials import Credentials

    client_id, client_secret = client_credentials(settings)
    credentials = Credentials(
        token=str(tokens.get("access_token") or ""),
        refresh_token=str(tokens.get("refresh_token") or ""),
        token_uri=TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(GMAIL_SCOPES),
    )
    transport = GmailTransport.from_credentials(credentials, settings)
    return await transport.connected_address()


# --------------------------------------------------------------------------- #
# Starting a production
# --------------------------------------------------------------------------- #
#
# The same three steps the tick service has had since Phase 3, with two
# differences that are the whole reason they are duplicated as routes rather
# than shared as ones: the caller is authenticated, and the owner is the caller.
#
# The work itself is not duplicated. `orchestrator/intake.py` holds it, and both
# services call the same functions — the DRAFT gate that catches a hallucinated
# prop before it becomes an email exists once.


class NewProject(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    budget_baseline: Money | None = None


class ProjectStarted(BaseModel):
    project_id: str
    title: str


class UploadScript(BaseModel):
    """Either text, or a file. Not both, and not neither.

    A file arrives base64-encoded rather than as multipart because everything
    else this service speaks is JSON, and one encoding is worth the third of a
    payload it costs.
    """

    filename: str = "script.txt"
    mime_type: str = "text/plain"
    text_content: str = ""
    content_b64: str = ""


class FoundProp(BaseModel):
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


async def _owned(
    services: ApiServices, project_id: str, producer: Producer
) -> ProjectRecord:
    """Same check `_digest_for` makes, for the routes that do not build one.

    The admin SDK bypasses `firestore.rules` entirely, so this is the boundary
    for anything reaching Firestore from here — the rules constrain the
    browser, not this service. A missing project and somebody else's return the
    same 404 on purpose: distinguishing them tells a caller whether an id
    exists, which is not theirs to learn.
    """
    project = await services.repo.get_project(project_id)
    if project is None or project.owner_uid != producer.uid:
        raise HTTPException(status_code=404, detail=f"no project {project_id}")
    return project


@app.post("/projects", status_code=201)
async def start_project(
    request: Request, body: NewProject, producer: Signed
) -> ProjectStarted:
    """Start a production, owned by whoever asked for it.

    The owner is the authenticated caller and is not a field on the body. The
    tick service's version takes `owner_uid` as a string, which is right there
    — it is called by a shell with a gcloud token — and would be a way to
    create a production in somebody else's name here.
    """
    services = services_of(request)
    project_id = _project_id_for(body.title)

    for candidate in _candidates(project_id):
        try:
            _ = await intake.create_project(
                services.repo,
                services.clock,
                project_id=candidate,
                title=body.title,
                owner_uid=producer.uid,
                budget_baseline=body.budget_baseline,
            )
        except AlreadyExists:
            # `create()` refuses a clash rather than overwriting, so two
            # productions called "Nightfall" get distinct ids instead of one
            # quietly replacing the other's screenplay.
            continue
        log.info(
            "project started",
            extra={"project_id": candidate, "uid": producer.uid},
        )
        return ProjectStarted(project_id=candidate, title=body.title)

    raise HTTPException(
        status_code=409,
        detail=(
            "Too many productions with that name already. Give this one a "
            "title of its own."
        ),
    )


@app.post("/projects/{project_id}/script")
async def upload_script(
    request: Request, project_id: str, body: UploadScript, producer: Signed
) -> ScriptRead:
    """Read a screenplay into DRAFT props for the producer to confirm.

    Nothing is researched and nobody is emailed until they do. That gap is
    where a prop the model invented is caught, before it becomes a real message
    to a real seller.
    """
    services = services_of(request)
    _ = await _owned(services, project_id, producer)

    try:
        source = _source_for(body)
    except UnreadableScriptError as exc:
        # 422 rather than 400: the request was well formed, the file was not
        # something that can be sent on. The message is written for the
        # producer and is rendered as-is.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        found = await intake.read_script(
            services.repo,
            services.clock,
            services.brain,
            project_id=project_id,
            source=source,
        )
    except ValueError as exc:
        # The scripted brain refusing a document it cannot read. Its message
        # names CINEMA_BRAIN_BACKEND, which is the actual fix, so it goes
        # through to the producer rather than becoming a 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    log.info(
        "script read",
        extra={"project_id": project_id, "uid": producer.uid, "props": len(found)},
    )
    return ScriptRead(props=[FoundProp(**asdict(prop)) for prop in found])


@app.post("/projects/{project_id}/items/confirm")
async def confirm_items(
    request: Request, project_id: str, body: ConfirmItems, producer: Signed
) -> ItemsConfirmed:
    """The producer signs off the list. Only now does anything start moving."""
    services = services_of(request)
    _ = await _owned(services, project_id, producer)

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

    log.info(
        "items confirmed",
        extra={
            "project_id": project_id,
            "uid": producer.uid,
            "confirmed": len(result.confirmed),
            "abandoned": len(result.abandoned),
        },
    )
    return ItemsConfirmed(confirmed=result.confirmed, abandoned=result.abandoned)


def _source_for(body: UploadScript) -> ScriptSource:
    """Text stays text; a document travels as bytes.

    A PDF is not flattened here. Role A hands it to Gemini as an attachment,
    which keeps the layout a screenplay depends on and reads a scanned one that
    no text extractor could. All this decides is which of the two fields the
    upload belongs in.
    """
    if body.content_b64 and is_document(
        filename=body.filename, mime_type=body.mime_type
    ):
        check_document(filename=body.filename, content_b64=body.content_b64)
        return ScriptSource(
            filename=body.filename,
            mime_type=body.mime_type or PDF_MIME,
            content_b64=body.content_b64,
        )

    text = (
        decode_upload(filename=body.filename, content_b64=body.content_b64)
        if body.content_b64
        else body.text_content
    )
    if not text.strip():
        raise UnreadableScriptError("There is no screenplay text to read.")

    return ScriptSource(
        filename=body.filename,
        mime_type=body.mime_type,
        text_content=text,
    )


def _project_id_for(title: str) -> str:
    """A readable id from the title.

    Readable because it is in every log line, every Firestore path and the URL
    a producer might paste to a colleague. A uuid would never collide and would
    make all of those unreadable, so this collides and handles it instead.
    """
    slug = "".join(c if c.isalnum() else "-" for c in title.lower())
    slug = "-".join(part for part in slug.split("-") if part)[:48]
    # The pattern the tick service's route enforces: must start alphanumeric,
    # at least two characters.
    return slug if len(slug) >= 2 and slug[0].isalnum() else f"p-{slug}"[:48]


def _candidates(base: str) -> list[str]:
    """The id, then a few numbered variants. Bounded, so a loop cannot run away."""
    return [base] + [f"{base}-{n}" for n in range(2, 12)]


# --------------------------------------------------------------------------- #
# Talking to the agent
# --------------------------------------------------------------------------- #


class Ask(BaseModel):
    project_id: str
    question: str = Field(min_length=1, max_length=2000)


class Reference(BaseModel):
    """Something the answer pointed at, that the panel renders as a link."""

    kind: str
    id: str
    label: str


class Answer(BaseModel):
    text: str
    references: list[Reference] = Field(default_factory=list)
    waiting_on_you: int
    """How many decisions are sitting at READY_FOR_HUMAN right now.

    Returned on every answer rather than only when asked. The chat is one view
    of that number and never the only one — a purchase decision that exists
    solely in a transcript is a purchase decision that scrolls away.
    """

    source: str = "stored-facts"
    """Which half answered: ``agent`` or ``stored-facts``.

    On the wire because the difference matters to whoever is reading. Both
    answers are true; only one of them reasoned, and a deployment where the
    brain is misconfigured otherwise looks exactly like one where it is not.
    Finding that out by noticing the prose feels flat is not a diagnosis.
    """


async def _digest_for(
    services: ApiServices, project_id: str, producer: Producer
) -> ProjectDigest:
    """The facts this producer may be told about, and no others.

    The ownership check is here rather than in the rules alone because the api
    service reaches Firestore through the admin SDK, which bypasses
    ``firestore.rules`` entirely — the same reason purchase orders needed their
    own database. A signed-in producer asking about somebody else's production
    gets a 404, not a summary of it.
    """
    project = await services.repo.get_project(project_id)
    if project is None or project.owner_uid != producer.uid:
        # Deliberately not distinguishing "no such project" from "not yours".
        # The difference tells a caller whether a project id exists, which is
        # not theirs to learn.
        raise HTTPException(status_code=404, detail=f"no project {project_id}")

    items = await services.repo.list_items(project_id)
    negotiations = await services.repo.list_negotiations(project_id)

    names: dict[str, str] = {}
    for record in negotiations.values():
        if record.supplier_id in names:
            continue
        supplier = await services.repo.get_supplier(project_id, record.supplier_id)
        names[record.supplier_id] = supplier.name if supplier else record.supplier_id

    return build_digest(project_id, project.title, items, negotiations, names)


@app.post("/chat")
async def chat(request: Request, body: Ask, producer: Signed) -> Answer:
    """Answer a question about a production.

    The brain reasons; the stored facts are the floor. If the brain is
    unavailable or returns something unusable, this falls back to the
    deterministic summary — which is true, just not reasoned — and **says which
    one answered**. A silent fallback would leave the screen looking exactly as
    it does when everything is wired, which is the failure this codebase keeps
    having to design against.
    """
    services = services_of(request)
    digest = await _digest_for(services, body.project_id, producer)

    reasoned = await _reasoned(services, digest, body.question)
    if reasoned is not None:
        text, references = reasoned
        source = "agent"
    else:
        text, references = summarise(digest, body.question)
        source = "stored-facts"

    log.info(
        "chat",
        extra={
            "project_id": body.project_id,
            "uid": producer.uid,
            "source": source,
        },
    )
    return Answer(
        text=text,
        references=[Reference(kind=k, id=i, label=lbl) for k, i, lbl in references],
        waiting_on_you=digest.waiting_count,
        source=source,
    )


async def _reasoned(
    services: ApiServices, digest: ProjectDigest, question: str
) -> tuple[str, list[tuple[str, str, str]]] | None:
    """Ask the brain, and check what comes back. ``None`` means fall back.

    Every id is checked against the digest that was handed over — which is the
    reason the digest was built as a boundary. An id the digest never carried
    was invented, and is dropped rather than rendered as a link to a prop that
    does not exist. The prose still stands: a briefing that cites one bad id is
    usually right about everything else, and discarding the whole answer would
    lose more than it protects.
    """
    try:
        briefing = await services.brain.brief_producer(as_question(digest, question))
    except Exception:
        # Any failure at all — a model that is down, a schema that did not
        # validate, a timeout. The deterministic summary is a true answer, so
        # there is always something to say; what must not happen is saying
        # nothing, or saying it as though the brain had spoken.
        log.warning("briefing failed, falling back to stored facts", exc_info=True)
        return None

    text = briefing.text.strip()
    if not text:
        return None

    known = digest.known_ids()
    item_names = {i.item_id: i.name for i in digest.items}
    talk_names = {n.negotiation_id: n.item_name for n in digest.negotiations}

    references: list[tuple[str, str, str]] = []
    invented = 0
    for item_id in briefing.referenced_item_ids:
        if item_id in known:
            references.append(("item", item_id, item_names.get(item_id, item_id)))
        else:
            invented += 1
    for negotiation_id in briefing.referenced_negotiation_ids:
        if negotiation_id in known:
            references.append(
                ("negotiation", negotiation_id, talk_names.get(negotiation_id, ""))
            )
        else:
            invented += 1

    if invented:
        log.warning(
            "briefing cited ids the digest never carried",
            extra={"project_id": digest.project_id, "invented": invented},
        )
    return text, references
