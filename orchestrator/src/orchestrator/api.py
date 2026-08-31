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
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from google.cloud.firestore_v1 import AsyncClient
from pydantic import BaseModel

from orchestrator.auth import Producer, init_firebase, require_producer
from orchestrator.clock import SimClock
from orchestrator.gmail import (
    GmailTransport,
    client_credentials,
    producer_token_store,
)
from orchestrator.logs import configure_logging
from orchestrator.records import MailboxRecord, MailboxStatus
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import GMAIL_SCOPES, Settings

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


def build_api_services(settings: Settings | None = None) -> ApiServices:
    resolved = settings or Settings()
    client = AsyncClient(project=resolved.gcp_project)
    repo = FirestoreRepository(client)
    return ApiServices(
        settings=resolved, client=client, repo=repo, clock=SimClock(repo)
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


@app.get("/health")
async def health(request: Request) -> Health:
    return Health(status="ok", project=services_of(request).settings.gcp_project)


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
