# httpx .json() and Starlette's app.state are untyped by nature, and
# urllib's parse_qs is annotated to return Unknown-keyed dicts.
# The `tokens` fixture is requested for its side effect — standing the Auth
# emulator up before init_firebase runs — so it is deliberately unread.
# pyright: reportAny=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnusedParameter=false
"""The producer's browser-facing service.

Most of this file is about one document. ``/mailbox/callback`` arrives as a
redirect from Google carrying no Authorization header, so the single-use state
value is the *only* thing binding an authorization code to a producer. If it
can be replayed, reused, or outlived, somebody attaches their mailbox to
another producer's account — or has the agent send as them.
"""

import base64
from datetime import UTC, datetime, timedelta
from typing import cast, final
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cinema_contracts import (
    AgentBrain,
    ClockMode,
    ExtractedQuote,
    Money,
    NegotiationState,
    ProducerBriefing,
    ProducerQuestion,
    SceneMention,
    ScriptSource,
)
from cinema_contracts.testing import ScriptedBrain
from conftest import TokenMinter
from google.cloud.firestore_v1 import AsyncClient
from orchestrator.api import MAX_REASON, ApiServices, app, build_api_services
from orchestrator.auth import init_firebase
from orchestrator.clock import ClockState, SimClock
from orchestrator.records import (
    ItemRecord,
    ItemStatus,
    NegotiationRecord,
    ProjectRecord,
    SupplierRecord,
)
from orchestrator.repository import FirestoreRepository
from orchestrator.settings import Settings

T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
UID = "producer-1"

SETTINGS = Settings(
    _env_file=None,  # pyright: ignore[reportCallIssue]
    gcp_project="demo-cinema",
    oauth_client_id="client-id.apps.googleusercontent.com",
    oauth_client_secret="client-secret",
    oauth_redirect_uri="https://api.example.test/mailbox/callback",
)


def _services(client: AsyncClient) -> ApiServices:
    """The api service with the deterministic brain in place of Gemini.

    ScriptedBrain is the same stand-in `make e2e` runs on, so these tests stay
    offline and a script upload still exercises the whole DRAFT gate.
    """
    repo = FirestoreRepository(client)
    return ApiServices(
        settings=SETTINGS,
        client=client,
        repo=repo,
        clock=SimClock(repo),
        brain=ScriptedBrain(),
    )


@pytest.fixture
async def api(firestore: AsyncClient, tokens: TokenMinter) -> httpx.AsyncClient:
    init_firebase(SETTINGS)
    app.state.services = _services(firestore)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _auth(
    tokens: TokenMinter, email: str = "producer@example.invalid"
) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens.mint(email, role='producer')}"}


async def test_health_says_which_brain_is_wired(api: httpx.AsyncClient) -> None:
    """The question "is the agent actually connected to the chat" needs an
    answer that is not "read the prose and guess".

    The tick has reported this since Phase 3 and this service did not, which is
    backwards: the tick is private, and this is the one a producer's browser
    talks to. A deployment still running the keyword matcher looks exactly like
    one that is not, right up until somebody asks it something interesting.
    """
    body = (await api.get("/health")).json()

    assert body["status"] == "ok"
    assert body["brain_backend"] == SETTINGS.brain_backend.value


async def test_health_does_not_report_what_this_service_does_not_do(
    api: httpx.AsyncClient,
) -> None:
    """No mail_backend, no research_web_search. This service sends no mail and
    searches nothing, and a health endpoint reporting fields its service does
    not use is one people stop reading."""
    body = (await api.get("/health")).json()

    assert "mail_backend" not in body
    assert "research_web_search" not in body


# --------------------------------------------------------------------------- #
# Starting a consent
# --------------------------------------------------------------------------- #


