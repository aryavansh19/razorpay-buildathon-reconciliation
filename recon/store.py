"""Writing the generated dataset and the run artefacts to disk.

The three sources are written as separate CSVs that look like what a merchant
actually receives: an order export, a gateway settlement report, and a bank
statement. Keeping them as three files rather than one joined table matters,
because the joins are the problem under study and a pre-joined fixture would
quietly assume them away.

Ground truth is written to its own file so a reviewer can check any individual
claim by hand. The reconciler never reads it.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from .models import GroundTruth, Ledger, ReconException, rupees


def _write_csv(path: Path, headers: list[str], rows: list[list]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def write_dataset(ledger: Ledger, directory: str | Path) -> dict[str, Path]:
    """Write the three sources as CSV. Returns the paths written."""
    directory = Path(directory)
    written: dict[str, Path] = {}

    written["orders"] = _write_csv(
        directory / "merchant_orders.csv",
        ["order_id", "merchant_ref", "customer_id", "amount_paise", "currency", "created_at", "status"],
        [
            [
                order.order_id,
                order.merchant_ref,
                order.customer_id,
                order.amount_paise,
                order.currency,
                order.created_at.isoformat(),
                order.status.value,
            ]
            for order in sorted(ledger.orders.values(), key=lambda o: o.order_id)
        ],
    )

    written["payments"] = _write_csv(
        directory / "settlement_report_payments.csv",
        [
            "payment_id",
            "order_id",
            "gross_paise",
            "fee_paise",
            "tax_paise",
            "net_paise",
            "method",
            "currency",
            "captured_at",
            "settlement_id",
        ],
        [
            [
                payment.payment_id,
                payment.order_id or "",
                payment.gross_paise,
                payment.fee_paise,
                payment.tax_paise,
                payment.net_paise,
                payment.method,
                payment.currency,
                payment.captured_at.isoformat(),
                payment.settlement_id or "",
            ]
            for payment in sorted(ledger.payments.values(), key=lambda p: p.payment_id)
        ],
    )

    written["refunds"] = _write_csv(
        directory / "settlement_report_refunds.csv",
        ["refund_id", "payment_id", "amount_paise", "is_partial", "created_at", "settlement_id"],
        [
            [
                refund.refund_id,
                refund.payment_id,
                refund.amount_paise,
                int(refund.is_partial),
                refund.created_at.isoformat(),
                refund.settlement_id or "",
            ]
            for refund in sorted(ledger.refunds.values(), key=lambda r: r.refund_id)
        ],
    )

    written["chargebacks"] = _write_csv(
        directory / "settlement_report_chargebacks.csv",
        ["dispute_id", "payment_id", "amount_paise", "fee_paise", "created_at", "settlement_id"],
        [
            [
                chargeback.dispute_id,
                chargeback.payment_id,
                chargeback.amount_paise,
                chargeback.fee_paise,
                chargeback.created_at.isoformat(),
                chargeback.settlement_id or "",
            ]
            for chargeback in sorted(ledger.chargebacks.values(), key=lambda c: c.dispute_id)
        ],
    )

    written["adjustments"] = _write_csv(
        directory / "settlement_report_adjustments.csv",
        ["adjustment_id", "amount_paise", "description", "created_at", "settlement_id"],
        [
            [
                adjustment.adjustment_id,
                adjustment.amount_paise,
                adjustment.description,
                adjustment.created_at.isoformat(),
                adjustment.settlement_id or "",
            ]
            for adjustment in sorted(ledger.adjustments.values(), key=lambda a: a.adjustment_id)
        ],
    )

    written["settlements"] = _write_csv(
        directory / "settlement_report_headers.csv",
        [
            "settlement_id",
            "utr",
            "gateway_reported_net_paise",
            "recomputed_net_paise",
            "settled_at",
            "kind",
        ],
        [
            [
                settlement.settlement_id,
                settlement.utr,
                settlement.net_paise,
                ledger.computed_net_paise(settlement.settlement_id),
                settlement.settled_at.isoformat(),
                settlement.kind.value,
            ]
            for settlement in sorted(
                ledger.settlements.values(), key=lambda s: s.settlement_id
            )
        ],
    )

    written["bank_statement"] = _write_csv(
        directory / "bank_statement.csv",
        ["bank_txn_id", "value_date", "amount_paise", "direction", "narration", "parsed_reference"],
        [
            [
                txn.bank_txn_id,
                txn.value_date.isoformat(),
                txn.amount_paise,
                "credit" if txn.is_credit else "debit",
                txn.narration,
                txn.utr_hint or "",
            ]
            for txn in sorted(ledger.bank_txns.values(), key=lambda t: t.bank_txn_id)
        ],
    )

    return written


def write_ground_truth(truth: GroundTruth, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(truth), indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return path


def write_exception_ledger(exceptions: list[ReconException], path: str | Path) -> Path:
    """The exception list as a CSV a finance team could actually work from."""
    path = Path(path)
    return _write_csv(
        path,
        [
            "subject_id",
            "correspondence",
            "reason_code",
            "amount_paise",
            "amount",
            "needs_human",
            "reason",
            "suggested_action",
        ],
        [
            [
                exception.subject_id,
                exception.kind.value,
                exception.reason_code.value,
                exception.amount_paise,
                rupees(exception.amount_paise),
                "yes" if exception.needs_human else "no",
                exception.reason,
                exception.suggested_action,
            ]
            for exception in sorted(
                exceptions, key=lambda e: (e.reason_code.value, e.subject_id)
            )
        ],
    )
