"""The Role A / Role B interface contract.

Import from here, not from submodules, so the public surface stays visible in
one place::

    from cinema_contracts import AgentBrain, Money, NegotiationContext, NextMove
"""

from cinema_contracts.enums import (
    TERMINAL_STATES,
    ClockMode,
    EscalationReason,
    MessageDirection,
    MoveAction,
    NegotiationState,
)
from cinema_contracts.models import (
    BriefingItem,
    BriefingNegotiation,
    ExtractedQuote,
    InboundMessage,
    ItemBrief,
    ItemResearch,
    MessageSummary,
    NegotiationContext,
    NextMove,
    ProducerBriefing,
    ProducerQuestion,
    PropDraft,
    QuoteExtraction,
    ReferenceBand,
    SceneMention,
    ScriptSource,
    SupplierCandidate,
)
from cinema_contracts.money import Currency, CurrencyMismatchError, Money
from cinema_contracts.protocols import AgentBrain

__all__ = [
    "TERMINAL_STATES",
    "AgentBrain",
    "BriefingItem",
    "BriefingNegotiation",
    "ClockMode",
    "Currency",
    "CurrencyMismatchError",
    "EscalationReason",
    "ExtractedQuote",
    "InboundMessage",
    "ItemBrief",
    "ItemResearch",
    "MessageDirection",
    "MessageSummary",
    "Money",
    "MoveAction",
    "NegotiationContext",
    "NegotiationState",
    "NextMove",
    "ProducerBriefing",
    "ProducerQuestion",
    "PropDraft",
    "QuoteExtraction",
    "ReferenceBand",
    "SceneMention",
    "ScriptSource",
    "SupplierCandidate",
]
