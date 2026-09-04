# The Firestore async client ships incomplete annotations: collection(),
# document(), stream() and friends come back as Unknown. Suppressing the
# unknown-type family here keeps the rest of the codebase under the strict
# settings rather than loosening them repo-wide for one dependency's sake.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportExplicitAny=false
"""Firestore access. The only module that knows document paths.

Also the home of the guardrail. ``create_purchase_order`` uses ``create()``
against a document keyed by item ID, so a duplicate order is refused by the
storage engine before any code here gets a chance to be clever about it. See
the docstring on that method.

Everything is a plain read-compute-write. Nothing is cached between calls, so
any handler using this repository is safe to kill mid-run — Hard Rule 3.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from cinema_contracts import TERMINAL_STATES, Money
from google.api_core.exceptions import Aborted, AlreadyExists, FailedPrecondition
from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter

from orchestrator.clock import ClockState
from orchestrator.records import (
    ITEM_TERMINAL_STATUSES,
    ItemRecord,
    ItemStatus,
    MailboxRecord,
    MailboxStatus,
    MessageRecord,
    NegotiationRecord,
    ProjectRecord,
    PurchaseOrderRecord,
    SupplierRecord,
)

if TYPE_CHECKING:
    from google.cloud.firestore_v1.async_document import AsyncDocumentReference

PROJECTS = "projects"
ITEMS = "items"
SUPPLIERS = "suppliers"
NEGOTIATIONS = "negotiations"
MESSAGES = "messages"
PURCHASE_ORDERS = "purchase_orders"
MAILBOXES = "mailboxes"
OAUTH_STATES = "oauth_states"


class DuplicateOrderError(RuntimeError):
    """Raised when a purchase order already exists for an item.

    Carries the item ID rather than a generic message because this surfaces in
    the UI as a guardrail moment, not as a stack trace. The demo deliberately
    triggers it.
    """

    item_id: str

    def __init__(self, item_id: str) -> None:
        super().__init__(
            f"A purchase order already exists for item {item_id}. "
            f"Refused by the database, not by application logic."
        )
        self.item_id = item_id


class DueNegotiation:
    """A negotiation the tick loop should act on, with its location.

    ``update_time`` is Firestore's version stamp for the document as it was
    read. It is what ``claim_negotiation`` compares against, and the reason a
    second overlapping tick cannot act on the same row.
    """

    project_id: str
    negotiation_id: str
    record: NegotiationRecord
    update_time: datetime

    def __init__(
        self,
        project_id: str,
        negotiation_id: str,
        record: NegotiationRecord,
        update_time: datetime,
    ) -> None:
        self.project_id = project_id
        self.negotiation_id = negotiation_id
        self.record = record
        self.update_time = update_time


class DueItem:
    """An item the tick loop should act on, with its location."""

    project_id: str
    item_id: str
    record: ItemRecord
    update_time: datetime

    def __init__(
        self,
        project_id: str,
        item_id: str,
        record: ItemRecord,
        update_time: datetime,
    ) -> None:
        self.project_id = project_id
        self.item_id = item_id
        self.record = record
        self.update_time = update_time


class FirestoreRepository:
    """Reads and writes every collection in the system.

    Satisfies ``orchestrator.clock.ClockStore``, so the clock persists through
    the same client as everything else without the clock module importing
    Firestore.
    """

    _db: AsyncClient

    def __init__(self, client: AsyncClient) -> None:
        self._db = client

    # ------------------------------------------------------------------ #
    # Claiming
    # ------------------------------------------------------------------ #

    async def _claim(
        self, ref: AsyncDocumentReference, update_time: datetime, until: datetime
    ) -> bool:
        """Push a due row forward, but only if nobody has touched it since.

        Cloud Scheduler does not wait for one ``/tick`` to finish before firing
        the next, so two ticks routinely read the same due row. Without this
        they would both ask the brain and both email the supplier — which looks
        exactly like the pestering bug already fixed in ``tick.py``, from an
        entirely different cause.

        The protection is Firestore's, not ours. Both ticks read the document at
        the same ``update_time`` and both attempt this conditional write; the
        storage engine admits exactly one and refuses the other with
        ``FAILED_PRECONDITION``. Same argument as ``create()`` on purchase
        orders: a check-then-write in our own code would have a race between the
        two halves, and this does not.

        Note what the *lease* is and is not. It is not what makes this safe —
        the compare-and-swap above is, at any clock speed, which is why DEMO
        mode needs no special handling. The lease only bounds how long a row
        sits idle if the winner is then killed mid-work.
        """
        try:
            _ = await ref.update(
                {"next_action_due_at": until},
                option=self._db.write_option(last_update_time=update_time),
            )
        except FailedPrecondition, Aborted:
            return False
        return True

    async def claim_negotiation(self, due: DueNegotiation, until: datetime) -> bool:
        """Take a negotiation for this tick. False means another tick has it."""
        return await self._claim(
            self._negotiation_ref(due.project_id, due.negotiation_id),
            due.update_time,
            until,
        )

    async def claim_item(self, due: DueItem, until: datetime) -> bool:
        """Take an item for this tick. False means another tick has it.

        Duplicated research is a wasted LLM call rather than a duplicated
        email, so the cost of losing this race is money instead of a supplier's
        goodwill. Worth claiming anyway, for the same reason.
        """
        return await self._claim(
            self._project_ref(due.project_id).collection(ITEMS).document(due.item_id),
            due.update_time,
            until,
        )

    # ------------------------------------------------------------------ #
    # Clock (implements ClockStore)
    # ------------------------------------------------------------------ #

    async def read(self, project_id: str) -> ClockState:
        project = await self.get_project(project_id)
        if project is None:
            raise KeyError(f"No clock for project {project_id!r}")
        return project.clock

    async def write(self, project_id: str, state: ClockState) -> None:
        ref = self._project_ref(project_id)
        _ = await ref.update({"clock": state.model_dump(mode="python")})

    # ------------------------------------------------------------------ #
    # Projects
    # ------------------------------------------------------------------ #

    def _project_ref(self, project_id: str) -> AsyncDocumentReference:
        return self._db.collection(PROJECTS).document(project_id)

    async def create_project(self, project_id: str, record: ProjectRecord) -> None:
        _ = await self._project_ref(project_id).create(record.to_firestore())

    async def list_project_ids(self) -> list[str]:
        """Every project id, sorted.

        The tick endpoint uses this when Cloud Scheduler calls it with no body:
        one scheduled call has to cover every production in the system, and
        the clock is advanced per project.
        """
        return sorted(
            [snapshot.id async for snapshot in self._db.collection(PROJECTS).stream()]
        )

    async def get_project(self, project_id: str) -> ProjectRecord | None:
        snapshot = await self._project_ref(project_id).get()
        data = snapshot.to_dict()
        return None if data is None else ProjectRecord.model_validate(data)

    async def rename_project(self, project_id: str, title: str) -> None:
        """Change what a production is called. Not what it is filed under.

        The document id stays. It is in the path of every item, supplier,
        negotiation and message underneath, and it is stored inside every
        purchase order — so following a retyped title would mean rewriting all
        of that, and losing whatever the rewrite missed. A production is
        allowed to be called one thing and filed under another; that is what a
        title is for.
        """
        _ = await self._project_ref(project_id).update({"title": title})

    async def delete_project(self, project_id: str) -> None:
        """Remove a production and everything underneath it.

        The subcollections are the point. Firestore does not delete children
        with their parent, and ``due_items`` and ``due_negotiations`` are
        *collection-group* queries — they find work by ``next_action_due_at``
        across the whole database, with no reference to which projects exist.
        Delete only the document and the tick stops ticking this production by
        name while its orphaned rows keep coming back through those queries, so
        the agent goes on emailing suppliers for a production its owner watched
        disappear.

        Deepest first, and the document last, so a run killed halfway has
        removed a suffix of the work rather than stranded it: the project is
        still listed, still owned, still visible, and running this again
        finishes the job. Doing it the other way round would orphan whatever
        was left with no way to find it from the panel.
        """
        project = self._project_ref(project_id)

        async for negotiation in project.collection(NEGOTIATIONS).list_documents():
            async for message in negotiation.collection(MESSAGES).list_documents():
                _ = await message.delete()
            _ = await negotiation.delete()

        for collection in (ITEMS, SUPPLIERS):
            async for document in project.collection(collection).list_documents():
                _ = await document.delete()

        _ = await project.delete()

    # ------------------------------------------------------------------ #
    # Mailboxes
    # ------------------------------------------------------------------ #
    #
    # Metadata only. The refresh token is in Secret Manager — see
    # MailboxRecord for why a credential does not go in here.

    async def get_mailbox(self, uid: str) -> MailboxRecord | None:
        snapshot = await self._db.collection(MAILBOXES).document(uid).get()
        data = snapshot.to_dict()
        return None if data is None else MailboxRecord.model_validate(data)

    async def save_mailbox(self, uid: str, rec: MailboxRecord) -> None:
        _ = await self._db.collection(MAILBOXES).document(uid).set(rec.to_firestore())

    async def mark_mailbox(
        self, uid: str, status: MailboxStatus, now: datetime
    ) -> None:
        """Record that a mailbox stopped working, without touching the rest.

        Called from the tick when a refresh is refused. A merge rather than a
        write of the whole record, because the tick is not the authority on
        which address is connected — only on whether it still works.
        """
        _ = await (
            self._db.collection(MAILBOXES)
            .document(uid)
            .set({"status": status.value, "updated_at": now}, merge=True)
        )

    # ------------------------------------------------------------------ #
    # OAuth state
    # ------------------------------------------------------------------ #
    #
    # The only thing binding an authorization code to a producer. The callback
    # arrives as a redirect from Google with no Authorization header, so there
    # is nothing else to check: whoever presents a live state value is treated
    # as the producer who started that consent.

    async def create_oauth_state(self, state: str, uid: str, now: datetime) -> None:
        """Record who began a consent. ``create``, so a value cannot be reused
        even by whoever minted it."""
        _ = (
            await self._db.collection(OAUTH_STATES)
            .document(state)
            .create({"uid": uid, "created_at": now})
        )

    async def claim_oauth_state(
        self, state: str, now: datetime, ttl: timedelta
    ) -> str | None:
        """Spend a state value, returning whose it was, or ``None``.

        Deleted before the caller does anything with it, and the delete is
        conditioned on the document's ``update_time``. Two callbacks racing the
        same state — a double-clicked popup, or a replay — both read it and
        both try; the storage engine admits exactly one. Filtering after the
        read would let both through, which is the same class of bug the tick's
        row claiming exists to stop.

        Expiry, an unknown value and one already spent are deliberately not
        distinguished: the caller has no legitimate use for the difference and
        an attacker probing for a live value does.
        """
        ref = self._db.collection(OAUTH_STATES).document(state)
        snapshot = await ref.get()
        data = snapshot.to_dict()
        if data is None:
            return None

        try:
            _ = await ref.delete(
                option=self._db.write_option(last_update_time=snapshot.update_time)
            )
        except FailedPrecondition:
            # Somebody else spent it between our read and our delete.
            return None

        created = data.get("created_at")
        if not isinstance(created, datetime) or now - created > ttl:
            return None
        return str(data.get("uid") or "") or None

    # ------------------------------------------------------------------ #
    # Items and suppliers
    # ------------------------------------------------------------------ #

    async def save_item(self, project_id: str, item_id: str, rec: ItemRecord) -> None:
        """Write an item, dropping the due field when it must not be picked up.

        Same trick as negotiations: a missing field leaves the document out of
        the index entirely. Two cases want that. Finished items are obvious.
        ``DRAFT`` is the important one — an item the producer has not confirmed
        yet must never be researched or emailed about, and enforcing that by
        absence from the queue is stronger than remembering to filter on it.
        """
        payload = rec.to_firestore()
        if rec.status in ITEM_TERMINAL_STATUSES or rec.status is ItemStatus.DRAFT:
            _ = payload.pop("next_action_due_at", None)
        ref = self._project_ref(project_id).collection(ITEMS).document(item_id)
        _ = await ref.set(payload)

    async def get_item(self, project_id: str, item_id: str) -> ItemRecord | None:
        ref = self._project_ref(project_id).collection(ITEMS).document(item_id)
        data = (await ref.get()).to_dict()
        return None if data is None else ItemRecord.model_validate(data)

    async def list_items(self, project_id: str) -> dict[str, ItemRecord]:
        collection = self._project_ref(project_id).collection(ITEMS)
        found: dict[str, ItemRecord] = {}
        async for snapshot in collection.stream():
            data = snapshot.to_dict()
            if data is not None:
                found[snapshot.id] = ItemRecord.model_validate(data)
        return found

    async def due_items(self, now: datetime, *, limit: int = 25) -> list[DueItem]:
        """Confirmed items waiting on research or on having negotiations opened.

        A second collection-group query alongside ``due_negotiations``, on the
        same shape of field. Deliberately the same pattern rather than a
        bespoke background job: one recovery story, one index shape, and the
        research step inherits the tick's killability for free.
        """
        query = (
            self._db.collection_group(ITEMS)
            .where(filter=FieldFilter("next_action_due_at", "<=", now))
            .order_by("next_action_due_at")
            .limit(limit)
        )

        due: list[DueItem] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if data is None:
                continue
            project_ref = snapshot.reference.parent.parent
            if project_ref is None:
                continue
            due.append(
                DueItem(
                    project_id=project_ref.id,
                    item_id=snapshot.id,
                    record=ItemRecord.model_validate(data),
                    update_time=snapshot.update_time,
                )
            )
        return due

    async def save_supplier(
        self, project_id: str, supplier_id: str, rec: SupplierRecord
    ) -> None:
        ref = self._project_ref(project_id).collection(SUPPLIERS).document(supplier_id)
        _ = await ref.set(rec.to_firestore())

    async def list_suppliers(self, project_id: str) -> dict[str, SupplierRecord]:
        collection = self._project_ref(project_id).collection(SUPPLIERS)
        found: dict[str, SupplierRecord] = {}
        async for snapshot in collection.stream():
            data = snapshot.to_dict()
            if data is not None:
                found[snapshot.id] = SupplierRecord.model_validate(data)
        return found

    async def get_supplier(
        self, project_id: str, supplier_id: str
    ) -> SupplierRecord | None:
        ref = self._project_ref(project_id).collection(SUPPLIERS).document(supplier_id)
        data = (await ref.get()).to_dict()
        return None if data is None else SupplierRecord.model_validate(data)

    # ------------------------------------------------------------------ #
    # Negotiations
    # ------------------------------------------------------------------ #

    def _negotiation_ref(
        self, project_id: str, negotiation_id: str
    ) -> AsyncDocumentReference:
        return (
            self._project_ref(project_id)
            .collection(NEGOTIATIONS)
            .document(negotiation_id)
        )

    async def save_negotiation(
        self, project_id: str, negotiation_id: str, rec: NegotiationRecord
    ) -> None:
        """Write a negotiation, dropping the due field once it is finished.

        Terminal negotiations have ``next_action_due_at`` deleted rather than
        set to null, which takes them out of the index the tick loop queries.
        Finished work then costs nothing to skip — there are no rows to read and
        no filter to maintain.
        """
        payload = rec.to_firestore()
        if rec.state in TERMINAL_STATES:
            _ = payload.pop("next_action_due_at", None)
        _ = await self._negotiation_ref(project_id, negotiation_id).set(payload)

    async def create_negotiation(
        self, project_id: str, negotiation_id: str, rec: NegotiationRecord
    ) -> bool:
        """Open a negotiation, or report that it already exists.

        ``create()`` rather than ``set()``, against a caller-chosen id derived
        from the item and supplier. Opening negotiations is the one step that
        fans out — one item becomes several — so a tick killed midway through
        would otherwise reopen the ones it had already created and email those
        suppliers twice.

        Returns False when the document was already there, which the caller
        treats as "someone got here first, nothing to do".
        """
        ref = self._negotiation_ref(project_id, negotiation_id)
        try:
            _ = await ref.create(rec.to_firestore())
        except AlreadyExists:
            return False
        return True

    async def get_negotiation(
        self, project_id: str, negotiation_id: str
    ) -> NegotiationRecord | None:
        data = (await self._negotiation_ref(project_id, negotiation_id).get()).to_dict()
        return None if data is None else NegotiationRecord.model_validate(data)

    async def list_negotiations(self, project_id: str) -> dict[str, NegotiationRecord]:
        collection = self._project_ref(project_id).collection(NEGOTIATIONS)
        found: dict[str, NegotiationRecord] = {}
        async for snapshot in collection.stream():
            data = snapshot.to_dict()
            if data is not None:
                found[snapshot.id] = NegotiationRecord.model_validate(data)
        return found

    async def due_negotiations(
        self, now: datetime, *, limit: int = 50
    ) -> list[DueNegotiation]:
        """Everything scheduled at or before ``now``, oldest first.

        A collection-group query, so one call covers every project. This is the
        only query in the system with an index behind it; see
        ``firestore.indexes.json``.

        ``limit`` bounds the work a single tick does. A tick that tried to drain
        an unbounded backlog would be the one thing most likely to be killed
        halfway, which is exactly what we are trying to avoid.
        """
        query = (
            self._db.collection_group(NEGOTIATIONS)
            .where(filter=FieldFilter("next_action_due_at", "<=", now))
            .order_by("next_action_due_at")
            .limit(limit)
        )

        due: list[DueNegotiation] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if data is None:
                continue
            project_ref = snapshot.reference.parent.parent
            if project_ref is None:
                continue  # orphaned document; nothing sensible to do with it
            due.append(
                DueNegotiation(
                    project_id=project_ref.id,
                    negotiation_id=snapshot.id,
                    record=NegotiationRecord.model_validate(data),
                    update_time=snapshot.update_time,
                )
            )
        return due

    async def live_thread_ids(self, project_id: str) -> frozenset[str]:
        """Every Gmail thread this project is currently negotiating in.

        The transport is given this so it only ever reads — and only ever marks
        read — mail in conversations we started. Without it, polling an inbox
        consumes whatever else happens to be unread in it, which is
        irreversible and, on a mailbox that is also somebody's personal one,
        destroys a hundred messages' worth of unread state.

        Scoped to one project, not to the whole system. It was a
        collection-group query while every project shared one mailbox, and that
        was right then. Now that a producer connects their own, the set has to
        match the mailbox being polled: handing project A's tick the thread ids
        of project B would have A's mailbox consume B's replies whenever the
        same person owns both — filed correctly, by the wrong project's tick,
        which is the kind of wrong that never shows up as a bug and quietly
        makes the ownership rule untrue.
        """
        query = (
            self._db.collection(PROJECTS)
            .document(project_id)
            .collection(NEGOTIATIONS)
            .where(filter=FieldFilter("gmail_thread_id", "!=", ""))
        )
        found: set[str] = set()
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if data is None:
                continue
            thread = str(data.get("gmail_thread_id") or "")
            if thread:
                found.add(thread)
        return frozenset(found)

    async def find_by_thread(
        self, project_id: str, thread_id: str
    ) -> DueNegotiation | None:
        """Locate a negotiation from an inbound Gmail thread ID.

        Inbound mail is routed by thread ID and never by subject line, because
        suppliers rewrite subjects and a subject match would file a reply
        against the wrong negotiation.

        Scoped to the project whose mailbox the message was read from, for the
        same reason ``live_thread_ids`` is.
        """
        if not thread_id:
            return None
        query = (
            self._db.collection(PROJECTS)
            .document(project_id)
            .collection(NEGOTIATIONS)
            .where(filter=FieldFilter("gmail_thread_id", "==", thread_id))
            .limit(1)
        )
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if data is None:
                continue
            return DueNegotiation(
                project_id=project_id,
                negotiation_id=snapshot.id,
                record=NegotiationRecord.model_validate(data),
                update_time=snapshot.update_time,
            )
        return None

    # ------------------------------------------------------------------ #
    # Messages
    # ------------------------------------------------------------------ #

    async def append_message(
        self,
        project_id: str,
        negotiation_id: str,
        message_id: str,
        rec: MessageRecord,
    ) -> bool:
        """Add one message to a negotiation's timeline. True if it was new.

        Keyed by Gmail message ID and written with ``create()``, so a message
        redelivered after a killed tick is refused rather than duplicating the
        conversation.

        The return value is what makes the *state transition* replay-safe too,
        not just the document. Filing a message is only half the work; the other
        half is applying QUOTE_RECEIVED to the negotiation. A caller that
        ignored this would re-apply that transition on every redelivery and
        burn a negotiation round for a reply the supplier only sent once.
        """
        ref = (
            self._negotiation_ref(project_id, negotiation_id)
            .collection(MESSAGES)
            .document(message_id)
        )
        try:
            _ = await ref.create(rec.to_firestore())
        except AlreadyExists:
            return False
        return True

    async def list_messages(
        self, project_id: str, negotiation_id: str
    ) -> list[MessageRecord]:
        collection = self._negotiation_ref(project_id, negotiation_id).collection(
            MESSAGES
        )
        query = collection.order_by("sim_sent_at")
        found: list[MessageRecord] = []
        async for snapshot in query.stream():
            data = snapshot.to_dict()
            if data is not None:
                found.append(MessageRecord.model_validate(data))
        return found

    # Purchase orders are deliberately absent from this class. They live in
    # OrdersRepository, against a different database. See its docstring.


class OrdersRepository:
    """Purchase orders. A separate class against a separate database.

    ## Why this is not a method on FirestoreRepository

    Two facts about Firestore, which only bite once deployed:

    1. **Security rules do not apply to server SDKs.** They govern the Firebase
       client SDKs — a browser holding a Firebase Auth token. A service account
       going through ``google-cloud-firestore`` bypasses ``firestore.rules``
       entirely, so a rule denying order writes constrains a producer's browser
       and constrains nothing whatsoever about the agent.
    2. **Firestore IAM has no collection-level granularity.**
       ``roles/datastore.user`` is all or nothing across a whole database.

    Together those mean a single database simply cannot express "this service
    account may write negotiations but not purchase orders". The smallest thing
    IAM can talk about is a database, so the boundary has to be a database.

    The tick service is granted access to ``(default)`` under an IAM condition
    and never constructs a client for this one. That makes Hard Rule 5 an
    infrastructure fact you can check with a single ``gcloud`` command, rather
    than a promise about our own code.

    Splitting the class as well as the database is belt and braces: the tick
    loop is handed a ``FirestoreRepository``, which has no method that could
    write an order even if the IAM binding were wrong.
    """

    _db: AsyncClient

    def __init__(self, client: AsyncClient) -> None:
        self._db = client

    async def create_purchase_order(self, rec: PurchaseOrderRecord) -> None:
        """File a purchase order, or refuse because one already exists.

        Three things make this the guardrail rather than a validation step:

        1. **The document ID is the item ID.** Not a generated order ID. So
           "order this item from a second supplier" is the same key as "order
           this item", and is refused for the same reason.
        2. **``create()``, never ``set()``.** ``set()`` overwrites; ``create()``
           fails with ``ALREADY_EXISTS`` if the document is there. The check and
           the write are one atomic operation inside Firestore, so there is no
           race between reading "does an order exist" and writing one.
        3. **The rules deny update and delete outright.** Even a caller holding
           producer credentials cannot rewrite or remove an order after the
           fact.

        None of that depends on this method behaving well. Delete the body and
        write the document by hand from a console and the second attempt still
        fails.
        """
        ref = self._db.collection(PURCHASE_ORDERS).document(rec.item_id)
        try:
            _ = await ref.create(rec.to_firestore())
        except AlreadyExists as exc:
            raise DuplicateOrderError(rec.item_id) from exc

    async def get_purchase_order(self, item_id: str) -> PurchaseOrderRecord | None:
        ref = self._db.collection(PURCHASE_ORDERS).document(item_id)
        data = (await ref.get()).to_dict()
        return None if data is None else PurchaseOrderRecord.model_validate(data)

    async def total_ordered(self) -> Money | None:
        """Sum of every purchase order, for the savings screen.

        Returns ``None`` when nothing has been ordered yet, rather than a zero
        whose currency would have to be guessed.
        """
        total: Money | None = None
        async for snapshot in self._db.collection(PURCHASE_ORDERS).stream():
            data = snapshot.to_dict()
            if data is None:
                continue
            price = PurchaseOrderRecord.model_validate(data).price
            total = price if total is None else total + price
        return total
