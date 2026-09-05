"""Domain model for three-way payment reconciliation.

Money discipline
----------------
Every monetary value in this system is an ``int`` count of **paise**. Floats are
never used for money. A settlement of INR 1,23,456.78 is ``12345678``.

This is not pedantry. Reconciliation is the act of asserting that two
independently-computed sums are equal, and binary floating point cannot
represent 0.01 exactly. A pipeline built on floats produces drift that is
indistinguishable from a genuine break, which destroys the only signal the
system exists to produce.

Sign convention
---------------
``BankTxn.amount_paise`` is signed: credits are positive, debits are negative.
Every other line item stores a positive magnitude and carries its direction in
its type. So ``RefundLine.amount_paise`` is positive, and the netting formula
subtracts it. Keeping magnitudes positive means a negative number anywhere in a
line item is a generator bug, which makes it cheap to assert against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Iterable

# Indian Standard Time. Settlement cycles, banking hours and value dates are all
# expressed in IST, so anchoring the whole system to it avoids an entire class of
# off-by-one-day breaks around midnight UTC.
IST = timezone(timedelta(hours=5, minutes=30))


def ist(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build an IST-aware datetime. Used by the generator and the tests."""
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def rupees(paise: int) -> str:
    """Format paise for human-readable reports, Indian digit grouping.

    ``12345678`` becomes ``1,23,456.78``. The Indian grouping convention puts the
    first separator after three digits and every two digits thereafter, which no
    stdlib locale call reliably produces on all platforms, so it is done by hand.
    """
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts + [tail])
    return f"{sign}{grouped}.{frac:02d}"


# ---------------------------------------------------------------------------
# Source 1: the merchant's own order ledger / general ledger
# ---------------------------------------------------------------------------


class OrderStatus(str, Enum):
    CREATED = "created"
    PAID = "paid"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Order:
    """A row in the merchant's order book. The merchant's version of truth."""

    order_id: str
    merchant_ref: str
    customer_id: str
    amount_paise: int
    currency: str
    created_at: datetime
    status: OrderStatus


# ---------------------------------------------------------------------------
# Source 2: the Razorpay settlement report (line items + settlement headers)
# ---------------------------------------------------------------------------


class LineKind(str, Enum):
    PAYMENT = "payment"
    REFUND = "refund"
    CHARGEBACK = "chargeback"
    ADJUSTMENT = "adjustment"


@dataclass(frozen=True)
class PaymentLine:
    """A captured payment as it appears in the settlement report.

    ``net_paise`` is what actually reaches the merchant's bank for this payment:
    gross, less the platform fee, less GST on that fee.
    """

    payment_id: str
    order_id: str | None
    gross_paise: int
    fee_paise: int
    tax_paise: int
    captured_at: datetime
    method: str
    currency: str = "INR"
    settlement_id: str | None = None

    @property
    def net_paise(self) -> int:
        return self.gross_paise - self.fee_paise - self.tax_paise


@dataclass(frozen=True)
class RefundLine:
    """A refund. ``amount_paise`` is a positive magnitude that nets *out*."""

    refund_id: str
    payment_id: str
    amount_paise: int
    created_at: datetime
    is_partial: bool = False
    settlement_id: str | None = None


@dataclass(frozen=True)
class ChargebackLine:
    """A dispute debit. Both the disputed amount and the bank's dispute fee net out."""

    dispute_id: str
    payment_id: str
    amount_paise: int
    fee_paise: int
    created_at: datetime
    settlement_id: str | None = None


@dataclass(frozen=True)
class AdjustmentLine:
    """A signed manual adjustment. The one line item where sign is meaningful."""

    adjustment_id: str
    amount_paise: int
    description: str
    created_at: datetime
    settlement_id: str | None = None


class SettlementKind(str, Enum):
    REGULAR = "regular"
    INSTANT = "instant"