async def test_the_consent_url_asks_for_a_refresh_token_every_time(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    """Without prompt=consent Google returns a refresh token only on a first
    authorization — so reconnecting after the seven-day expiry would hand back
    an access token that dies in an hour, which looks like it worked until the
    next tick."""
    body = (await api.post("/mailbox/start", headers=_auth(tokens))).json()

    query = parse_qs(urlparse(body["authorize_url"]).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [SETTINGS.oauth_redirect_uri]


async def test_starting_a_consent_needs_a_producer(api: httpx.AsyncClient) -> None:
    assert (await api.post("/mailbox/start")).status_code == 401


async def test_each_consent_gets_its_own_state(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    # One token, two requests — the same producer connecting twice, which is
    # what reconnecting after the seven-day expiry looks like.
    headers = _auth(tokens)
    first = (await api.post("/mailbox/start", headers=headers)).json()
    second = (await api.post("/mailbox/start", headers=headers)).json()

    states = [
        parse_qs(urlparse(b["authorize_url"]).query)["state"][0]
        for b in (first, second)
    ]
    assert states[0] != states[1]
    assert len(states[0]) > 30, "guessable is the same as absent here"


# --------------------------------------------------------------------------- #
# The state document
# --------------------------------------------------------------------------- #


async def test_a_state_is_spent_exactly_once(firestore: AsyncClient) -> None:
    """The replay guard. Two callbacks with the same value — a double-clicked
    popup, or somebody replaying one — and only one may win."""
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("s-1", UID, T0)

    first = await repo.claim_oauth_state("s-1", T0, timedelta(minutes=10))
    second = await repo.claim_oauth_state("s-1", T0, timedelta(minutes=10))

    assert first == UID
    assert second is None


async def test_a_stale_state_is_refused(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("s-1", UID, T0)

    claimed = await repo.claim_oauth_state(
        "s-1", T0 + timedelta(minutes=11), timedelta(minutes=10)
    )

    assert claimed is None


async def test_an_unknown_state_is_refused(firestore: AsyncClient) -> None:
    repo = FirestoreRepository(firestore)

    assert (
        await repo.claim_oauth_state("never-minted", T0, timedelta(minutes=10)) is None
    )


async def test_a_spent_state_is_gone_from_storage(firestore: AsyncClient) -> None:
    """Deleted rather than flagged. A used value that still exists is one more
    thing that can be read back."""
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("s-1", UID, T0)

    _ = await repo.claim_oauth_state("s-1", T0, timedelta(minutes=10))

    snapshot = await firestore.collection("oauth_states").document("s-1").get()
    assert not snapshot.exists


# --------------------------------------------------------------------------- #
# The callback
# --------------------------------------------------------------------------- #


async def test_the_callback_refuses_an_unknown_state(api: httpx.AsyncClient) -> None:
    response = await api.get(
        "/mailbox/callback", params={"code": "x", "state": "not-a-real-state"}
    )

    assert response.status_code == 400
    assert "expired" in response.text


async def test_a_cancelled_consent_is_not_an_error(api: httpx.AsyncClient) -> None:
    """They pressed Cancel. Nothing is wrong and nothing has changed."""
    response = await api.get("/mailbox/callback", params={"error": "access_denied"})

    assert response.status_code == 200
    assert "Not connected" in response.text


async def test_the_callback_says_nothing_about_why_a_state_failed(
    api: httpx.AsyncClient, firestore: AsyncClient
) -> None:
    """Unknown, spent and expired read identically.

    A legitimate producer gains nothing from the difference and somebody
    probing for a live value gains a great deal.
    """
    repo = FirestoreRepository(firestore)
    await repo.create_oauth_state("spent", UID, T0)
    _ = await repo.claim_oauth_state("spent", T0, timedelta(minutes=10))

    unknown = await api.get("/mailbox/callback", params={"code": "x", "state": "nope"})
    spent = await api.get("/mailbox/callback", params={"code": "x", "state": "spent"})

    assert unknown.text == spent.text


# --------------------------------------------------------------------------- #
# The boundary this service exists to keep
# --------------------------------------------------------------------------- #


def test_this_service_cannot_spend_money() -> None:
    """The reason it is not a few more routes on the approvals service.

    That account holds an unconditioned datastore.user because approving writes
    a purchase order. Hanging chat and script upload off it would run all of
    them as the one identity that can spend. Same shape of guard as the one
    asserting the tick app exposes no /approve route.
    """
    paths = [getattr(route, "path", "") for route in app.routes]

    assert not [p for p in paths if "approve" in p or "purchase" in p], paths
    assert "orders" not in set(ApiServices.__dataclass_fields__)
    assert "orders_client" not in set(ApiServices.__dataclass_fields__)


def test_the_composition_root_builds_no_orders_client() -> None:
    services = build_api_services(SETTINGS)
    try:
        assert not hasattr(services, "orders")
    finally:
        services.client.close()


def test_the_api_service_holds_no_client_for_the_orders_database() -> None:
    """Belt and braces on the same boundary, one layer down.

    Even with a wrong IAM binding there is nothing in this service's
    composition root that could reach purchase orders — no orders client, and
    the repository it holds has no method that writes one.
    """
    fields = set(ApiServices.__dataclass_fields__)

    assert "orders" not in fields
    assert "orders_client" not in fields
    assert not [m for m in dir(FirestoreRepository) if "purchase_order" in m]


# --------------------------------------------------------------------------- #
# Talking to the agent
# --------------------------------------------------------------------------- #


async def _owned_project(
    firestore: AsyncClient, project_id: str, owner: str
) -> FirestoreRepository:
    repo = FirestoreRepository(firestore)
    await repo.create_project(
        project_id,
        ProjectRecord(
            title="Kopitiam",
            clock=ClockState(
                sim_now=T0, real_anchor=T0, speed=0.0, mode=ClockMode.FROZEN
            ),
            created_at=T0,
            owner_uid=owner,
        ),
    )
    await repo.save_item(
        project_id,
        "mirror",
        ItemRecord(
            name="Mirror",
            category="prop",
            status=ItemStatus.SOURCING,
            mentions=[
                SceneMention(scene_number="12", line="He threw it at the mirror")
            ],
        ),
    )
    await repo.save_supplier(
        project_id,
        "sup1",
        SupplierRecord(name="Ah Seng Rentals", email="s@example.invalid"),
    )
    await repo.save_negotiation(
        project_id,
        "neg1",
        NegotiationRecord(
            item_id="mirror",
            supplier_id="sup1",
            state=NegotiationState.READY_FOR_HUMAN,
            rounds_used=3,
            latest_quote=ExtractedQuote(
                unit_price=Money(amount=719, currency="MYR"),
                total=Money(amount=719, currency="MYR"),
            ),
            escalation_reason="GOOD_QUOTE",
            created_at=T0,
            updated_at=T0,
        ),
    )
    return repo


async def test_chat_answers_about_the_production(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The default wiring: the brain answers, and every reference is real.

    This asserted the deterministic summary until the brain was wired in.
    Stored facts are now the fallback rather than the default, and that path
    has its own test — what belongs here is that the ordinary path works and
    that `waiting_on_you` is right however the prose was produced, because the
    rail and the inspector read that number too.
    """
    owner = tokens.create("owner@example.invalid")
    headers = {
        "Authorization": f"Bearer {tokens.grant(owner, 'owner@example.invalid')}"
    }
    _ = await _owned_project(firestore, "kopitiam", owner)

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": "kopitiam", "question": "what needs me?"},
        )
    ).json()

    assert body["source"] == "agent"
    assert body["waiting_on_you"] == 1
    assert body["text"].strip()
    # Nothing invented: the digest is the boundary, and it carried these.
    assert {r["id"] for r in body["references"]} <= {"mirror", "neg1"}


async def test_chat_refuses_another_producers_project(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The api service reads through the admin SDK, which bypasses
    firestore.rules entirely — the same reason purchase orders needed their own
    database. So the ownership check has to be here as well, or a signed-in
    producer can summarise a rival production.
    """
    _ = await _owned_project(firestore, "someone-elses", "a-different-producer")

    response = await api.post(
        "/chat",
        headers=_auth(tokens, "outsider@example.invalid"),
        json={"project_id": "someone-elses", "question": "what needs me?"},
    )

    assert response.status_code == 404
    assert "Kopitiam" not in response.text, "not even the title leaks"


async def test_a_missing_project_and_a_stranger_look_identical(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Telling them apart would say whether a project id exists, which is not
    the asker's to learn."""
    _ = await _owned_project(firestore, "real-one", "a-different-producer")
    headers = _auth(tokens, "prober@example.invalid")

    existing = await api.post(
        "/chat", headers=headers, json={"project_id": "real-one", "question": "hi"}
    )
    absent = await api.post(
        "/chat", headers=headers, json={"project_id": "no-such", "question": "hi"}
    )

    assert existing.status_code == absent.status_code == 404


async def test_chat_needs_a_producer(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/chat", json={"project_id": "anything", "question": "hi"}
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Starting a production from a browser
# --------------------------------------------------------------------------- #
#
# The tick service has had these three steps since Phase 3, reachable only from
# a shell with a gcloud token and taking `owner_uid` as a plain string in the
# body. Here the caller is authenticated and the owner is the caller, which is
# the whole difference and the thing worth testing.

SCREENPLAY = """SCENE 1

INT. KOPITIAM - NIGHT

Razak grabs the cup and throws it at the mirror.
"""


def _script_body(text: str = SCREENPLAY) -> dict[str, object]:
    return {"filename": "script.txt", "mime_type": "text/plain", "text_content": text}


async def _start(
    api: httpx.AsyncClient, headers: dict[str, str], title: str = "Nightfall"
) -> str:
    reply = await api.post("/projects", headers=headers, json={"title": title})
    assert reply.status_code == 201, reply.text
    return str(reply.json()["project_id"])


async def test_a_production_belongs_to_whoever_started_it(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The owner is the token, not a body field.

    The tick service's version takes owner_uid as a string — correct there,
    since a shell calls it — and would be a way to create a production in
    somebody else's name from a browser.
    """
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}

    project_id = await _start(api, headers)

    record = await FirestoreRepository(firestore).get_project(project_id)
    assert record is not None
    assert record.owner_uid == uid


async def test_starting_a_production_needs_a_producer(api: httpx.AsyncClient) -> None:
    reply = await api.post("/projects", json={"title": "Nightfall"})

    assert reply.status_code == 401


async def test_two_productions_can_share_a_title(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    """`create()` refuses a clash rather than overwriting, so the second gets
    an id of its own instead of silently replacing the first's screenplay."""
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}

    first = await _start(api, headers)
    second = await _start(api, headers)

    assert first != second


async def test_a_script_becomes_draft_props_and_nothing_else(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """DRAFT is inert. Nothing is researched and nobody is emailed until a
    producer confirms — the gap where an invented prop is caught."""
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}
    project_id = await _start(api, headers)

    reply = await api.post(
        f"/projects/{project_id}/script", headers=headers, json=_script_body()
    )

    assert reply.status_code == 200, reply.text
    names = {p["name"] for p in reply.json()["props"]}
    assert {"cup", "mirror"} <= names

    items = await FirestoreRepository(firestore).list_items(project_id)
    assert items
    assert all(item.status is ItemStatus.DRAFT for item in items.values())
    assert all(item.next_action_due_at is None for item in items.values())


async def test_every_prop_carries_the_line_it_came_from(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    """The receipt. A prop with no line is one a producer cannot check, and is
    the cheapest possible tell for one that was never in the script."""
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}
    project_id = await _start(api, headers)

    props = (
        await api.post(
            f"/projects/{project_id}/script", headers=headers, json=_script_body()
        )
    ).json()["props"]

    assert props
    assert all(p["lines"] for p in props)


async def test_confirming_is_what_starts_the_work(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}
    project_id = await _start(api, headers)
    props = (
        await api.post(
            f"/projects/{project_id}/script", headers=headers, json=_script_body()
        )
    ).json()["props"]
    keep, *drop = sorted(str(p["item_id"]) for p in props)

    reply = await api.post(
        f"/projects/{project_id}/items/confirm",
        headers=headers,
        json={
            "items": [{"item_id": keep, "qty": 3, "include": True}]
            + [{"item_id": i, "include": False} for i in drop]
        },
    )

    assert reply.status_code == 200, reply.text
    repo = FirestoreRepository(firestore)
    kept = await repo.get_item(project_id, keep)
    assert kept is not None
    assert kept.status is ItemStatus.RESEARCHING
    assert kept.qty == 3
    # Due immediately: confirming is the producer saying go.
    assert kept.next_action_due_at is not None

    for item_id in drop:
        dropped = await repo.get_item(project_id, item_id)
        assert dropped is not None
        # Abandoned, not deleted — the breakdown still shows what the script
        # asked for and what was left out.
        assert dropped.status is ItemStatus.ABANDONED


async def test_a_stranger_cannot_upload_a_script_to_your_production(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    """The admin SDK bypasses firestore.rules, so this check is the boundary.

    404 rather than 403, matching /chat: distinguishing "not yours" from "no
    such project" tells a caller whether an id exists.
    """
    owner = tokens.create("owner@example.invalid")
    owner_headers = {
        "Authorization": f"Bearer {tokens.grant(owner, 'owner@example.invalid')}"
    }
    project_id = await _start(api, owner_headers)

    stranger = tokens.create("stranger@example.invalid")
    stranger_headers = {
        "Authorization": f"Bearer {tokens.grant(stranger, 'stranger@example.invalid')}"
    }

    upload = await api.post(
        f"/projects/{project_id}/script",
        headers=stranger_headers,
        json=_script_body(),
    )
    confirm = await api.post(
        f"/projects/{project_id}/items/confirm",
        headers=stranger_headers,
        json={"items": []},
    )

    assert upload.status_code == 404
    assert confirm.status_code == 404


async def test_a_pdf_reaches_the_brain_as_a_document(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Not flattened to text on the way.

    Gemini reads the file, which keeps the layout a screenplay depends on and
    handles a scanned script that no text extractor could. This service's job
    is to decide which field the upload belongs in and to send it on.
    """
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}
    project_id = await _start(api, headers)

    seen: list[ScriptSource] = []

    @final
    class _Watcher:
        def __getattr__(self, name: str) -> object:
            return getattr(ScriptedBrain(), name)

        async def extract_props(self, source: ScriptSource) -> list[object]:
            seen.append(source)
            return []

    _with_brain(firestore, _Watcher())
    encoded = base64.b64encode(b"%PDF-1.4 pretend screenplay").decode()

    reply = await api.post(
        f"/projects/{project_id}/script",
        headers=headers,
        json={
            "filename": "nightfall.pdf",
            "mime_type": "application/pdf",
            "content_b64": encoded,
        },
    )

    assert reply.status_code == 200, reply.text
    assert len(seen) == 1
    assert seen[0].content_b64 == encoded
    assert seen[0].text_content == ""


async def test_a_pdf_is_refused_when_the_brain_cannot_read_one(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    """The scripted brain is a keyword scan over text.

    Returning no props would be the failure this whole path guards against: a
    producer would see an empty list and conclude their screenplay needed
    nothing bought. The refusal names CINEMA_BRAIN_BACKEND, which is the fix.
    """
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}
    project_id = await _start(api, headers)

    reply = await api.post(
        f"/projects/{project_id}/script",
        headers=headers,
        json={
            "filename": "nightfall.pdf",
            "mime_type": "application/pdf",
            "content_b64": base64.b64encode(b"%PDF-1.4 anything").decode(),
        },
    )

    assert reply.status_code == 422
    assert "CINEMA_BRAIN_BACKEND" in reply.json()["detail"]


async def test_an_empty_script_is_refused(
    api: httpx.AsyncClient, tokens: TokenMinter
) -> None:
    uid = tokens.create("owner@example.invalid")
    headers = {"Authorization": f"Bearer {tokens.grant(uid, 'owner@example.invalid')}"}
    project_id = await _start(api, headers)

    reply = await api.post(
        f"/projects/{project_id}/script", headers=headers, json=_script_body("   \n")
    )

    assert reply.status_code == 422


# --------------------------------------------------------------------------- #
# The brain answering, and being checked
# --------------------------------------------------------------------------- #


@final
class _Briefer:
    """A brain whose brief_producer says whatever a test wants."""

    _briefing: ProducerBriefing | None
    asked: list[ProducerQuestion]

    def __init__(self, briefing: ProducerBriefing | None = None) -> None:
        self._briefing = briefing
        self.asked = []

    def __getattr__(self, name: str) -> object:
        # Everything except brief_producer comes from the scripted brain, so a
        # test about briefing does not have to care about the other four.
        return getattr(ScriptedBrain(), name)

    async def brief_producer(self, question: ProducerQuestion) -> ProducerBriefing:
        self.asked.append(question)
        if self._briefing is None:
            raise RuntimeError("the model is down")
        return self._briefing


def _with_brain(firestore: AsyncClient, brain: object) -> None:
    repo = FirestoreRepository(firestore)
    app.state.services = ApiServices(
        settings=SETTINGS,
        client=firestore,
        repo=repo,
        clock=SimClock(repo),
        brain=cast(AgentBrain, brain),
    )


async def _ask(
    api: httpx.AsyncClient, tokens: TokenMinter, firestore: AsyncClient
) -> tuple[dict[str, str], str]:
    owner = tokens.create("owner@example.invalid")
    headers = {
        "Authorization": f"Bearer {tokens.grant(owner, 'owner@example.invalid')}"
    }
    _ = await _owned_project(firestore, "kopitiam", owner)
    return headers, "kopitiam"


async def test_a_briefing_is_labelled_as_the_agent(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    headers, project_id = await _ask(api, tokens, firestore)
    _with_brain(
        firestore,
        _Briefer(
            ProducerBriefing(
                text="Skyline is worth one more push.",
                referenced_negotiation_ids=["neg1"],
            )
        ),
    )

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "should I push?"},
        )
    ).json()

    assert body["source"] == "agent"
    assert body["text"] == "Skyline is worth one more push."
    assert [r["id"] for r in body["references"]] == ["neg1"]


async def test_an_invented_reference_is_dropped_not_rendered(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The reason the digest was built as a boundary.

    A link to a prop that does not exist is worse than no link: a producer
    clicking it learns the agent is unreliable, having been given no way to
    tell which of the other statements were also invented. The prose still
    stands — a briefing that cites one bad id is usually right about the rest.
    """
    headers, project_id = await _ask(api, tokens, firestore)
    _with_brain(
        firestore,
        _Briefer(
            ProducerBriefing(
                text="Two suppliers are worth chasing.",
                referenced_item_ids=["mirror", "ghost-prop"],
                referenced_negotiation_ids=["neg1", "neg-that-never-was"],
            )
        ),
    )

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "who is quiet?"},
        )
    ).json()

    assert body["source"] == "agent"
    assert body["text"] == "Two suppliers are worth chasing."
    assert sorted(r["id"] for r in body["references"]) == ["mirror", "neg1"]


async def test_a_failing_brain_falls_back_and_says_so(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Both answers are true; only one reasoned.

    A silent fallback would leave a deployment whose brain is misconfigured
    looking exactly like one where it is not, and finding that out by noticing
    the prose feels flat is not a diagnosis.
    """
    headers, project_id = await _ask(api, tokens, firestore)
    _with_brain(firestore, _Briefer(None))

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "what needs me?"},
        )
    ).json()

    assert body["source"] == "stored-facts"
    # Still a real answer, read from Firestore rather than reasoned about.
    assert "Ah Seng Rentals" in body["text"]
    assert body["waiting_on_you"] == 1
    # And it says why, rather than leaving that in Cloud Logging.
    assert body["fallback_reason"] == "RuntimeError: the model is down"


async def test_an_empty_briefing_falls_back_rather_than_saying_nothing(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    headers, project_id = await _ask(api, tokens, firestore)
    _with_brain(firestore, _Briefer(ProducerBriefing(text="   ")))

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "what needs me?"},
        )
    ).json()

    assert body["source"] == "stored-facts"
    assert body["text"].strip()


async def test_the_brain_is_only_handed_the_producers_own_production(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """The digest is the boundary: what is not in it cannot be talked about."""
    headers, project_id = await _ask(api, tokens, firestore)
    brain = _Briefer(ProducerBriefing(text="Fine."))
    _with_brain(firestore, brain)

    _ = await api.post(
        "/chat",
        headers=headers,
        json={"project_id": project_id, "question": "how are we doing?"},
    )

    assert len(brain.asked) == 1
    question = brain.asked[0]
    assert question.project_id == project_id
    assert {n.negotiation_id for n in question.negotiations} == {"neg1"}


async def test_a_long_failure_is_cut_down_to_something_readable(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """A gRPC error can be kilobytes of retry metadata on one line.

    Two things shorten it and they are not the same: taking the first line, and
    capping what is left. A test whose first line is already short passes with
    the cap removed, so this one has no newline in it at all.
    """
    headers, project_id = await _ask(api, tokens, firestore)

    @final
    class _Verbose:
        def __getattr__(self, name: str) -> object:
            return getattr(ScriptedBrain(), name)

        async def brief_producer(self, _question: ProducerQuestion) -> ProducerBriefing:
            raise RuntimeError("503 Deadline exceeded. " + "retry metadata " * 300)

    _with_brain(firestore, _Verbose())

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "what needs me?"},
        )
    ).json()

    assert body["fallback_reason"].startswith("RuntimeError: 503 Deadline exceeded.")
    assert len(body["fallback_reason"]) == MAX_REASON


async def test_an_answer_from_the_agent_carries_no_reason(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Otherwise every good answer grows a disclaimer."""
    headers, project_id = await _ask(api, tokens, firestore)
    _with_brain(firestore, _Briefer(ProducerBriefing(text="Push Skyline once more.")))

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "should I push?"},
        )
    ).json()

    assert body["source"] == "agent"
    assert body["fallback_reason"] == ""


async def test_an_empty_briefing_says_it_was_empty(
    api: httpx.AsyncClient, firestore: AsyncClient, tokens: TokenMinter
) -> None:
    """Not an exception, and just as much a failure. Named separately because
    "empty answer" and "Vertex refused" need different fixes."""
    headers, project_id = await _ask(api, tokens, firestore)
    _with_brain(firestore, _Briefer(ProducerBriefing(text="   ")))

    body = (
        await api.post(
            "/chat",
            headers=headers,
            json={"project_id": project_id, "question": "what needs me?"},
        )
    ).json()

    assert body["source"] == "stored-facts"
    assert "empty" in body["fallback_reason"].lower()
