"""Synthetic ledger generator with known ground truth.

The data is built *forwards*: orders become payments, payments are batched into
settlements, settlement nets are computed from the netting identity, and bank
credits are emitted from those nets. Because every causal step is recorded as it
happens, the generator knows the correct answer, and the reconciler can be
scored against it rather than judged on a screenshot.

That direction matters. A generator that emits three files and then guesses at
the correspondences between them cannot produce trustworthy ground truth, which
means the pipeline built on it cannot report a real precision. Building forwards
is what turns "our match rate is 94 percent" into a falsifiable claim.

Nothing here touches a network, a real API, or real data. Every identifier,
amount, customer and narration is fabricated locally from a seed. The same seed
produces byte-identical output, so any number in the report can be reproduced.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Iterator

from .models import (
    IST,
    AdjustmentLine,
    BankTxn,
    ChargebackLine,
    GroundTruth,
    Ledger,
    Order,
    OrderStatus,
    PaymentLine,
    ReasonCode,
    RefundLine,
    Settlement,
    SettlementKind,
)
from .money import (
    DISPUTE_FEE_PAISE,
    INSTANT_SETTLEMENT_BPS,
    bps,
    fee_and_tax,
)
from .narration import extract_utr

# Instrument mix, roughly reflecting Indian online volume: UPI dominant, cards a
# distant second. The mix is load-bearing rather than cosmetic, because UPI
# carries zero MDR and therefore produces zero-fee settlement lines.
_METHOD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("upi", 0.62),
    ("card", 0.22),
    ("netbanking", 0.09),
    ("wallet", 0.05),
    ("emi", 0.02),
)

# Narration templates. Several rails, several banks, and two templates that carry
# no reference at all, because plenty of real statements do not.
_NARRATION_WITH_REF: tuple[str, ...] = (
    "NEFT CR-RATN0TREASURY-RAZORPAY SOFTWARE PVT LTD-UTR {utr}-SETTLEMENT",
    "IMPS/{utr}/RAZORPAY/SETTLEMENT PAYOUT",
    "RTGS CR {utr} RAZORPAY SOFTWARE PRIVATE LIMITED",
    "UPI/{utr}/SETTLEMENT FROM RAZORPAY",
    "NEFT CR {utr} RAZORPAY SETTLEMENT REF{sid}",
)
_NARRATION_WITHOUT_REF: tuple[str, ...] = (
    "NEFT CR-HDFC0000001-RAZORPAY SOFTWARE-SETTLEMENT",
    "TRANSFER FROM 50200012345678 RAZORPAY SETTLEMENT",
    "FT - RAZORPAY SOFTWARE PVT LTD - PAYOUT",
)

# Narrations that name the settlement batch in prose rather than as a reference
# token. A regex tuned for UTR shapes will not find anything here, and it should
# not: "BATCH SETL00023" is not a reference, it is a sentence fragment that
# happens to identify one. Reading it is a language problem.
_NARRATION_TEXTUAL_BATCH: tuple[str, ...] = (
    "NEFT CR-RATN0TREASURY-RAZORPAY SOFTWARE-BATCH {sid_compact} PAYOUT",
    "FT RAZORPAY SOFTWARE PVT LTD SETTLEMENT BATCH NO {sid_compact}",
    "TRANSFER RAZORPAY - PAYOUT AGAINST BATCH {sid_compact} - NET OF FEES",
)

# Credits that are not settlements. A statement is not a settlement report, and a
# reconciler that assumes every credit must belong to the gateway will invent
# matches for these.
_FOREIGN_CREDIT_NARRATIONS: tuple[str, ...] = (
    "NEFT CR-ICIC0000123-ACME DISTRIBUTORS-INV 4471",
    "INTEREST CREDIT SB ACCOUNT",
    "IMPS/318842910273/VENDOR REFUND/GST INPUT",
    "NEFT CR-SBIN0011513-KAPOOR TRADING CO-ADVANCE",
    "CASH DEPOSIT BRANCH COUNTER",
)

_DEBIT_NARRATIONS: tuple[str, ...] = (
    "BANK CHARGES - NEFT OUTWARD",
    "GST ON BANK CHARGES",
    "ACCOUNT MAINTENANCE FEE",
)


@dataclass
class GeneratorConfig:
    """Every knob, with the defaults used for the reported run.

    The rates are set so that the difficult cases are common enough to be
    measured, not so rare that a single lucky run looks clean. Roughly one
    settlement in four carries at least one injected complication.
    """

    seed: int = 20_260_829
    days: int = 45
    start_date: date = date(2026, 7, 1)
    orders_per_day_min: int = 7
    orders_per_day_max: int = 16

    # Customer behaviour
    abandon_rate: float = 0.14
    cancel_rate: float = 0.03
    refund_rate: float = 0.09
    partial_refund_share: float = 0.5
    chargeback_rate: float = 0.02
    late_refund_share: float = 0.25

    # Settlement behaviour. Instant settlements are kept relatively rare so that
    # regular batches retain enough depth for netting and subset-sum matching to
    # be exercised properly. A dataset of mostly single-payment settlements would
    # make the hard passes unreachable.
    instant_settlement_rate: float = 0.05
    settlement_lag_days: int = 2

    # Ingestion and reporting defects. These rates are set relative to the number
    # of settlements, not payments, and are tuned so every scenario appears often
    # enough that the per-scenario breakdown in the report is meaningful rather
    # than a sample of one.
    narration_no_ref_rate: float = 0.16
    narration_garbled_ref_rate: float = 0.10
    rounding_drift_rate: float = 0.12
    net_misreport_rate: float = 0.08
    duplicate_credit_rate: float = 0.06
    sweep_rate: float = 0.08
    # A sweep group's settled dates may span at most this many days. Kept at or
    # below the reconciler's lag tolerance so that a credit dated at the last
    # cycle remains inside the window for the first.
    max_sweep_span_days: int = 2
    unbanked_rate: float = 0.06
    value_date_slip_rate: float = 0.22

    # Cross-source defects
    orphan_payment_count: int = 8
    order_amount_drift_rate: float = 0.04
    foreign_credit_count: int = 9
    bank_debit_count: int = 7

    # Pairs of settlements forced to identical nets, whose credits carry the batch
    # reference in a format no reference regex recognises. These are deliberately
    # unreachable by the deterministic ladder: two candidates match the amount
    # exactly, so declining is the only correct deterministic outcome. The
    # narration text distinguishes them, which is work a language model does well
    # and arithmetic cannot do at all. They exist to make the assisted tier
    # measurable rather than decorative.
    ambiguous_pair_count: int = 3

    # Bank holidays inside the window. Settlements do not land on these days or
    # on Sundays, which is what produces clustered Monday credits and the
    # occasional three-day lag.
    holidays: frozenset[date] = field(
        default_factory=lambda: frozenset(
            {
                date(2026, 7, 6),
                date(2026, 7, 17),
                date(2026, 7, 29),
            }
        )
    )


class _Generator:
    """Single-use builder. One instance produces one dataset."""

    def __init__(self, cfg: GeneratorConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.ledger = Ledger()
        self.truth = GroundTruth()
        self._counters: dict[str, Iterator[int]] = {}
        # Line items grouped by settlement, maintained during generation so the
        # net can be computed without repeatedly scanning the whole ledger.
        self._by_settlement: dict[str, dict[str, list]] = {}
        # Settlements whose credit names the batch in prose instead of exposing a
        # reference token. Populated by the ambiguous-pair stage.
        self._textual_reference: set[str] = set()

    # -- identifiers -------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter):05d}"

    def _bump(self, scenario: str, count: int = 1) -> None:
        self.truth.injected[scenario] = self.truth.injected.get(scenario, 0) + count

    # -- primitives --------------------------------------------------------

    def _draw_method(self) -> str:
        roll = self.rng.random()
        cumulative = 0.0
        for method, weight in _METHOD_WEIGHTS:
            cumulative += weight
            if roll < cumulative:
                return method
        return _METHOD_WEIGHTS[-1][0]

    def _draw_amount_paise(self) -> int:
        """A long-tailed ticket-size distribution with occasional paise.

        The paise matter. Whole-rupee amounts everywhere would make every
        rounding path in the pipeline dead code, and a rounding bug that only
        appears on non-round amounts is exactly the bug reconciliation exists to
        catch.
        """
        roll = self.rng.random()
        if roll < 0.70:
            amount = self.rng.randrange(19_900, 299_900, 100)
        elif roll < 0.93:
            amount = self.rng.randrange(300_000, 1_500_000, 100)
        else:
            amount = self.rng.randrange(1_500_000, 9_000_000, 100)
        if self.rng.random() < 0.15:
            amount += self.rng.randrange(1, 100)
        return amount

    def _settlement_date_for(self, captured: datetime, instant: bool) -> date:
        captured_date = captured.astimezone(IST).date()
        if instant:
            return captured_date
        target = captured_date + timedelta(days=self.cfg.settlement_lag_days)
        return self._next_settlement_day(target)

    def _next_settlement_day(self, day: date) -> date:
        """Roll forward past Sundays and listed bank holidays."""
        guard = 0
        while (day.weekday() == 6 or day in self.cfg.holidays) and guard < 10:
            day += timedelta(days=1)
            guard += 1
        return day

    # -- stage 1: orders and payments --------------------------------------

    def _build_orders_and_payments(self) -> None:
        for offset in range(self.cfg.days):
            day = self.cfg.start_date + timedelta(days=offset)
            count = self.rng.randint(self.cfg.orders_per_day_min, self.cfg.orders_per_day_max)
            for _ in range(count):
                self._build_one_order(day)

    def _build_one_order(self, day: date) -> None:
        order_id = self._next_id("order")
        amount = self._draw_amount_paise()
        created = datetime(
            day.year,
            day.month,
            day.day,
            self.rng.randint(7, 22),
            self.rng.randint(0, 59),
            tzinfo=IST,
        )

        roll = self.rng.random()
        if roll < self.cfg.abandon_rate:
            status = OrderStatus.ABANDONED
        elif roll < self.cfg.abandon_rate + self.cfg.cancel_rate:
            status = OrderStatus.CANCELLED
        else:
            status = OrderStatus.PAID

        self.ledger.orders[order_id] = Order(
            order_id=order_id,
            merchant_ref=f"SO-{order_id.split('_')[1]}",
            customer_id=f"cust_{self.rng.randrange(1, 240):04d}",
            amount_paise=amount,
            currency="INR",
            created_at=created,
            status=status,
        )

        if status is not OrderStatus.PAID:
            # An unpaid order is not a break. The correct behaviour is to leave it
            # alone, so it is recorded as such and scored that way.
            self.truth.abandoned_orders.append(order_id)
            return

        gross = amount
        drifted = self.rng.random() < self.cfg.order_amount_drift_rate
        if drifted:
            # A discount applied after the order was written, or a partial
            # capture. The pairing is still correct, but the amounts disagree by
            # more than any sane tolerance, so a human has to look.
            discount = self.rng.randrange(5_000, min(50_000, max(6_000, amount // 3)), 100)
            gross = amount - discount

        method = self._draw_method()
        fee, tax = fee_and_tax(gross, method)
        payment_id = self._next_id("pay")
        captured = created + timedelta(minutes=self.rng.randint(1, 180))

        self.ledger.payments[payment_id] = PaymentLine(
            payment_id=payment_id,
            order_id=order_id,
            gross_paise=gross,
            fee_paise=fee,
            tax_paise=tax,
            captured_at=captured,
            method=method,
        )
        self.truth.payment_to_order[payment_id] = order_id

        if drifted:
            self._bump("order_amount_drift")
            self.truth.expect(payment_id, ReasonCode.ORDER_AMOUNT_DRIFT)

    def _build_orphan_payments(self) -> None:
        """Payments with no order behind them.

        In production these come from legacy checkouts, direct API calls, or an
        order record that never made it into the merchant's own database. They
        are genuinely unmatchable on the order side and must surface as
        exceptions rather than be forced onto some nearby order.
        """
        for _ in range(self.cfg.orphan_payment_count):
            gross = self._draw_amount_paise()
            method = self._draw_method()
            fee, tax = fee_and_tax(gross, method)
            payment_id = self._next_id("pay")
            day = self.cfg.start_date + timedelta(days=self.rng.randrange(self.cfg.days))
            captured = datetime(
                day.year, day.month, day.day, self.rng.randint(8, 20), 0, tzinfo=IST
            )
            self.ledger.payments[payment_id] = PaymentLine(
                payment_id=payment_id,
                order_id=None,
                gross_paise=gross,
                fee_paise=fee,
                tax_paise=tax,
                captured_at=captured,
                method=method,
            )
            self.truth.orphan_payments.append(payment_id)
            self.truth.expect(payment_id, ReasonCode.ORPHAN_PAYMENT)
            self._bump("orphan_payment")

    # -- stage 2: refunds and disputes -------------------------------------

    def _build_refunds_and_chargebacks(self) -> None:
        paid = [p for p in self.ledger.payments.values() if p.order_id is not None]
        self.rng.shuffle(paid)

        refund_count = int(len(paid) * self.cfg.refund_rate)
        for payment in paid[:refund_count]:
            partial = self.rng.random() < self.cfg.partial_refund_share
            if partial:
                # Refund a slice of the gross, on a rupee boundary, never the
                # whole amount.
                slice_paise = self.rng.randrange(
                    max(10_000, payment.gross_paise // 5),
                    max(20_000, payment.gross_paise // 2),
                    100,
                )
                amount = min(slice_paise, payment.gross_paise - 100)
            else:
                amount = payment.gross_paise
            refund_id = self._next_id("rfnd")
            self.ledger.refunds[refund_id] = RefundLine(
                refund_id=refund_id,
                payment_id=payment.payment_id,
                amount_paise=amount,
                created_at=payment.captured_at + timedelta(days=self.rng.randint(1, 15)),
                is_partial=partial,
            )
            self._bump("partial_refund" if partial else "full_refund")

        # Disputes skew hard to cards. A UPI chargeback is rare enough that
        # generating them uniformly would misrepresent the problem.
        card_like = [p for p in paid if p.method in ("card", "emi")]
        pool = card_like if card_like else paid
        chargeback_count = max(1, int(len(paid) * self.cfg.chargeback_rate))
        for payment in pool[:chargeback_count]:
            dispute_id = self._next_id("disp")
            self.ledger.chargebacks[dispute_id] = ChargebackLine(
                dispute_id=dispute_id,
                payment_id=payment.payment_id,
                amount_paise=payment.gross_paise,
                fee_paise=DISPUTE_FEE_PAISE,
                created_at=payment.captured_at + timedelta(days=self.rng.randint(5, 40)),
            )
            self._bump("chargeback")

    # -- stage 3: settlement batching --------------------------------------

    def _build_settlements(self) -> None:
        """Batch payments into settlements, then attribute refunds and disputes.

        Batching is by settlement date, which is where the one-to-many shape
        comes from: a day's payments net into a single credit. Instant
        settlements are carved out as single-payment batches on the capture date.
        """
        instant_ids: set[str] = set()
        for payment in self.ledger.payments.values():
            if self.rng.random() < self.cfg.instant_settlement_rate:
                instant_ids.add(payment.payment_id)

        buckets: dict[tuple[date, SettlementKind, str], list[PaymentLine]] = {}
        for payment in self.ledger.payments.values():
            instant = payment.payment_id in instant_ids
            settle_date = self._settlement_date_for(payment.captured_at, instant)
            kind = SettlementKind.INSTANT if instant else SettlementKind.REGULAR
            # Instant settlements are per payment, so they are keyed uniquely.
            discriminator = payment.payment_id if instant else ""
            buckets.setdefault((settle_date, kind, discriminator), []).append(payment)

        for (settle_date, kind, _), payments in sorted(buckets.items(), key=lambda kv: kv[0][0]):
            settlement_id = self._next_id("setl")
            settled_at = datetime(
                settle_date.year, settle_date.month, settle_date.day, 11, 0, tzinfo=IST
            )
            for payment in payments:
                self.ledger.payments[payment.payment_id] = replace(
                    payment, settlement_id=settlement_id
                )
            self._by_settlement[settlement_id] = {
                "payments": [self.ledger.payments[p.payment_id] for p in payments],
                "refunds": [],
                "chargebacks": [],
                "adjustments": [],
            }
            # Reported net is filled in later, once the identity is computed.
            self.ledger.settlements[settlement_id] = Settlement(
                settlement_id=settlement_id,
                utr=self._make_utr(),
                net_paise=0,
                settled_at=settled_at,
                kind=kind,
            )
            if kind is SettlementKind.INSTANT:
                self._bump("instant_settlement")

        self._attribute_deductions()

    def _make_utr(self) -> str:
        return f"RZP{self.rng.randrange(10 ** 12, 10 ** 13)}"

    def _ordered_settlements(self) -> list[Settlement]:
        return sorted(
            self.ledger.settlements.values(), key=lambda s: (s.settled_at, s.settlement_id)
        )

    def _attribute_deductions(self) -> None:
        """Attach each refund and dispute to the settlement that absorbs it.

        A deduction lands in the first settlement on or after the day it was
        raised, except when it slips a cycle. That slip is common in practice and
        it is the reason a settlement net cannot be derived from the payments of
        one capture date alone.
        """
        ordered = self._ordered_settlements()

        def target_for(raised: datetime, slip: bool) -> str | None:
            raised_date = raised.astimezone(IST).date()
            eligible = [s for s in ordered if s.settled_at.astimezone(IST).date() >= raised_date]
            if not eligible:
                return None
            if slip and len(eligible) > 1:
                return eligible[1].settlement_id
            return eligible[0].settlement_id

        for refund_id, refund in list(self.ledger.refunds.items()):
            slip = self.rng.random() < self.cfg.late_refund_share
            settlement_id = target_for(refund.created_at, slip)
            if settlement_id is None:
                # Raised after the last settlement in the window. It will be
                # absorbed by a cycle outside the dataset, so it carries no
                # settlement and is excluded from every net.
                continue
            updated = replace(refund, settlement_id=settlement_id)
            self.ledger.refunds[refund_id] = updated
            self._by_settlement[settlement_id]["refunds"].append(updated)
            if slip:
                self._bump("late_refund_cycle_slip")

        for dispute_id, chargeback in list(self.ledger.chargebacks.items()):
            settlement_id = target_for(chargeback.created_at, False)
            if settlement_id is None:
                continue
            updated = replace(chargeback, settlement_id=settlement_id)
            self.ledger.chargebacks[dispute_id] = updated
            self._by_settlement[settlement_id]["chargebacks"].append(updated)

    # -- stage 4: nets, carry-forward, reported figures --------------------

    def _compute_nets(self) -> None:
        """Compute each settlement net and handle negative balances.

        When deductions exceed collections the gateway does not claw money back
        from the bank account; it carries the deficit into the next cycle as an
        adjustment. Modelling that is what produces settlements which correctly
        have no bank credit at all, and those are the records a naive pipeline
        reports as missing money.
        """
        ordered = self._ordered_settlements()
        for index, settlement in enumerate(ordered):
            group = self._by_settlement[settlement.settlement_id]
            net = sum(p.net_paise for p in group["payments"])
            net -= sum(r.amount_paise for r in group["refunds"])
            net -= sum(c.amount_paise + c.fee_paise for c in group["chargebacks"])
            net += sum(a.amount_paise for a in group["adjustments"])

            if settlement.kind is SettlementKind.INSTANT and net > 0:
                charge = bps(net, INSTANT_SETTLEMENT_BPS)
                if charge:
                    adjustment_id = self._next_id("adj")
                    adjustment = AdjustmentLine(
                        adjustment_id=adjustment_id,
                        amount_paise=-charge,
                        description="Instant settlement charge",
                        created_at=settlement.settled_at,
                        settlement_id=settlement.settlement_id,
                    )
                    self.ledger.adjustments[adjustment_id] = adjustment
                    group["adjustments"].append(adjustment)
                    net -= charge

            if net <= 0:
                # Push the deficit forward and pay out nothing this cycle. The
                # reported net stays equal to the computed net, negative and all,
                # so this record does not also look like a reporting break. It is
                # exactly one finding: no bank credit, correctly.
                if index + 1 < len(ordered):
                    nxt = ordered[index + 1]
                    adjustment_id = self._next_id("adj")
                    adjustment = AdjustmentLine(
                        adjustment_id=adjustment_id,
                        amount_paise=net,
                        description=f"Carried forward from {settlement.settlement_id}",
                        created_at=nxt.settled_at,
                        settlement_id=nxt.settlement_id,
                    )
                    self.ledger.adjustments[adjustment_id] = adjustment
                    self._by_settlement[nxt.settlement_id]["adjustments"].append(adjustment)
                    self._bump("negative_net_carried_forward")
                self.ledger.settlements[settlement.settlement_id] = replace(
                    settlement, net_paise=net
                )
                self.truth.unbanked_settlements.append(settlement.settlement_id)
                self.truth.expect(settlement.settlement_id, ReasonCode.NO_BANK_CREDIT)
                continue

            reported = net
            if self.rng.random() < self.cfg.net_misreport_rate:
                # The gateway's reported figure disagrees with its own line
                # items. Money follows the line items, so the bank credit is
                # correct and the *report* is wrong. This is the case that
                # rewards recomputing the identity instead of trusting the
                # header.
                skew = self.rng.choice([-1, 1]) * self.rng.randrange(1_000, 25_000)
                reported = net + skew
                self._bump("settlement_net_misreported")
                self.truth.expect(settlement.settlement_id, ReasonCode.NET_IDENTITY_BREAK)

            self.ledger.settlements[settlement.settlement_id] = replace(
                settlement, net_paise=reported
            )

    def _true_net(self, settlement_id: str) -> int:
        group = self._by_settlement[settlement_id]
        net = sum(p.net_paise for p in group["payments"])
        net -= sum(r.amount_paise for r in group["refunds"])
        net -= sum(c.amount_paise + c.fee_paise for c in group["chargebacks"])
        net += sum(a.amount_paise for a in group["adjustments"])
        return net

    # -- stage 4b: engineered amount ambiguity -----------------------------

    def _engineer_ambiguous_pairs(self) -> None:
        """Force pairs of nearby settlements to identical nets.

        A reserve release or a fee correction lands as an adjustment and can leave
        two cycles netting to exactly the same figure. When that happens and the
        credit narration carries no reference token, the amount is no longer
        discriminating: two settlements explain the credit equally well and the
        deterministic passes must decline both.

        This is the case that separates a reconciler from a matcher. The
        arithmetic is symmetric and cannot be broken by more arithmetic. What
        breaks it is reading "BATCH SETL00023" in the narration, which is a
        language task, and the answer is then checked by the same arithmetic gate
        as everything else. So the model contributes something real and still
        cannot introduce an unbalanced match.
        """
        unbanked = set(self.truth.unbanked_settlements)
        # Settlements already carrying a reporting break are excluded. This stage
        # realigns a header to force the amounts equal, which would silently repair
        # a misreport injected earlier and leave the ground truth claiming a break
        # that no longer exists. Keeping the two scenarios disjoint means each one
        # is measured on its own.
        misreported = {
            settlement_id
            for settlement_id, reasons in self.truth.expected_exception_subjects.items()
            if ReasonCode.NET_IDENTITY_BREAK.value in reasons
        }
        pool = [
            settlement
            for settlement in self._ordered_settlements()
            if settlement.settlement_id not in unbanked
            and settlement.settlement_id not in misreported
            and self._true_net(settlement.settlement_id) > 0
        ]
        used: set[str] = set()
        made = 0

        for i, first in enumerate(pool):
            if made >= self.cfg.ambiguous_pair_count:
                break
            if first.settlement_id in used:
                continue
            for second in pool[i + 1 :]:
                if second.settlement_id in used:
                    continue
                gap = (
                    second.settled_at.astimezone(IST).date()
                    - first.settled_at.astimezone(IST).date()
                ).days
                if gap > 1:
                    break
                net_first = self._true_net(first.settlement_id)
                net_second = self._true_net(second.settlement_id)
                delta = net_first - net_second
                if delta == 0 or net_second + delta <= 0:
                    continue

                adjustment_id = self._next_id("adj")
                adjustment = AdjustmentLine(
                    adjustment_id=adjustment_id,
                    amount_paise=delta,
                    description=f"Reserve release on {second.settlement_id}",
                    created_at=second.settled_at,
                    settlement_id=second.settlement_id,
                )
                self.ledger.adjustments[adjustment_id] = adjustment
                self._by_settlement[second.settlement_id]["adjustments"].append(adjustment)
                # The header follows the line items, so this pair stays free of any
                # reporting break. The only difficulty here is identification.
                self.ledger.settlements[second.settlement_id] = replace(
                    self.ledger.settlements[second.settlement_id],
                    net_paise=self._true_net(second.settlement_id),
                )

                self._textual_reference.update({first.settlement_id, second.settlement_id})
                used.update({first.settlement_id, second.settlement_id})
                made += 1
                self._bump("ambiguous_equal_net_pair")
                break

    # -- stage 5: the bank statement ---------------------------------------

    def _build_bank_statement(self) -> None:
        payable = [
            s
            for s in self._ordered_settlements()
            if s.settlement_id not in set(self.truth.unbanked_settlements)
        ]

        swept: set[str] = set()
        sweep_groups: list[list[Settlement]] = []
        index = 0
        while index < len(payable) - 1:
            if self.rng.random() < self.cfg.sweep_rate:
                size = self.rng.randint(2, 3)
                group = payable[index : index + size]
                # A sweep collapses *adjacent* cycles. The group must be tight
                # enough in time that a single credit dated at the last cycle is
                # still a plausible payout for the first, otherwise this is not a
                # sweep, it is two unrelated credits that happen to add up.
                span_days = (
                    group[-1].settled_at.astimezone(IST).date()
                    - group[0].settled_at.astimezone(IST).date()
                ).days
                # An engineered ambiguous pair must keep its own credit. Letting a
                # sweep absorb one would destroy the ambiguity the pair exists to
                # create.
                collides = any(
                    s.settlement_id in self._textual_reference for s in group
                )
                if (
                    len(group) >= 2
                    and span_days <= self.cfg.max_sweep_span_days
                    and not collides
                ):
                    sweep_groups.append(group)
                    swept.update(s.settlement_id for s in group)
                    index += len(group)
                    continue
            index += 1

        for settlement in payable:
            if settlement.settlement_id in swept:
                continue
            if self.rng.random() < self.cfg.unbanked_rate:
                # Still in transit at the statement cut-off. Correct behaviour is
                # to raise it, not to match it.
                self.truth.unbanked_settlements.append(settlement.settlement_id)
                self.truth.expect(settlement.settlement_id, ReasonCode.NO_BANK_CREDIT)
                self._bump("settlement_in_transit")
                continue
            self._emit_credit_for([settlement])

        for group in sweep_groups:
            self._emit_credit_for(group)
            self._bump("sweep_credit")

        self._build_foreign_credits()
        self._build_bank_debits()

    def _emit_credit_for(self, settlements: list[Settlement]) -> None:
        """Emit one bank credit covering one or more settlements.

        The credit is value-dated from the *latest* settlement it covers, because
        money cannot arrive before the last cycle it pays out. For a single
        settlement that is simply its own date; for a sweep it is what keeps the
        credit causally possible for every member of the group.
        """
        amount = sum(self._true_net(s.settlement_id) for s in settlements)
        # The narration echoes the first cycle's reference, which is what makes a
        # sweep interesting: the reference resolves to one real settlement while
        # the amount belongs to several. A reference-only matcher accepts it and is
        # wrong.
        anchor = settlements[0]
        latest = max(settlements, key=lambda s: s.settled_at)

        drift = 0
        if self.rng.random() < self.cfg.rounding_drift_rate:
            # Sub-rupee drift from the bank's own rounding on the transfer.
            drift = self.rng.choice([-1, 1]) * self.rng.randrange(1, 60)
            self._bump("rounding_drift")

        value_date = latest.settled_at.astimezone(IST).date()
        # A sweep already consumes most of the lag budget through its span, so it
        # does not additionally slip. Slipping both would push the earliest member
        # of the group outside any defensible window.
        if len(settlements) == 1 and self.rng.random() < self.cfg.value_date_slip_rate:
            value_date = value_date + timedelta(days=self.rng.randint(1, 2))
            self._bump("value_date_slip")

        narration, exposed_ref = self._make_narration(anchor)
        bank_txn_id = self._next_id("bank")
        txn = BankTxn(
            bank_txn_id=bank_txn_id,
            value_date=value_date,
            amount_paise=amount + drift,
            narration=narration,
            utr_hint=extract_utr(narration),
            balance_paise=None,
        )
        self.ledger.bank_txns[bank_txn_id] = txn

        for settlement in settlements:
            self.truth.settlement_to_bank.setdefault(settlement.settlement_id, []).append(
                bank_txn_id
            )
        if len(settlements) > 1:
            self.truth.swept_bank_credits[bank_txn_id] = [
                s.settlement_id for s in settlements
            ]

        if exposed_ref is None:
            self._bump("narration_without_reference")

        if self.rng.random() < self.cfg.duplicate_credit_rate:
            # The bank re-posts an entry it already posted. Matching both copies
            # would double-count revenue, so exactly one must match and the other
            # must be raised.
            dup_id = self._next_id("bank")
            self.ledger.bank_txns[dup_id] = replace(txn, bank_txn_id=dup_id)
            self.truth.duplicate_bank_credits.append(dup_id)
            self.truth.expect(dup_id, ReasonCode.DUPLICATE_BANK_CREDIT)
            self._bump("duplicate_bank_credit")

    def _make_narration(self, settlement: Settlement) -> tuple[str, str | None]:
        """Build a narration and report which reference, if any, it exposes."""
        if settlement.settlement_id in self._textual_reference:
            # Names the batch in prose. No reference token, so the deterministic
            # passes get nothing from it and the amount is ambiguous by design.
            template = self.rng.choice(_NARRATION_TEXTUAL_BATCH)
            compact = settlement.settlement_id.replace("_", "").upper()
            return template.format(sid_compact=compact), None

        roll = self.rng.random()
        if roll < self.cfg.narration_no_ref_rate:
            return self.rng.choice(_NARRATION_WITHOUT_REF), None

        reference = settlement.utr
        if roll < self.cfg.narration_no_ref_rate + self.cfg.narration_garbled_ref_rate:
            # Truncated or transposed by the remitting bank's field limits. The
            # parser will extract something reference-shaped that matches no
            # settlement, which is strictly worse than extracting nothing and is
            # why the pipeline must fall through on a reference miss rather than
            # give up.
            digits = list(reference[3:])
            if len(digits) > 4:
                i = self.rng.randrange(len(digits) - 1)
                digits[i], digits[i + 1] = digits[i + 1], digits[i]
            reference = "RZP" + "".join(digits[:-1])
            self._bump("garbled_reference")

        template = self.rng.choice(_NARRATION_WITH_REF)
        narration = template.format(utr=reference, sid=settlement.settlement_id.upper())
        return narration, reference

    def _build_foreign_credits(self) -> None:
        for _ in range(self.cfg.foreign_credit_count):
            day = self.cfg.start_date + timedelta(days=self.rng.randrange(self.cfg.days))
            bank_txn_id = self._next_id("bank")
            self.ledger.bank_txns[bank_txn_id] = BankTxn(
                bank_txn_id=bank_txn_id,
                value_date=day,
                amount_paise=self._draw_amount_paise(),
                narration=self.rng.choice(_FOREIGN_CREDIT_NARRATIONS),
                utr_hint=None,
            )
            self.truth.expect(bank_txn_id, ReasonCode.UNATTRIBUTED_BANK_CREDIT)
            self._bump("foreign_credit")

    def _build_bank_debits(self) -> None:
        """Debits are noise the reconciler must ignore rather than explain.

        They are present because a pipeline that scans "every statement line"
        instead of "every credit" will try to attribute them to settlements, and
        that failure only shows up if the statement actually contains debits.
        """
        for _ in range(self.cfg.bank_debit_count):
            day = self.cfg.start_date + timedelta(days=self.rng.randrange(self.cfg.days))
            bank_txn_id = self._next_id("bank")
            self.ledger.bank_txns[bank_txn_id] = BankTxn(
                bank_txn_id=bank_txn_id,
                value_date=day,
                amount_paise=-self.rng.randrange(5_000, 90_000),
                narration=self.rng.choice(_DEBIT_NARRATIONS),
                utr_hint=None,
            )
            self._bump("bank_debit_noise")

    # -- entry point -------------------------------------------------------

    def build(self) -> tuple[Ledger, GroundTruth]:
        self._build_orders_and_payments()
        self._build_orphan_payments()
        self._build_refunds_and_chargebacks()
        self._build_settlements()
        self._compute_nets()
        self._engineer_ambiguous_pairs()
        self._build_bank_statement()
        self._assert_internal_consistency()
        return self.ledger, self.truth

    def _assert_internal_consistency(self) -> None:
        """Guard the generator against itself.

        Ground truth is only worth having if it is actually true. These checks
        fail loudly at generation time rather than letting a generator bug
        masquerade as a reconciler result, which would invalidate every number in
        the report.
        """
        for settlement_id, group in self._by_settlement.items():
            recomputed = self.ledger.computed_net_paise(settlement_id)
            expected = self._true_net(settlement_id)
            assert recomputed == expected, (
                f"{settlement_id}: ledger index disagrees with generator "
                f"({recomputed} vs {expected}). The settlement_id wiring is wrong."
            )

        for settlement_id, bank_ids in self.truth.settlement_to_bank.items():
            assert settlement_id in self.ledger.settlements, settlement_id
            for bank_id in bank_ids:
                assert bank_id in self.ledger.bank_txns, bank_id

        unbanked = set(self.truth.unbanked_settlements)
        banked = set(self.truth.settlement_to_bank)
        overlap = unbanked & banked
        assert not overlap, f"settlements both banked and unbanked: {sorted(overlap)}"

        # A declared reporting break must actually be present in the data, and an
        # undeclared one must not. Without this check a later generation stage can
        # repair or introduce a break and leave the ground truth describing a
        # dataset that no longer exists, which would silently corrupt every
        # exception-ledger figure in the report.
        for settlement_id, settlement in self.ledger.settlements.items():
            recomputed = self.ledger.computed_net_paise(settlement_id)
            drifted = abs(settlement.net_paise - recomputed) > 100
            declared = ReasonCode.NET_IDENTITY_BREAK.value in (
                self.truth.expected_exception_subjects.get(settlement_id) or []
            )
            assert drifted == declared, (
                f"{settlement_id}: reported {settlement.net_paise} vs recomputed "
                f"{recomputed} (drifted={drifted}) but ground truth declares "
                f"NET_IDENTITY_BREAK={declared}"
            )

        for payment_id, order_id in self.truth.payment_to_order.items():
            assert payment_id in self.ledger.payments, payment_id
            assert order_id in self.ledger.orders, order_id

        for payment_id in self.truth.orphan_payments:
            assert self.ledger.payments[payment_id].order_id is None

        # Every credit is either attributed to settlements, a known duplicate, or
        # a known foreign credit. An unclassified credit would mean the scorer
        # cannot decide whether raising it was correct.
        attributed: set[str] = set()
        for bank_ids in self.truth.settlement_to_bank.values():
            attributed.update(bank_ids)
        known = attributed | set(self.truth.duplicate_bank_credits)
        for txn in self.ledger.credits():
            if txn.bank_txn_id in known:
                continue
            assert ReasonCode.UNATTRIBUTED_BANK_CREDIT.value in (
                self.truth.expected_exception_subjects.get(txn.bank_txn_id) or []
            ), f"credit {txn.bank_txn_id} is not classified in ground truth"


def generate(config: GeneratorConfig | None = None) -> tuple[Ledger, GroundTruth]:
    """Build a synthetic three-source dataset and its ground truth.

    Deterministic for a given ``config.seed``.
    """
    return _Generator(config or GeneratorConfig()).build()