@dataclass(frozen=True)
class Settlement:
    """A settlement header as reported by the gateway.

    ``net_paise`` is the gateway's *reported* net. The reconciler never trusts it
    blindly: it recomputes the net from the constituent line items and treats any
    difference as a finding in its own right.
    """

    settlement_id: str
    utr: str
    net_paise: int
    settled_at: datetime
    kind: SettlementKind = SettlementKind.REGULAR


# ---------------------------------------------------------------------------
# Source 3: the bank statement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BankTxn:
    """A line on the merchant's bank statement.

    ``utr_hint`` is what a narration parser managed to extract, which is often
    nothing and is sometimes wrong. Treating it as a hint rather than a key is
    the difference between a reconciler that works on real statements and one
    that works only on clean fixtures.
    """

    bank_txn_id: str
    value_date: date
    amount_paise: int  # signed: credit positive, debit negative
    narration: str
    utr_hint: str | None = None
    balance_paise: int | None = None

    @property
    def is_credit(self) -> bool:
        return self.amount_paise > 0


# ---------------------------------------------------------------------------
# The assembled ledger
# ---------------------------------------------------------------------------


@dataclass
class Ledger:
    """All three sources, indexed for lookup.

    This is the single object the matching passes read and the verifier
    re-checks against. Nothing in the pipeline reads a CSV directly.
    """

    orders: dict[str, Order] = field(default_factory=dict)
    payments: dict[str, PaymentLine] = field(default_factory=dict)
    refunds: dict[str, RefundLine] = field(default_factory=dict)
    chargebacks: dict[str, ChargebackLine] = field(default_factory=dict)
    adjustments: dict[str, AdjustmentLine] = field(default_factory=dict)
    settlements: dict[str, Settlement] = field(default_factory=dict)
    bank_txns: dict[str, BankTxn] = field(default_factory=dict)

    # -- derived indexes ---------------------------------------------------

    def lines_for_settlement(self, settlement_id: str) -> dict[str, list]:
        """Every line item the gateway attributed to one settlement."""
        return {
            "payments": [p for p in self.payments.values() if p.settlement_id == settlement_id],
            "refunds": [r for r in self.refunds.values() if r.settlement_id == settlement_id],
            "chargebacks": [
                c for c in self.chargebacks.values() if c.settlement_id == settlement_id
            ],
            "adjustments": [
                a for a in self.adjustments.values() if a.settlement_id == settlement_id
            ],
        }

    def computed_net_paise(self, settlement_id: str) -> int:
        """Recompute a settlement's net from its line items.

        This is the netting identity the whole exercise turns on::

            net = sum(payment.gross - payment.fee - payment.tax)
                - sum(refund.amount)
                - sum(chargeback.amount + chargeback.fee)
                + sum(adjustment.amount)

        One bank credit corresponds to this sum, not to any individual payment.
        Matching a bank credit to a payment amount is the single most common way
        naive reconcilers report a false break.
        """
        lines = self.lines_for_settlement(settlement_id)
        total = sum(p.net_paise for p in lines["payments"])
        total -= sum(r.amount_paise for r in lines["refunds"])
        total -= sum(c.amount_paise + c.fee_paise for c in lines["chargebacks"])
        total += sum(a.amount_paise for a in lines["adjustments"])
        return total

    def credits(self) -> list[BankTxn]:
        return [t for t in self.bank_txns.values() if t.is_credit]

    def debits(self) -> list[BankTxn]:
        return [t for t in self.bank_txns.values() if not t.is_credit]

    def record_count(self) -> int:
        """Total records under reconciliation. Reported as batch size."""
        return (
            len(self.orders)
            + len(self.payments)
            + len(self.refunds)
            + len(self.chargebacks)
            + len(self.adjustments)
            + len(self.settlements)
            + len(self.bank_txns)
        )

    def summary(self) -> dict[str, int]:
        return {
            "orders": len(self.orders),
            "payments": len(self.payments),
            "refunds": len(self.refunds),
            "chargebacks": len(self.chargebacks),
            "adjustments": len(self.adjustments),
            "settlements": len(self.settlements),
            "bank_txns": len(self.bank_txns),
            "total": self.record_count(),
        }


