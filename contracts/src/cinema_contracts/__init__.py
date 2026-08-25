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
    BreakdownSource,
    ExtractedQuote,
    InboundMessage,
    ItemBrief,
    ItemDraft,
    ItemResearch,
    MessageSummary,
    NegotiationContext,
    NextMove,
    QuoteExtraction,
    ReferenceBand,
    SceneMention,
    SupplierCandidate,
)
from cinema_contracts.money import Currency, CurrencyMismatchError, Money
from cinema_contracts.protocols import AgentBrain

__all__ = [
    "TERMINAL_STATES",
    "AgentBrain",
    "BreakdownSource",
    "ClockMode",
    "Currency",
    "CurrencyMismatchError",
    "EscalationReason",
    "ExtractedQuote",
    "InboundMessage",
    "ItemBrief",
    "ItemDraft",
    "ItemResearch",
    "MessageDirection",
    "MessageSummary",
    "Money",
    "MoveAction",
    "NegotiationContext",
    "NegotiationState",
    "NextMove",
    "QuoteExtraction",
    "ReferenceBand",
    "SceneMention",
    "SupplierCandidate",
]
