"""Risk contracts (B06, B10). approval_required/execution_enabled are type-fixed (T14)."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from aqe.contracts.core import Disposition, RuleOutcome, StrictModel, UTCDateTime


class Position(StrictModel):
    instrument_id: str
    market_type: str
    quantity_base: float  # signed: + long / - short, in base units
    entry_price_quote: float | None = None


class PortfolioSnapshot(StrictModel):
    """A user-supplied account snapshot. Absence of a snapshot means UNKNOWN, never flat (T11)."""

    snapshot_id: str
    as_of: UTCDateTime
    equity_quote: float
    positions: tuple[Position, ...]
    daily_loss_quote: float = 0.0
    source: str = "user_supplied"


class RuleResult(StrictModel):
    rule_id: str
    order: int
    outcome: RuleOutcome
    reason: str
    recovery_condition: str | None = None
    caps_disposition_at: str | None = None  # Disposition this result imposes


class RiskDecision(StrictModel):
    analysis_id: str
    horizon_seconds: int
    ruleset_id: str
    ruleset_hash: str
    rules: tuple[RuleResult, ...]
    risk_outcome: RuleOutcome
    disposition: Disposition
    reasons: tuple[str, ...]
    portfolio_status: Literal["PROVIDED", "UNKNOWN"]
    approval_required: Literal[True] = True
    execution_enabled: Literal[False] = False
    decided_at: UTCDateTime
    valid_until: UTCDateTime

    @model_validator(mode="after")
    def _check(self) -> "RiskDecision":
        orders = [r.order for r in self.rules]
        if orders != sorted(orders):
            raise ValueError("rules must be recorded in evaluation order")
        if self.disposition == Disposition.REVIEW_CANDIDATE and self.risk_outcome != RuleOutcome.PASS:
            raise ValueError("REVIEW_CANDIDATE requires overall risk PASS")
        return self