# ---------------------------------------------------------------------------
# Ground truth, emitted by the generator and used only for scoring
# ---------------------------------------------------------------------------


@dataclass
class GroundTruth:
    """The correct answer, known because the generator built the data forwards.

    The reconciler is never given this object. It exists so the report can state
    a measured precision instead of asserting a match rate and hoping. Any
    accepted match absent from here is a false positive, and a false positive in
    reconciliation is worse than a miss: a wrong match silently closes a break
    that a human would otherwise have caught.
    """

    settlement_to_bank: dict[str, list[str]] = field(default_factory=dict)
    payment_to_order: dict[str, str] = field(default_factory=dict)
    # Settlements genuinely absent from the statement, e.g. still in transit at
    # the statement cut-off. Correct behaviour is to raise these as exceptions,
    # not to match them.
    unbanked_settlements: list[str] = field(default_factory=list)
    # Payments with no corresponding order in the merchant ledger.
    orphan_payments: list[str] = field(default_factory=list)
    # Orders that were never paid. Correct behaviour is to leave them alone.
    abandoned_orders: list[str] = field(default_factory=list)
    # Bank credits that are the bank re-posting an entry it already posted.
    duplicate_bank_credits: list[str] = field(default_factory=list)
    # Bank credits that sweep several settlements into one line.
    swept_bank_credits: dict[str, list[str]] = field(default_factory=dict)
    # Records that *should* land in the exception ledger, mapped to every reason
    # code that is correct for them. This is what makes the exception list
    # scoreable rather than decorative: a pipeline that raises the right
    # exception for the right record is doing its job, and one that quietly
    # matches these records is producing a false positive.
    #
    # The value is a list because a single record can carry more than one
    # independent finding. A settlement can be misreported by the gateway *and*
    # still be in transit at the statement cut-off, and those are two separate
    # things a human needs told. Collapsing them to one would force the scorer to
    # pick a winner and would penalise a pipeline for reporting both.
    #
    # A subject can also legitimately appear here *and* be matched. A misreported
    # net is the standard case: the money moved correctly and reconciles against
    # the bank, while the gateway's reported figure is wrong.
    expected_exception_subjects: dict[str, list[str]] = field(default_factory=dict)
    # Per-scenario tally, so the report can say which injected condition the
    # pipeline actually handled rather than only reporting an aggregate.
    injected: dict[str, int] = field(default_factory=dict)

    def expected_bank_for(self, settlement_id: str) -> list[str]:
        return self.settlement_to_bank.get(settlement_id, [])

    def expect(self, subject_id: str, reason: ReasonCode | str) -> None:
        """Declare that ``subject_id`` should be raised with ``reason``."""
        code = reason.value if isinstance(reason, ReasonCode) else str(reason)
        reasons = self.expected_exception_subjects.setdefault(subject_id, [])
        if code not in reasons:
            reasons.append(code)

    def expected_exception_pairs(self) -> set[tuple[str, str]]:
        return {
            (subject_id, reason)
            for subject_id, reasons in self.expected_exception_subjects.items()
            for reason in reasons
        }


# ---------------------------------------------------------------------------
# Reconciliation outputs
# ---------------------------------------------------------------------------


class Tier(str, Enum):
    """How a match was arrived at.

    The three-number report this system exists to produce is a count of matches
    in each of these tiers. Collapsing them into one "match rate" is exactly the
    move that makes a reconciliation demo unfalsifiable.
    """

    AUTO = "auto_deterministic"
    ASSISTED = "llm_assisted_verified"
    UNRESOLVED = "unresolved"


class MatchKind(str, Enum):
    SETTLEMENT_BANK = "settlement_bank"
    PAYMENT_ORDER = "payment_order"


