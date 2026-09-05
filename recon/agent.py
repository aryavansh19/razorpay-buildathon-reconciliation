"""Settlement Q&A agent.

A question-answering surface over a *completed* reconciliation run. The agent has
no ability to match, match differently, mutate the ledger, or move money. It reads
verified output through a fixed set of tools and answers in prose.

Why this is safe to put a language model behind
-----------------------------------------------
The reconciler's guarantee is that every accepted match survived deterministic
re-derivation. This agent inherits that guarantee by construction, because the only
facts it can reach are ones the pipeline already verified. It cannot answer "how
much settled on the 14th" by computing something; it can only answer by calling a
tool that reads the reconciled result.

Groundedness is then checkable rather than hoped for. Every tool records which
record identifiers it surfaced. After the model answers, every identifier appearing
in its prose is checked against that set. An identifier the model produced without
a tool having returned it is a fabrication, and it is reported as one instead of
being presented as an answer.

That check is the same idea as the verification gate in ``verify.py``, applied to
language instead of arithmetic: the model proposes an explanation, and something
deterministic confirms the explanation refers to real records.

Both backends work the same way. Without ``ANTHROPIC_API_KEY`` a deterministic
intent router answers from templates, so the agent is demonstrable offline.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .classify import (
    ANTHROPIC_URL,
    ANTHROPIC_VERSION,
    DEFAULT_MODEL,
    USD_PER_MILLION_INPUT_TOKENS,
    USD_PER_MILLION_OUTPUT_TOKENS,
)
from .models import MatchKind, Tier, rupees
from .pipeline import PipelineResult

# Record identifiers produced by the generator. Used both to extract citations from
# an answer and to validate that a requested identifier is well formed before a
# tool goes looking for it.
RECORD_ID = re.compile(r"\b(?:order|pay|rfnd|disp|adj|setl|bank)_\d{4,6}\b")


# ---------------------------------------------------------------------------
# Read-only view over a finished run
# ---------------------------------------------------------------------------


class ReconciliationView:
    """Indexes a completed run for lookup. Nothing here mutates anything."""

    def __init__(self, result: PipelineResult) -> None:
        self.ledger = result.ledger
        self.report = result.report
        self.matches = result.matches
        self.exceptions = result.exceptions

        # settlement_id -> bank_txn_id, and the reverse
        self.settlement_to_credit: dict[str, str] = {}
        self.credit_to_settlements: dict[str, tuple[str, ...]] = {}
        self.payment_to_order: dict[str, str] = {}
        self.order_to_payment: dict[str, str] = {}
        self.match_by_subject: dict[str, Any] = {}

        for match in self.matches:
            if match.kind is MatchKind.SETTLEMENT_BANK:
                self.credit_to_settlements[match.left_id] = match.right_ids
                self.match_by_subject[match.left_id] = match
                for settlement_id in match.right_ids:
                    self.settlement_to_credit[settlement_id] = match.left_id
                    self.match_by_subject[settlement_id] = match
            else:
                order_id = match.right_ids[0]
                self.payment_to_order[match.left_id] = order_id
                self.order_to_payment[order_id] = match.left_id
                self.match_by_subject[match.left_id] = match
                self.match_by_subject[order_id] = match

        self.exceptions_by_subject: dict[str, list] = {}
        for exception in self.exceptions:
            self.exceptions_by_subject.setdefault(exception.subject_id, []).append(exception)

        self.payment_to_settlement: dict[str, str] = {
            payment_id: payment.settlement_id
            for payment_id, payment in self.ledger.payments.items()
            if payment.settlement_id
        }

    def record_type(self, record_id: str) -> str | None:
        if record_id in self.ledger.settlements:
            return "settlement"
        if record_id in self.ledger.bank_txns:
            return "bank_txn"
        if record_id in self.ledger.payments:
            return "payment"
        if record_id in self.ledger.orders:
            return "order"
        if record_id in self.ledger.refunds:
            return "refund"
        if record_id in self.ledger.chargebacks:
            return "chargeback"
        if record_id in self.ledger.adjustments:
            return "adjustment"
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    tool: str
    args: dict
    ok: bool
    data: Any = None
    error: str | None = None
    records_touched: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    def payload(self) -> str:
        if not self.ok:
            return json.dumps({"error": self.error}, default=str)
        return json.dumps(self.data, default=str)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[ReconciliationView, dict], tuple[Any, list[str]]]


def _tool_reconciliation_summary(view: ReconciliationView, _args: dict):
    report = view.report
    data = {
        "seed": report.seed,
        "records_in_batch": report.total_records,
        "batch_composition": report.ledger_summary,
        "auto_matched_deterministic": report.auto_matched,
        "model_assisted_verified": report.assisted_matched,
        "unresolved_raised": report.unresolved,
        "combined_match_rate_pct": round(report.combined_match_rate, 2),
        "settlement_to_bank": {
            "expected": len(report.settlement_bank.expected),
            "correct": len(report.settlement_bank.true_positives),
            "false_positives": len(report.settlement_bank.false_positives),
            "precision_pct": round(report.settlement_bank.precision, 2),
            "recall_pct": round(report.settlement_bank.recall, 2),
        },
        "payment_to_order": {
            "expected": len(report.payment_order.expected),
            "correct": len(report.payment_order.true_positives),
            "false_positives": len(report.payment_order.false_positives),
            "precision_pct": round(report.payment_order.precision, 2),
            "recall_pct": round(report.payment_order.recall, 2),
        },
        "throughput_records_per_second": round(report.records_per_second),
        "verification_gate_rejections": len(report.verifier_rejections),
        "coverage_holes": len(report.coverage_holes),
        "audit_hash_chain_intact": report.audit_chain_ok,
        "audit_replay_reproduces_state": report.audit_replay_ok,
    }
    return data, []


def _tool_money_summary(view: ReconciliationView, _args: dict):
    money = view.report.money
    data = {
        key: {"paise": value, "rupees": rupees(value)} for key, value in money.items()
    }
    return data, []


def _tool_findings_by_reason(view: ReconciliationView, _args: dict):
    counts: dict[str, dict] = {}
    for exception in view.exceptions:
        entry = counts.setdefault(
            exception.reason_code.value,
            {"count": 0, "total_paise": 0, "example_subjects": []},
        )
        entry["count"] += 1
        entry["total_paise"] += exception.amount_paise
        if len(entry["example_subjects"]) < 3:
            entry["example_subjects"].append(exception.subject_id)
    for entry in counts.values():
        entry["total"] = rupees(entry["total_paise"])
    touched = [
        subject for entry in counts.values() for subject in entry["example_subjects"]
    ]
    # A controller asking what is outstanding wants the aggregate, not only the
    # per-reason split. Returning the split alone forces whoever reads this to add
    # the numbers up themselves, which is exactly the manual arithmetic this project
    # exists to remove.
    data = {
        "total_findings": sum(entry["count"] for entry in counts.values()),
        "total_value_paise": view.report.money.get("value_in_exceptions", 0),
        "total_value": rupees(view.report.money.get("value_in_exceptions", 0)),
        "by_reason": counts,
    }
    return data, touched


def _tool_list_exceptions(view: ReconciliationView, args: dict):
    reason = args.get("reason_code")
    limit = int(args.get("limit", 20))
    rows = []
    for exception in sorted(
        view.exceptions, key=lambda e: (e.reason_code.value, e.subject_id)
    ):
        if reason and exception.reason_code.value != reason:
            continue
        rows.append(
            {
                "subject_id": exception.subject_id,
                "reason_code": exception.reason_code.value,
                "amount": rupees(exception.amount_paise),
                "amount_paise": exception.amount_paise,
                "needs_human": exception.needs_human,
                "reason": exception.reason,
                "suggested_action": exception.suggested_action,
            }
        )
    rows = rows[: max(1, min(limit, 100))]
    return {"count": len(rows), "exceptions": rows}, [row["subject_id"] for row in rows]


def _tool_explain_record(view: ReconciliationView, args: dict):
    record_id = str(args.get("record_id", "")).strip()
    kind = view.record_type(record_id)
    if kind is None:
        # The hint deliberately avoids writing concrete identifiers. Anything
        # identifier-shaped in an error string would be picked up by the grounding
        # check as a record the answer referred to, and an error message must not be
        # able to launder a fabrication.
        raise KeyError(
            f"{record_id!r} is not a record in this batch. Identifiers are a type "
            f"prefix and a zero-padded number, where the prefix is one of order, "
            f"pay, rfnd, disp, adj, setl or bank."
        )

    data: dict[str, Any] = {"record_id": record_id, "record_type": kind}
    touched = [record_id]

    if kind == "settlement":
        settlement = view.ledger.settlements[record_id]
        recomputed = view.ledger.computed_net_paise(record_id)
        credit = view.settlement_to_credit.get(record_id)
        data.update(
            {
                "settled_on": settlement.settled_at.date().isoformat(),
                "kind": settlement.kind.value,
                "gateway_reference": settlement.utr,
                "gateway_reported_net": rupees(settlement.net_paise),
                "recomputed_net_from_line_items": rupees(recomputed),
                "header_agrees_with_line_items": settlement.net_paise == recomputed,
                "matched_to_bank_credit": credit,
                "match_pass": (
                    view.match_by_subject[record_id].pass_name
                    if record_id in view.match_by_subject
                    else None
                ),
            }
        )
        if credit:
            touched.append(credit)
    elif kind == "bank_txn":
        txn = view.ledger.bank_txns[record_id]
        settlements = view.credit_to_settlements.get(record_id, ())
        data.update(
            {
                "value_date": txn.value_date.isoformat(),
                "direction": "credit" if txn.is_credit else "debit",
                "amount": rupees(txn.amount_paise),
                "narration": txn.narration,
                "reference_parsed_from_narration": txn.utr_hint,
                "explains_settlements": list(settlements),
                "is_sweep": len(settlements) > 1,
                "match_pass": (
                    view.match_by_subject[record_id].pass_name
                    if record_id in view.match_by_subject
                    else None
                ),
            }
        )
        touched.extend(settlements)
    elif kind == "payment":
        payment = view.ledger.payments[record_id]
        order_id = view.payment_to_order.get(record_id)
        settlement_id = payment.settlement_id
        data.update(
            {
                "captured_at": payment.captured_at.isoformat(),
                "method": payment.method,
                "gross": rupees(payment.gross_paise),
                "fee": rupees(payment.fee_paise),
                "gst_on_fee": rupees(payment.tax_paise),
                "net_to_merchant": rupees(payment.net_paise),
                "order_reference_on_payment": payment.order_id,
                "matched_to_order": order_id,
                "in_settlement": settlement_id,
            }
        )
        if order_id:
            touched.append(order_id)
        if settlement_id:
            touched.append(settlement_id)
    elif kind == "order":
        order = view.ledger.orders[record_id]
        payment_id = view.order_to_payment.get(record_id)
        data.update(
            {
                "created_at": order.created_at.isoformat(),
                "merchant_ref": order.merchant_ref,
                "customer_id": order.customer_id,
                "amount": rupees(order.amount_paise),
                "status_in_merchant_ledger": order.status.value,
                "matched_to_payment": payment_id,
            }
        )
        if payment_id:
            touched.append(payment_id)
    elif kind == "refund":
        refund = view.ledger.refunds[record_id]
        data.update(
            {
                "payment_id": refund.payment_id,
                "amount": rupees(refund.amount_paise),
                "is_partial": refund.is_partial,
                "created_at": refund.created_at.isoformat(),
                "deducted_from_settlement": refund.settlement_id,
            }
        )
        touched.append(refund.payment_id)
        if refund.settlement_id:
            touched.append(refund.settlement_id)
    elif kind == "chargeback":
        chargeback = view.ledger.chargebacks[record_id]
        data.update(
            {
                "payment_id": chargeback.payment_id,
                "disputed_amount": rupees(chargeback.amount_paise),
                "dispute_fee": rupees(chargeback.fee_paise),
                "created_at": chargeback.created_at.isoformat(),
                "deducted_from_settlement": chargeback.settlement_id,
            }
        )
        touched.append(chargeback.payment_id)
        if chargeback.settlement_id:
            touched.append(chargeback.settlement_id)
    else:
        adjustment = view.ledger.adjustments[record_id]
        data.update(
            {
                "amount": rupees(adjustment.amount_paise),
                "description": adjustment.description,
                "created_at": adjustment.created_at.isoformat(),
                "applied_to_settlement": adjustment.settlement_id,
            }
        )
        if adjustment.settlement_id:
            touched.append(adjustment.settlement_id)

    findings = view.exceptions_by_subject.get(record_id, [])
    data["findings_raised"] = [
        {
            "reason_code": finding.reason_code.value,
            "reason": finding.reason,
            "amount": rupees(finding.amount_paise),
            "suggested_action": finding.suggested_action,
        }
        for finding in findings
    ]
    return data, touched


def _tool_settlement_breakdown(view: ReconciliationView, args: dict):
    settlement_id = str(args.get("settlement_id", "")).strip()
    if settlement_id not in view.ledger.settlements:
        raise KeyError(f"{settlement_id!r} is not a settlement in this batch.")

    settlement = view.ledger.settlements[settlement_id]
    lines = view.ledger.lines_for_settlement(settlement_id)
    payments_net = sum(p.net_paise for p in lines["payments"])
    refunds_total = sum(r.amount_paise for r in lines["refunds"])
    chargebacks_total = sum(c.amount_paise + c.fee_paise for c in lines["chargebacks"])
    adjustments_total = sum(a.amount_paise for a in lines["adjustments"])
    recomputed = payments_net - refunds_total - chargebacks_total + adjustments_total

    touched = [settlement_id]
    touched.extend(p.payment_id for p in lines["payments"])
    touched.extend(r.refund_id for r in lines["refunds"])
    touched.extend(c.dispute_id for c in lines["chargebacks"])
    touched.extend(a.adjustment_id for a in lines["adjustments"])
    credit = view.settlement_to_credit.get(settlement_id)
    if credit:
        touched.append(credit)

    data = {
        "settlement_id": settlement_id,
        "settled_on": settlement.settled_at.date().isoformat(),
        "netting_identity": (
            "net = sum(payment.gross - fee - tax) - sum(refund) "
            "- sum(chargeback + dispute fee) + sum(adjustment)"
        ),
        "components": {
            "payments_net": rupees(payments_net),
            "refunds_deducted": rupees(refunds_total),
            "chargebacks_and_fees_deducted": rupees(chargebacks_total),
            "adjustments_applied": rupees(adjustments_total),
        },
        "line_counts": {name: len(values) for name, values in lines.items()},
        "recomputed_net": rupees(recomputed),
        "gateway_reported_net": rupees(settlement.net_paise),
        "difference": rupees(settlement.net_paise - recomputed),
        "header_agrees_with_line_items": settlement.net_paise == recomputed,
        "matched_bank_credit": credit,
        "payment_ids": [p.payment_id for p in lines["payments"]],
        "refund_ids": [r.refund_id for r in lines["refunds"]],
        "chargeback_ids": [c.dispute_id for c in lines["chargebacks"]],
        "adjustment_ids": [a.adjustment_id for a in lines["adjustments"]],
    }
    return data, touched


def _tool_trace_payment(view: ReconciliationView, args: dict):
    payment_id = str(args.get("payment_id", "")).strip()
    if payment_id not in view.ledger.payments:
        raise KeyError(f"{payment_id!r} is not a payment in this batch.")
    payment = view.ledger.payments[payment_id]
    order_id = view.payment_to_order.get(payment_id)
    settlement_id = payment.settlement_id
    credit = view.settlement_to_credit.get(settlement_id) if settlement_id else None

    touched = [payment_id]
    chain = []
    if order_id:
        order = view.ledger.orders[order_id]
        chain.append(
            {
                "stage": "order",
                "id": order_id,
                "amount": rupees(order.amount_paise),
                "status": order.status.value,
            }
        )
        touched.append(order_id)
    chain.append(
        {
            "stage": "payment",
            "id": payment_id,
            "gross": rupees(payment.gross_paise),
            "net_after_fees": rupees(payment.net_paise),
            "method": payment.method,
        }
    )
    if settlement_id:
        chain.append(
            {
                "stage": "settlement",
                "id": settlement_id,
                "recomputed_net": rupees(view.ledger.computed_net_paise(settlement_id)),
                "settled_on": view.ledger.settlements[settlement_id]
                .settled_at.date()
                .isoformat(),
            }
        )
        touched.append(settlement_id)
    if credit:
        txn = view.ledger.bank_txns[credit]
        chain.append(
            {
                "stage": "bank_credit",
                "id": credit,
                "amount": rupees(txn.amount_paise),
                "value_date": txn.value_date.isoformat(),
            }
        )
        touched.append(credit)

    data = {
        "payment_id": payment_id,
        "chain": chain,
        "fully_traced_to_bank": credit is not None,
        "broken_at": None
        if credit
        else ("order" if not order_id else "settlement" if not settlement_id else "bank"),
    }
    return data, touched


def _tool_search_bank_credits(view: ReconciliationView, args: dict):
    unmatched_only = bool(args.get("unmatched_only", False))
    limit = int(args.get("limit", 20))
    min_rupees = args.get("min_amount_rupees")
    max_rupees = args.get("max_amount_rupees")

    rows = []
    for txn in sorted(view.ledger.credits(), key=lambda t: (t.value_date, t.bank_txn_id)):
        matched = txn.bank_txn_id in view.credit_to_settlements
        if unmatched_only and matched:
            continue
        if min_rupees is not None and txn.amount_paise < float(min_rupees) * 100:
            continue
        if max_rupees is not None and txn.amount_paise > float(max_rupees) * 100:
            continue
        rows.append(
            {
                "bank_txn_id": txn.bank_txn_id,
                "value_date": txn.value_date.isoformat(),
                "amount": rupees(txn.amount_paise),
                "narration": txn.narration,
                "matched": matched,
                "explains_settlements": list(view.credit_to_settlements.get(txn.bank_txn_id, ())),
            }
        )
    rows = rows[: max(1, min(limit, 100))]
    return {"count": len(rows), "credits": rows}, [row["bank_txn_id"] for row in rows]


def _tool_gate_rejections(view: ReconciliationView, _args: dict):
    """Proposals that failed verification. Often the most interesting question."""
    rows = [
        {"subject_id": subject, "from_pass": pass_name, "why_rejected": failure}
        for pass_name, subject, failure in view.report.verifier_rejections
    ]
    return {"count": len(rows), "rejections": rows}, [row["subject_id"] for row in rows]


TOOLS: dict[str, Tool] = {
    "reconciliation_summary": Tool(
        name="reconciliation_summary",
        description=(
            "Headline outcome of the run: batch size, the three match tiers, "
            "precision and recall against ground truth, throughput, and audit checks. "
            "Start here for any question about how the run went overall."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_tool_reconciliation_summary,
    ),
    "money_summary": Tool(
        name="money_summary",
        description=(
            "Money totals for the batch: gross collected, fees and GST, refunds, "
            "chargebacks, recomputed settled net, bank credits on the statement, and "
            "the value currently sitting in unresolved exceptions."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_tool_money_summary,
    ),
    "findings_by_reason": Tool(
        name="findings_by_reason",
        description=(
            "Counts and total value of unresolved findings grouped by reason code, "
            "with a few example record identifiers for each."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_tool_findings_by_reason,
    ),
    "list_exceptions": Tool(
        name="list_exceptions",
        description=(
            "The exception ledger. Optionally filter to one reason code, for example "
            "NO_BANK_CREDIT, UNATTRIBUTED_BANK_CREDIT, NET_IDENTITY_BREAK, "
            "DUPLICATE_BANK_CREDIT, ORPHAN_PAYMENT, ORDER_AMOUNT_DRIFT."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason_code": {"type": "string", "description": "Optional reason code filter."},
                "limit": {"type": "integer", "description": "Max rows, default 20."},
            },
        },
        fn=_tool_list_exceptions,
    ),
    "explain_record": Tool(
        name="explain_record",
        description=(
            "Everything known about one record of any type: what it is, whether it "
            "matched, to what, by which pass, and any findings raised against it. "
            "Use this whenever the question names a specific identifier."
        ),
        parameters={
            "type": "object",
            "properties": {
                "record_id": {
                    "type": "string",
                    "description": (
                        "A record identifier: a type prefix of order, pay, rfnd, disp, "
                        "adj, setl or bank, an underscore, then a zero-padded number."
                    ),
                }
            },
            "required": ["record_id"],
        },
        fn=_tool_explain_record,
    ),
    "settlement_breakdown": Tool(
        name="settlement_breakdown",
        description=(
            "Full netting arithmetic for one settlement: payments net of fees, refunds "
            "deducted, chargebacks deducted, adjustments applied, the recomputed net, "
            "and whether the gateway's reported header figure agrees with it."
        ),
        parameters={
            "type": "object",
            "properties": {"settlement_id": {"type": "string"}},
            "required": ["settlement_id"],
        },
        fn=_tool_settlement_breakdown,
    ),
    "trace_payment": Tool(
        name="trace_payment",
        description=(
            "Follow one payment along the chain order to payment to settlement to bank "
            "credit, and report where the chain breaks if it does."
        ),
        parameters={
            "type": "object",
            "properties": {"payment_id": {"type": "string"}},
            "required": ["payment_id"],
        },
        fn=_tool_trace_payment,
    ),
    "search_bank_credits": Tool(
        name="search_bank_credits",
        description=(
            "Search bank statement credits, optionally only unmatched ones, optionally "
            "filtered by an amount range in rupees."
        ),
        parameters={
            "type": "object",
            "properties": {
                "unmatched_only": {"type": "boolean"},
                "min_amount_rupees": {"type": "number"},
                "max_amount_rupees": {"type": "number"},
                "limit": {"type": "integer"},
            },
        },
        fn=_tool_search_bank_credits,
    ),
    "gate_rejections": Tool(
        name="gate_rejections",
        description=(
            "Candidate matches that a pass proposed and the deterministic verification "
            "gate rejected, with the arithmetic reason each was rejected."
        ),
        parameters={"type": "object", "properties": {}},
        fn=_tool_gate_rejections,
    ),
}


def run_tool(view: ReconciliationView, name: str, args: dict) -> ToolResult:
    started = time.perf_counter()
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(
            tool=name,
            args=args,
            ok=False,
            error=f"unknown tool {name!r}; available: {sorted(TOOLS)}",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    try:
        data, touched = tool.fn(view, args or {})
        return ToolResult(
            tool=name,
            args=args,
            ok=True,
            data=data,
            records_touched=[t for t in touched if t],
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool error
        return ToolResult(
            tool=name,
            args=args,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=(time.perf_counter() - started) * 1000,
        )


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


@dataclass
class Answer:
    question: str
    text: str
    backend: str
    tool_calls: list[ToolResult] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    ungrounded_citations: list[str] = field(default_factory=list)
    steps: int = 0
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def grounded(self) -> bool:
        """Whether every identifier in the prose came from a tool result."""
        return not self.ungrounded_citations

    @property
    def tools_used(self) -> list[str]:
        return [call.tool for call in self.tool_calls]

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * USD_PER_MILLION_INPUT_TOKENS
            + self.output_tokens / 1_000_000 * USD_PER_MILLION_OUTPUT_TOKENS
        )


def check_grounding(text: str, tool_calls: list[ToolResult]) -> tuple[list[str], list[str]]:
    """Split identifiers in ``text`` into grounded and ungrounded.

    Grounded means a tool call actually surfaced that identifier. An identifier the
    model wrote without any tool having returned it is a fabrication, regardless of
    whether it happens to exist in the ledger, because the model had no legitimate
    way to know about it.
    """
    surfaced: set[str] = set()
    for call in tool_calls:
        surfaced.update(call.records_touched)
        # Identifiers can also appear inside a tool's payload, for example the
        # payment_ids list in a settlement breakdown.
        if call.ok:
            surfaced.update(RECORD_ID.findall(json.dumps(call.data, default=str)))
        surfaced.update(RECORD_ID.findall(json.dumps(call.args, default=str)))

    cited = list(dict.fromkeys(RECORD_ID.findall(text)))
    grounded = [record for record in cited if record in surfaced]
    ungrounded = [record for record in cited if record not in surfaced]
    return grounded, ungrounded


# ---------------------------------------------------------------------------
# Offline backend: deterministic intent router
# ---------------------------------------------------------------------------


class RouterAgent:
    """Keyword router over the same tools. The baseline, and the offline demo path.

    Honest about what it is: this does not understand a question, it recognises a
    handful of shapes. It exists so the agent is demonstrable without credentials
    and so the hosted model has something real to be compared against.
    """

    name = "router"

    def __init__(self, view: ReconciliationView) -> None:
        self.view = view

    @staticmethod
    def _has(text: str, *words: str) -> bool:
        """Whole-word match.

        Substring matching is wrong here and quietly so. Testing ``"gate" in text``
        fires on "gateway", which sent "which settlements did the gateway misreport"
        to the verification-gate tool. Word boundaries are the difference between a
        router and a coincidence.
        """
        return any(re.search(rf"\b{re.escape(word)}\b", text) for word in words)

    def ask(self, question: str) -> Answer:
        started = time.perf_counter()
        lowered = question.lower()
        calls: list[ToolResult] = []
        has = self._has

        ids = RECORD_ID.findall(question)
        if ids:
            record_id = ids[0]
            if record_id.startswith("setl_") and has(
                lowered, "break", "breakdown", "net", "composed", "composition", "line", "lines"
            ):
                calls.append(run_tool(self.view, "settlement_breakdown", {"settlement_id": record_id}))
            elif record_id.startswith("pay_") and has(lowered, "trace", "chain", "follow"):
                calls.append(run_tool(self.view, "trace_payment", {"payment_id": record_id}))
            else:
                calls.append(run_tool(self.view, "explain_record", {"record_id": record_id}))
        elif has(lowered, "misreport", "misreported", "identity", "header", "disagree", "disagrees"):
            calls.append(
                run_tool(self.view, "list_exceptions", {"reason_code": "NET_IDENTITY_BREAK"})
            )
        elif has(lowered, "reject", "rejected", "refused", "gate") and not has(lowered, "gateway"):
            calls.append(run_tool(self.view, "gate_rejections", {}))
        elif has(lowered, "unmatched", "unattributed", "unexplained") and has(
            lowered, "credit", "credits", "bank"
        ):
            calls.append(run_tool(self.view, "search_bank_credits", {"unmatched_only": True}))
        elif has(lowered, "duplicate", "duplicates", "re-post", "repost", "twice"):
            calls.append(
                run_tool(self.view, "list_exceptions", {"reason_code": "DUPLICATE_BANK_CREDIT"})
            )
        elif has(lowered, "orphan", "orphans") or (
            has(lowered, "order") and has(lowered, "no", "without") and has(lowered, "payment", "payments")
        ):
            calls.append(run_tool(self.view, "list_exceptions", {"reason_code": "ORPHAN_PAYMENT"}))
        elif has(lowered, "exception", "exceptions", "unresolved", "outstanding", "finding", "findings"):
            calls.append(run_tool(self.view, "findings_by_reason", {}))
        elif has(lowered, "money", "gross", "revenue", "fee", "fees", "gst", "refund", "refunds", "cash"):
            calls.append(run_tool(self.view, "money_summary", {}))
        else:
            calls.append(run_tool(self.view, "reconciliation_summary", {}))

        text = self._render(question, calls)
        grounded, ungrounded = check_grounding(text, calls)
        return Answer(
            question=question,
            text=text,
            backend=self.name,
            tool_calls=calls,
            citations=grounded,
            ungrounded_citations=ungrounded,
            steps=len(calls),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _render(self, question: str, calls: list[ToolResult]) -> str:
        lines = [
            "Answered by the offline keyword router, reading verified reconciliation "
            "output through the same tools the hosted model uses.",
            "",
        ]
        for call in calls:
            if not call.ok:
                lines.append(f"{call.tool} could not answer: {call.error}")
                continue
            lines.append(f"From {call.tool}:")
            lines.append(json.dumps(call.data, indent=2, default=str))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hosted model backend
# ---------------------------------------------------------------------------

_AGENT_SYSTEM_PROMPT = """\
You answer questions about a completed payment reconciliation run for a finance
team. You read the reconciled output through tools. You never perform
reconciliation yourself and you never move money.

