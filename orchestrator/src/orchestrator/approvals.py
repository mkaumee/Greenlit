# Starlette's app.state is an untyped attribute bag.
# pyright: reportAny=false
"""The money path. A separate service, on purpose.

    POST /items/{item_id}/approve        create the order, mark it ORDERED
    POST /negotiations/{nid}/floor       set a ceiling and hand it back
    POST /negotiations/{nid}/cancel      stop it
    GET  /health

## Why this is not a route on app.py

The tick service's account has no IAM binding on the ``orders`` database. That
absence *is* Hard Rule 5 — it is what makes "the agent cannot spend money" a
fact about IAM rather than a promise about our code. Adding an approval route
to `app.py` would mean granting that account orders access, and the claim would
quietly stop being true while every test still passed.

So this is a second ASGI app with its own entrypoint, deployed as its own Cloud
Run service under an account that *does* have orders access. It is the only
identity in the system that can write a purchase order, and it acts only on a
verified producer token.

``orchestrator/tests/test_app.py`` asserts the tick app exposes no approval
route, so bolting one on there fails the build.

## The order of the two writes

Approving touches two databases: the order goes in ``orders``, the negotiation
status in ``(default)``. There is no transaction across them, so one has to go
first and a crash in between has to leave something recoverable.

The order goes first. The other way round would leave a negotiation reading
``ORDERED`` with nothing behind it — something that looks bought and is not,
which is the worse of the two lies. This way a crash leaves a real order and a
negotiation still saying ``READY_FOR_HUMAN``, and the retry finishes the job.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from cinema_contracts import Money, NegotiationState
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud.firestore_v1 import AsyncClient
from pydantic import BaseModel

from orchestrator.auth import Producer, init_firebase, require_producer
from orchestrator.clock import SimClock
from orchestrator.logs import configure_logging
from orchestrator.records import ItemStatus, PurchaseOrderRecord
from orchestrator.repository import (
    DuplicateOrderError,
    FirestoreRepository,
    OrdersRepository,
)
from orchestrator.settings import Settings
from orchestrator.state_machine import (
    IllegalTransitionError,
    NegotiationEvent,
    apply_event,
)

log = logging.getLogger("orchestrator.approvals")


@dataclass(frozen=True, slots=True)
class ApprovalServices:
    """Both databases. This service is the only thing that holds them together."""

    settings: Settings
    client: AsyncClient
    orders_client: AsyncClient
    repo: FirestoreRepository
    orders: OrdersRepository
    clock: SimClock


def build_approval_services(settings: Settings | None = None) -> ApprovalServices:
    resolved = settings or Settings()
    client = AsyncClient(project=resolved.gcp_project)
    orders_client = AsyncClient(
        project=resolved.gcp_project, database=resolved.orders_database
    )
    repo = FirestoreRepository(client)
    return ApprovalServices(
        settings=resolved,
        client=client,
        orders_client=orders_client,
        repo=repo,
        orders=OrdersRepository(orders_client),
        clock=SimClock(repo),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = Settings()
    configure_logging(settings)
    services = build_approval_services(settings)
    init_firebase(services.settings)
    app.state.services = services
    log.info(
        "approvals up",
        extra={
            "project": services.settings.gcp_project,
            "orders_database": services.settings.orders_database,
        },
    )
    try:
        yield
    finally:
        services.client.close()
        services.orders_client.close()


app = FastAPI(title="Agentic Cinema approvals", lifespan=lifespan)


@app.exception_handler(IllegalTransitionError)
async def illegal_transition(_: Request, exc: IllegalTransitionError) -> JSONResponse:
    """A producer acting on a negotiation that has moved on is a 409, not a 500.

    Two screens open on the same negotiation is ordinary, and the second one is
    working from a stale view rather than doing anything wrong. The state
    machine's message already names the state and what would have been legal in
    it, which is what the caller needs to go and refresh.
    """
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def services_of(request: Request) -> ApprovalServices:
    services = getattr(request.app.state, "services", None)
    if services is None:  # pragma: no cover - only if lifespan was skipped
        raise HTTPException(status_code=503, detail="services not initialised")
    return services


ProducerDep = Annotated[Producer, Depends(require_producer)]
"""The verified human, injected. Every money-touching route takes one."""


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class Health(BaseModel):
    status: str
    project: str
    orders_database: str
    auth_emulator: str
    """Empty when verifying real signatures. Populated means unsigned tokens
    are being trusted, which is fine locally and alarming anywhere else."""


class Approve(BaseModel):
    project_id: str
    negotiation_id: str


class Approved(BaseModel):
    item_id: str
    negotiation_id: str
    price: Money
    approved_by: str
    already_existed: bool
    """True when this call completed an earlier attempt rather than creating
    the order. The caller does not need to care; the log does."""


class NegotiationRef(BaseModel):
    project_id: str


class SetFloor(BaseModel):
    project_id: str
    floor_price: Money


class NegotiationUpdated(BaseModel):
    negotiation_id: str
    state: str


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/health")
async def health(request: Request) -> Health:
    settings = services_of(request).settings
    return Health(
        status="ok",
        project=settings.gcp_project,
        orders_database=settings.orders_database,
        auth_emulator=settings.auth_emulator_host,
    )


@app.post("/items/{item_id}/approve")
async def approve(
    request: Request,
    item_id: str,
    body: Approve,
    producer: ProducerDep,
) -> Approved:
    """Buy it. The only route in the system that can.

    Refuses unless the negotiation is actually waiting for a person and has a
    price on the table — approving something still mid-conversation would order
    at a number nobody agreed.
    """
    services = services_of(request)
    record = await services.repo.get_negotiation(body.project_id, body.negotiation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such negotiation")
    if record.item_id != item_id:
        raise HTTPException(
            status_code=400,
            detail=f"negotiation {body.negotiation_id} is not for item {item_id}",
        )

    if record.state is NegotiationState.ORDERED:
        # A second click on a button that already worked. Both writes landed,
        # so there is nothing to do and nothing to complain about — reporting a
        # conflict here would put a red banner in front of a producer who did
        # everything right.
        #
        # Only when the order backing it is really there, though. A negotiation
        # reading ORDERED with nothing behind it is the failure this route's
        # write order exists to prevent, and it must not be waved through: that
        # falls to the refusal below, which names the state it is actually in.
        done = await services.orders.get_purchase_order(item_id)
        if done is not None and done.negotiation_id == body.negotiation_id:
            return Approved(
                item_id=item_id,
                negotiation_id=body.negotiation_id,
                price=done.price,
                approved_by=done.approved_by,
                already_existed=True,
            )

    if record.state is not NegotiationState.READY_FOR_HUMAN:
        raise HTTPException(
            status_code=409,
            detail=(
                f"negotiation is {record.state.value}, not READY_FOR_HUMAN. "
                f"Only a negotiation the agent has handed back can be approved."
            ),
        )
    if record.latest_quote is None:
        raise HTTPException(
            status_code=409, detail="no quote on this negotiation to approve"
        )

    now = await services.clock.now(body.project_id)
    price = record.latest_quote.unit_price
    already_existed = False

    try:
        await services.orders.create_purchase_order(
            PurchaseOrderRecord(
                item_id=item_id,
                project_id=body.project_id,
                supplier_id=record.supplier_id,
                negotiation_id=body.negotiation_id,
                price=price,
                approved_by=producer.uid,
                approved_at=now,
            )
        )
    except DuplicateOrderError:
        # Two very different situations arrive here and they must not be
        # conflated. Either a previous attempt by this same approval already
        # wrote the order and died before flipping the negotiation — in which
        # case finishing the job is exactly right — or someone is trying to buy
        # an item that has already been bought, which is the guardrail.
        existing = await services.orders.get_purchase_order(item_id)
        if existing is None or existing.negotiation_id != body.negotiation_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"item {item_id} has already been ordered"
                    + (
                        f" via negotiation {existing.negotiation_id}"
                        f" from supplier {existing.supplier_id}."
                        if existing is not None
                        else "."
                    )
                    + " Refused by the database, not by application logic."
                ),
            ) from None
        already_existed = True
        price = existing.price
        log.info("completing a half-finished approval", extra={"item_id": item_id})

    record.state = apply_event(record.state, NegotiationEvent.HUMAN_APPROVED)
    record.next_action_due_at = None
    record.updated_at = now
    await services.repo.save_negotiation(body.project_id, body.negotiation_id, record)

    item = await services.repo.get_item(body.project_id, item_id)
    if item is not None:
        item.status = ItemStatus.ORDERED
        item.chosen_quote = record.latest_quote
        item.next_action_due_at = None
        item.updated_at = now
        await services.repo.save_item(body.project_id, item_id, item)

    log.info(
        "purchase approved",
        extra={"item_id": item_id, "by": producer.display, "price": str(price)},
    )
    return Approved(
        item_id=item_id,
        negotiation_id=body.negotiation_id,
        price=price,
        approved_by=producer.uid,
        already_existed=already_existed,
    )


@app.post("/negotiations/{negotiation_id}/floor")
async def set_floor(
    request: Request,
    negotiation_id: str,
    body: SetFloor,
    producer: ProducerDep,
) -> NegotiationUpdated:
    """Not good enough. Here is my ceiling — go back and try again.

    The other half of the handoff, and the more interesting one: it is the
    producer changing the agent's instructions mid-negotiation rather than
    accepting or abandoning. Due immediately, so the next tick picks it up.
    """
    services = services_of(request)
    record = await services.repo.get_negotiation(body.project_id, negotiation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such negotiation")

    now = await services.clock.now(body.project_id)
    record.state = apply_event(record.state, NegotiationEvent.HUMAN_RETURNED_WITH_FLOOR)
    record.floor_price = body.floor_price
    record.escalation_reason = ""
    record.next_action_due_at = now
    record.updated_at = now
    await services.repo.save_negotiation(body.project_id, negotiation_id, record)

    log.info(
        "negotiation handed back with a floor",
        extra={"negotiation_id": negotiation_id, "by": producer.display},
    )
    return NegotiationUpdated(negotiation_id=negotiation_id, state=record.state.value)


@app.post("/negotiations/{negotiation_id}/cancel")
async def cancel(
    request: Request,
    negotiation_id: str,
    body: NegotiationRef,
    producer: ProducerDep,
) -> NegotiationUpdated:
    """Stop. A producer can always end a negotiation they started."""
    services = services_of(request)
    record = await services.repo.get_negotiation(body.project_id, negotiation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such negotiation")

    now = await services.clock.now(body.project_id)
    record.state = apply_event(record.state, NegotiationEvent.HUMAN_CANCELLED)
    record.next_action_due_at = None
    record.updated_at = now
    await services.repo.save_negotiation(body.project_id, negotiation_id, record)

    log.info(
        "negotiation cancelled",
        extra={"negotiation_id": negotiation_id, "by": producer.display},
    )
    return NegotiationUpdated(negotiation_id=negotiation_id, state=record.state.value)