@dataclass
class Match:
    """One accepted correspondence between records.

    ``right_ids`` is a tuple because the relationship is genuinely one-to-many in
    both directions: a sweep credit covers several settlements, and a split
    settlement lands as several credits.
    """

    kind: MatchKind
    left_id: str
    right_ids: tuple[str, ...]
    tier: Tier
    pass_name: str
    delta_paise: int
    confidence: float
    evidence: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.kind.value, self.left_id, self.right_ids)


class ReasonCode(str, Enum):
    """Why a record could not be matched.

    Every unresolved record carries one of these. A generic "unmatched" bucket
    hides whether the pipeline failed or the data is genuinely broken, and those
    two cases need opposite responses from a human.
    """

    NO_BANK_CREDIT = "NO_BANK_CREDIT"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    NET_IDENTITY_BREAK = "NET_IDENTITY_BREAK"
    DUPLICATE_BANK_CREDIT = "DUPLICATE_BANK_CREDIT"
    UNATTRIBUTED_BANK_CREDIT = "UNATTRIBUTED_BANK_CREDIT"
    ORPHAN_PAYMENT = "ORPHAN_PAYMENT"
    ORDER_AMOUNT_DRIFT = "ORDER_AMOUNT_DRIFT"
    SUBSET_SUM_BUDGET_EXCEEDED = "SUBSET_SUM_BUDGET_EXCEEDED"
    VERIFICATION_REJECTED = "VERIFICATION_REJECTED"
    CLASSIFIER_ABSTAINED = "CLASSIFIER_ABSTAINED"


@dataclass
class ReconException:
    """An honest unresolved item, destined for the exception ledger.

    ``needs_human`` distinguishes a break someone must act on from an
    informational finding. ``suggested_action`` is what the residue classifier
    proposes; it is advisory text only and never moves money.
    """

    kind: MatchKind
    subject_id: str
    reason_code: ReasonCode
    reason: str
    amount_paise: int
    suggested_action: str = ""
    needs_human: bool = True
    evidence: dict = field(default_factory=dict)


@dataclass
class Tolerance:
    """Declared tolerances, in paise and days.

    These are deliberately explicit and small. A tolerance wide enough to make
    the match rate look good is a tolerance wide enough to match unrelated
    records, so the report prints these values alongside the results.
    """

    amount_paise: int = 100  # one rupee, absorbs paise rounding only
    settlement_lag_days: int = 3  # T+2 plus a day of slack for holidays
    max_sweep_size: int = 4
    subset_sum_node_budget: int = 20_000

    # Whether a payment with no order reference may be auto-matched to an order the
    # merchant has marked abandoned or cancelled.
    #
    # Default off, on a cost argument rather than a correctness one. Both readings
    # of such a pair are possible: the merchant's status may be stale, or the two
    # records may simply share an amount by coincidence. The consequences are not
    # symmetric. A wrong match closes a break permanently and nobody looks again,
    # while declining costs one exception that a human clears in under a minute.
    #
    # A measured sweep with this enabled produced exactly one false positive in
    # 46,191 records, an orphan payment attached to an abandoned order two days
    # older with an identical amount. Turning it off removed that false positive and
    # cost no correct matches. The flag stays configurable so the trade-off can be
    # re-measured rather than assumed.
    match_payments_to_inactive_orders: bool = False

    def within_amount(self, delta_paise: int) -> bool:
        return abs(delta_paise) <= self.amount_paise

    def within_lag(self, settled_at: datetime, value_date: date) -> bool:
        """Bank value date must land in ``[settled_date - 1, settled_date + lag]``.

        The lower bound is not zero: a bank occasionally value-dates a credit one
        day before the gateway's settlement timestamp when the cycle straddles
        midnight.
        """
        settled_date = settled_at.astimezone(IST).date()
        return (
            settled_date - timedelta(days=1)
            <= value_date
            <= settled_date + timedelta(days=self.settlement_lag_days)
        )


def total_paise(items: Iterable[int]) -> int:
    return sum(items)