Rules:
- Every factual claim must come from a tool result. Call tools before answering.
- Never state a record identifier that a tool has not returned to you. If you need
  a record you have not seen, call a tool to get it.
- Never compute or estimate a figure yourself. If a number is not in a tool result,
  say it is not available rather than deriving it.
- Cite the specific record identifiers your answer rests on.
- Amounts are Indian rupees, already formatted, for example 1,23,456.78. Reproduce
  them exactly as given.
- Be concise and concrete. A finance controller wants the number, the records, and
  what to do next.
- If the reconciliation genuinely could not resolve something, say so plainly and
  give the reason code. An honest unresolved item is a useful answer."""


class LLMAgent:
    """Tool-calling agent over the hosted model. Falls back to the router."""

    name = "llm"

    def __init__(
        self,
        view: ReconciliationView,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_steps: int = 6,
        timeout_s: float = 60.0,
    ) -> None:
        self.view = view
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self._router = RouterAgent(view)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def ask(self, question: str) -> Answer:
        if not self.available:
            answer = self._router.ask(question)
            answer.backend = "llm:unavailable->router"
            return answer

        started = time.perf_counter()
        messages: list[dict] = [{"role": "user", "content": question}]
        calls: list[ToolResult] = []
        input_tokens = output_tokens = 0
        text = ""

        try:
            for step in range(self.max_steps):
                body = self._call(messages)
                usage = body.get("usage", {})
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
                content = body.get("content", [])
                messages.append({"role": "assistant", "content": content})

                tool_uses = [
                    block for block in content if block.get("type") == "tool_use"
                ]
                text = "".join(
                    block.get("text", "") for block in content if block.get("type") == "text"
                )

                if not tool_uses:
                    break

                results = []
                for block in tool_uses:
                    result = run_tool(self.view, block.get("name", ""), block.get("input") or {})
                    calls.append(result)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.get("id"),
                            "content": result.payload(),
                            "is_error": not result.ok,
                        }
                    )
                messages.append({"role": "user", "content": results})
            else:
                text = text or (
                    "Reached the tool-call limit without producing an answer. "
                    "The question may need to be narrower."
                )
        except Exception as exc:  # noqa: BLE001 - degrade to the offline path
            answer = self._router.ask(question)
            answer.backend = f"{self.name}:error->router"
            answer.error = f"{type(exc).__name__}: {exc}"
            answer.tool_calls = calls + answer.tool_calls
            return answer

        grounded, ungrounded = check_grounding(text, calls)
        return Answer(
            question=question,
            text=text.strip(),
            backend=f"{self.name}:{self.model}",
            tool_calls=calls,
            citations=grounded,
            ungrounded_citations=ungrounded,
            steps=len(calls),
            latency_ms=(time.perf_counter() - started) * 1000,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _call(self, messages: list[dict]) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": 1_500,
            "temperature": 0,
            "system": _AGENT_SYSTEM_PROMPT,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in TOOLS.values()
            ],
            "messages": messages,
        }
        request = urllib.request.Request(
            ANTHROPIC_URL,
            data=json.dumps(payload, default=str).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key or "",
                "anthropic-version": ANTHROPIC_VERSION,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))


def build_agent(view: ReconciliationView, backend: str = "auto"):
    """Choose an agent backend, preferring the hosted model when a key is present."""
    backend = (backend or "auto").lower()
    if backend == "router":
        return RouterAgent(view)
    if backend == "llm":
        agent = LLMAgent(view)
        if not agent.available:
            raise RuntimeError(
                "backend 'llm' requires ANTHROPIC_API_KEY. Use --backend auto to fall "
                "back to the offline router."
            )
        return agent
    if backend == "auto":
        agent = LLMAgent(view)
        return agent if agent.available else RouterAgent(view)
    raise ValueError(f"unknown agent backend {backend!r}")
