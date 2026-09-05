"""Self-contained HTML report.

One file, no build step, no dependencies, no server required. Everything the page
needs is inlined: the data as a JSON payload, the styles, and the interaction code.
Open it by double-clicking, or commit it and read it on GitHub Pages.

Why this exists alongside ``report.md``
--------------------------------------
The exception ledger's reader is a finance controller, not an engineer. A CSV and a
terminal dump are the right medium for a reviewer checking the numbers, and the
wrong medium for the person whose job is to clear 47 findings. Being able to filter
by reason code, sort by value, and click into a settlement to see the netting
arithmetic that produced it is the difference between output and a tool.

It also means the drill-down is auditable by hand. Clicking ``setl_00016`` shows the
payments, refunds, chargebacks and adjustments that were summed, the recomputed
figure, and the gateway's reported figure next to it. A reader can check the
arithmetic themselves rather than taking the match rate on trust.

Accessibility
-------------
Semantic tables with captions and column scopes, real buttons rather than clickable
divs, visible keyboard focus, ``aria-live`` on the regions that update, labelled
inputs, and a contrast ratio above 4.5:1 for body text. Every interaction is
reachable by keyboard alone.
"""

from __future__ import annotations

import json
from typing import Any

from .models import MatchKind, Tier, rupees
from .pipeline import PipelineResult


def _build_payload(result: PipelineResult) -> dict[str, Any]:
    """Everything the page renders, as plain JSON-serialisable data."""
    report = result.report
    ledger = result.ledger

    settlement_to_credit: dict[str, str] = {}
    credit_to_settlements: dict[str, list[str]] = {}
    payment_to_order: dict[str, str] = {}
    match_pass: dict[str, str] = {}
    match_tier: dict[str, str] = {}

    for match in result.matches:
        if match.kind is MatchKind.SETTLEMENT_BANK:
            credit_to_settlements[match.left_id] = list(match.right_ids)
            match_pass[match.left_id] = match.pass_name
            match_tier[match.left_id] = match.tier.value
            for settlement_id in match.right_ids:
                settlement_to_credit[settlement_id] = match.left_id
                match_pass[settlement_id] = match.pass_name
                match_tier[settlement_id] = match.tier.value
        else:
            payment_to_order[match.left_id] = match.right_ids[0]
            match_pass[match.left_id] = match.pass_name
            match_tier[match.left_id] = match.tier.value

    findings_by_subject: dict[str, list[dict]] = {}
    for exception in result.exceptions:
        findings_by_subject.setdefault(exception.subject_id, []).append(
            {
                "reason_code": exception.reason_code.value,
                "reason": exception.reason,
                "amount": rupees(exception.amount_paise),
                "suggested_action": exception.suggested_action,
            }
        )

    records: dict[str, dict] = {}

    for settlement_id, settlement in ledger.settlements.items():
        lines = ledger.lines_for_settlement(settlement_id)
        payments_net = sum(p.net_paise for p in lines["payments"])
        refunds_total = sum(r.amount_paise for r in lines["refunds"])
        chargebacks_total = sum(c.amount_paise + c.fee_paise for c in lines["chargebacks"])
        adjustments_total = sum(a.amount_paise for a in lines["adjustments"])
        recomputed = payments_net - refunds_total - chargebacks_total + adjustments_total
        records[settlement_id] = {
            "type": "settlement",
            "id": settlement_id,
            "settled_on": settlement.settled_at.date().isoformat(),
            "kind": settlement.kind.value,
            "reference": settlement.utr,
            "reported_net": rupees(settlement.net_paise),
            "recomputed_net": rupees(recomputed),
            "difference": rupees(settlement.net_paise - recomputed),
            "header_agrees": settlement.net_paise == recomputed,
            "amount_paise": recomputed,
            "matched_credit": settlement_to_credit.get(settlement_id),
            "match_pass": match_pass.get(settlement_id),
            "breakdown": [
                {"label": "Payments, net of fees and GST", "value": rupees(payments_net)},
                {"label": "Refunds deducted", "value": "-" + rupees(refunds_total)},
                {
                    "label": "Chargebacks and dispute fees deducted",
                    "value": "-" + rupees(chargebacks_total),
                },
                {"label": "Adjustments applied", "value": rupees(adjustments_total)},
            ],
            "line_ids": {
                "payments": [p.payment_id for p in lines["payments"]],
                "refunds": [r.refund_id for r in lines["refunds"]],
                "chargebacks": [c.dispute_id for c in lines["chargebacks"]],
                "adjustments": [a.adjustment_id for a in lines["adjustments"]],
            },
            "findings": findings_by_subject.get(settlement_id, []),
        }

    for bank_txn_id, txn in ledger.bank_txns.items():
        records[bank_txn_id] = {
            "type": "bank_txn",
            "id": bank_txn_id,
            "value_date": txn.value_date.isoformat(),
            "direction": "credit" if txn.is_credit else "debit",
            "amount": rupees(txn.amount_paise),
            "amount_paise": txn.amount_paise,
            "narration": txn.narration,
            "parsed_reference": txn.utr_hint,
            "explains": credit_to_settlements.get(bank_txn_id, []),
            "is_sweep": len(credit_to_settlements.get(bank_txn_id, [])) > 1,
            "match_pass": match_pass.get(bank_txn_id),
            "match_tier": match_tier.get(bank_txn_id),
            "findings": findings_by_subject.get(bank_txn_id, []),
        }

    for payment_id, payment in ledger.payments.items():
        records[payment_id] = {
            "type": "payment",
            "id": payment_id,
            "captured_at": payment.captured_at.isoformat(sep=" ")[:16],
            "method": payment.method,
            "gross": rupees(payment.gross_paise),
            "fee": rupees(payment.fee_paise),
            "gst": rupees(payment.tax_paise),
            "net": rupees(payment.net_paise),
            "amount_paise": payment.gross_paise,
            "order_reference": payment.order_id,
            "matched_order": payment_to_order.get(payment_id),
            "settlement": payment.settlement_id,
            "findings": findings_by_subject.get(payment_id, []),
        }

    for order_id, order in ledger.orders.items():
        records[order_id] = {
            "type": "order",
            "id": order_id,
            "created_at": order.created_at.isoformat(sep=" ")[:16],
            "amount": rupees(order.amount_paise),
            "amount_paise": order.amount_paise,
            "status": order.status.value,
            "customer": order.customer_id,
            "merchant_ref": order.merchant_ref,
            "findings": findings_by_subject.get(order_id, []),
        }

    for refund_id, refund in ledger.refunds.items():
        records[refund_id] = {
            "type": "refund",
            "id": refund_id,
            "amount": rupees(refund.amount_paise),
            "amount_paise": refund.amount_paise,
            "is_partial": refund.is_partial,
            "payment": refund.payment_id,
            "settlement": refund.settlement_id,
            "created_at": refund.created_at.isoformat(sep=" ")[:16],
            "findings": [],
        }

    for dispute_id, chargeback in ledger.chargebacks.items():
        records[dispute_id] = {
            "type": "chargeback",
            "id": dispute_id,
            "amount": rupees(chargeback.amount_paise),
            "fee": rupees(chargeback.fee_paise),
            "amount_paise": chargeback.amount_paise,
            "payment": chargeback.payment_id,
            "settlement": chargeback.settlement_id,
            "created_at": chargeback.created_at.isoformat(sep=" ")[:16],
            "findings": [],
        }

    for adjustment_id, adjustment in ledger.adjustments.items():
        records[adjustment_id] = {
            "type": "adjustment",
            "id": adjustment_id,
            "amount": rupees(adjustment.amount_paise),
            "amount_paise": adjustment.amount_paise,
            "description": adjustment.description,
            "settlement": adjustment.settlement_id,
            "findings": [],
        }

    exceptions = [
        {
            "subject_id": exception.subject_id,
            "correspondence": exception.kind.value,
            "reason_code": exception.reason_code.value,
            "amount": rupees(exception.amount_paise),
            "amount_paise": exception.amount_paise,
            "needs_human": exception.needs_human,
            "reason": exception.reason,
            "suggested_action": exception.suggested_action,
        }
        for exception in sorted(
            result.exceptions, key=lambda e: (-e.amount_paise, e.subject_id)
        )
    ]

    sb, po, ex = report.settlement_bank, report.payment_order, report.exceptions_score

    return {
        "meta": {
            "seed": report.seed,
            "records": report.total_records,
            "composition": report.ledger_summary,
            "wall_seconds": round(report.wall_seconds, 3),
            "records_per_second": round(report.records_per_second),
            "generated_at": report.generated_at,
            "classifier_backend": report.classifier_usage.backend or "offline baseline",
        },
        "tiers": {
            "auto": report.auto_matched,
            "auto_value": rupees(report.tier_value_paise.get(Tier.AUTO.value, 0)),
            "assisted": report.assisted_matched,
            "assisted_value": rupees(report.tier_value_paise.get(Tier.ASSISTED.value, 0)),
            "unresolved": report.unresolved,
            "unresolved_value": rupees(report.money.get("value_in_exceptions", 0)),
            "combined_match_rate": round(report.combined_match_rate, 2),
            "correspondences": report.total_correspondences_expected,
        },
        "quality": [
            {
                "label": "Settlement to bank",
                "expected": len(sb.expected),
                "correct": len(sb.true_positives),
                "false_positives": len(sb.false_positives),
                "missed": len(sb.false_negatives),
                "precision": round(sb.precision, 2),
                "recall": round(sb.recall, 2),
            },
            {
                "label": "Payment to order",
                "expected": len(po.expected),
                "correct": len(po.true_positives),
                "false_positives": len(po.false_positives),
                "missed": len(po.false_negatives),
                "precision": round(po.precision, 2),
                "recall": round(po.recall, 2),
            },
            {
                "label": "Exception ledger, (record, reason) pairs",
                "expected": ex.expected_total,
                "correct": len(ex.exact),
                "false_positives": len(ex.spurious),
                "missed": len(ex.missed),
                "precision": round(ex.pair_precision, 2),
                "recall": round(ex.pair_recall, 2),
            },
        ],
        "false_positive_detail": {
            "settlement_to_bank": sorted(sb.false_positives),
            "payment_to_order": sorted(po.false_positives),
        },
        "passes": [
            {
                "name": stats.name,
                "considered": stats.considered,
                "accepted": stats.accepted,
                "rejected": stats.rejected,
                "declined": stats.declined_ambiguous,
                "ms": round(stats.elapsed_ms, 1),
                "notes": stats.notes,
            }
            for stats in report.pass_stats
        ],
        "gate_rejections": [
            {"subject": subject, "pass": pass_name, "failure": failure}
            for pass_name, subject, failure in report.verifier_rejections
        ],
        "exceptions": exceptions,
        "records": records,
        "audit": {
            "events": report.audit_events,
            "chain_intact": report.audit_chain_ok,
            "replay_ok": report.audit_replay_ok,
            "coverage_holes": len(report.coverage_holes),
        },
        "tolerance": {
            "amount_paise": report.tolerance.amount_paise,
            "settlement_lag_days": report.tolerance.settlement_lag_days,
            "max_sweep_size": report.tolerance.max_sweep_size,
            "subset_sum_node_budget": report.tolerance.subset_sum_node_budget,
            "match_payments_to_inactive_orders": (
                report.tolerance.match_payments_to_inactive_orders
            ),
        },
        "injected": report.injected_scenarios,
        "money": {key: rupees(value) for key, value in report.money.items()},
        "cost": {
            "backend": report.classifier_usage.backend or "offline baseline",
            "items": report.classifier_usage.calls,
            "input_tokens": report.classifier_usage.input_tokens,
            "output_tokens": report.classifier_usage.output_tokens,
            "usd": round(report.classifier_usage.estimated_cost_usd, 6),
            "mean_latency_ms": round(report.classifier_usage.mean_latency_ms, 2),
        },
    }


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settlement reconciliation | seed __SEED__</title>
<style>
  :root {
    --ink: #10161f;
    --ink-soft: #4a5568;
    --line: #dfe4ec;
    --bg: #f6f8fb;
    --card: #ffffff;
    --accent: #0b5fd0;
    --accent-soft: #e8f1fd;
    --good: #1a6b3c;
    --good-soft: #e6f4ec;
    --warn: #8a4b06;
    --warn-soft: #fdf1e2;
    --bad: #a11c1c;
    --bad-soft: #fbeaea;
    --mono: ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 64px; }
  a { color: var(--accent); }
  h1 { font-size: 25px; margin: 0 0 6px; letter-spacing: -0.01em; }
  h2 { font-size: 18px; margin: 34px 0 12px; letter-spacing: -0.01em; }
  h3 { font-size: 15px; margin: 20px 0 8px; }
  p { margin: 8px 0; }
  .sub { color: var(--ink-soft); font-size: 13.5px; }
  code, .mono { font-family: var(--mono); font-size: 12.5px; }

  .banner {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 18px 20px; margin-bottom: 22px;
  }
  .synthetic {
    margin-top: 10px; padding: 9px 12px; border-radius: 7px;
    background: var(--warn-soft); color: var(--warn); font-size: 13px;
  }

  nav.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 20px; }
  nav.tabs button {
    font: inherit; font-size: 13.5px; padding: 8px 14px; cursor: pointer;
    background: var(--card); color: var(--ink-soft);
    border: 1px solid var(--line); border-radius: 999px;
  }
  nav.tabs button[aria-selected="true"] {
    background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600;
  }
  button:focus-visible, input:focus-visible, [tabindex]:focus-visible {
    outline: 3px solid #7ab3f5; outline-offset: 2px;
  }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 14px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 16px 18px;
  }
  .card .label { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-soft); }
  .card .big { font-size: 30px; font-weight: 650; margin: 6px 0 2px; letter-spacing: -0.02em; }
  .card .money { font-family: var(--mono); font-size: 13px; color: var(--ink-soft); }
  .card .how { font-size: 12.5px; color: var(--ink-soft); margin-top: 8px; }
  .card.accent { border-left: 4px solid var(--accent); }
  .card.good { border-left: 4px solid var(--good); }
  .card.warn { border-left: 4px solid var(--warn); }

  table { width: 100%; border-collapse: collapse; background: var(--card); font-size: 13.5px; }
  caption { text-align: left; font-size: 13px; color: var(--ink-soft); padding: 0 0 8px; }
  th, td { text-align: left; padding: 9px 11px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { background: #eef2f7; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-soft); }
  td.num, th.num { text-align: right; font-family: var(--mono); font-size: 12.5px; white-space: nowrap; }
  .tablewrap { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  tbody tr:last-child td { border-bottom: none; }

  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11.5px; font-weight: 600; font-family: var(--mono);
  }
  .pill.good { background: var(--good-soft); color: var(--good); }
  .pill.warn { background: var(--warn-soft); color: var(--warn); }
  .pill.bad  { background: var(--bad-soft);  color: var(--bad); }
  .pill.flat { background: #eef2f7; color: var(--ink-soft); }

  .filters { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; margin: 4px 0 14px; }
  .filters button {
    font: inherit; font-size: 12.5px; padding: 5px 11px; cursor: pointer;
    background: var(--card); border: 1px solid var(--line); border-radius: 999px; color: var(--ink-soft);
  }
  .filters button[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }
  .search { padding: 7px 11px; border: 1px solid var(--line); border-radius: 7px; font: inherit; font-size: 13px; min-width: 230px; }

  .rowbtn {
    all: unset; cursor: pointer; color: var(--accent);
    font-family: var(--mono); font-size: 12.5px; text-decoration: underline;
  }
  .rowbtn:hover { text-decoration: none; }

  .detail {
    background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--accent);
    border-radius: 10px; padding: 16px 18px; margin: 14px 0;
  }
  .kv { display: grid; grid-template-columns: minmax(150px, 220px) 1fr; gap: 4px 16px; font-size: 13.5px; }
  .kv dt { color: var(--ink-soft); }
  .kv dd { margin: 0; font-family: var(--mono); font-size: 12.5px; word-break: break-word; }
  .ledger { font-family: var(--mono); font-size: 12.5px; }
  .ledger div { display: flex; justify-content: space-between; gap: 20px; padding: 3px 0; }
  .ledger .total { border-top: 1px solid var(--line); margin-top: 5px; padding-top: 6px; font-weight: 700; }
  .idlist { display: flex; flex-wrap: wrap; gap: 5px; }

  .finding { background: var(--warn-soft); border-radius: 7px; padding: 10px 12px; margin: 8px 0; font-size: 13px; }
  .finding .code { font-family: var(--mono); font-weight: 700; color: var(--warn); }
  .finding .action { color: var(--ink-soft); margin-top: 4px; }

  .chat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
  .chatlog { max-height: 460px; overflow-y: auto; margin-bottom: 12px; }
  .msg { padding: 10px 13px; border-radius: 9px; margin: 8px 0; font-size: 13.5px; }
  .msg.you { background: var(--accent-soft); }
  .msg.bot { background: #f2f5f9; white-space: pre-wrap; }
  .msg .meta { font-size: 11.5px; color: var(--ink-soft); margin-top: 7px; font-family: var(--mono); }
  .chatform { display: flex; gap: 8px; }
  .chatform input { flex: 1; padding: 9px 12px; border: 1px solid var(--line); border-radius: 7px; font: inherit; font-size: 13.5px; }
  .chatform button {
    font: inherit; font-size: 13.5px; font-weight: 600; padding: 9px 18px; cursor: pointer;
    background: var(--accent); color: #fff; border: none; border-radius: 7px;
  }
  .chatform button:disabled { opacity: .55; cursor: not-allowed; }
  .suggestions { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 0; }
  .suggestions button {
    font: inherit; font-size: 12.5px; padding: 5px 11px; cursor: pointer;
    background: var(--bg); border: 1px solid var(--line); border-radius: 999px; color: var(--accent);
  }
  .offline { background: var(--warn-soft); color: var(--warn); padding: 12px 14px; border-radius: 8px; font-size: 13.5px; }
  [hidden] { display: none !important; }
  .visually-hidden {
    position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap;
  }
  footer { margin-top: 42px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--ink-soft); font-size: 12.5px; }
</style>
</head>
<body>
<div class="wrap">

  <header class="banner">
    <h1>Three-way settlement reconciliation</h1>
    <p class="sub">
      Razorpay AI Buildathon, Track 04 &mdash; AI Finance Controller.
      Merchant order ledger, gateway settlement report and bank statement,
      reconciled against each other.
    </p>
    <p class="sub mono" id="metaLine"></p>
    <div class="synthetic">
      All data is synthetic and generated locally from seed <strong>__SEED__</strong>.
      No real payment data, customer identifier or credential is used. Nothing here
      moves money.
    </div>
  </header>

  <nav class="tabs" role="tablist" aria-label="Report sections">
    <button role="tab" id="tab-overview"   aria-controls="panel-overview"   aria-selected="true">Overview</button>
    <button role="tab" id="tab-exceptions" aria-controls="panel-exceptions" aria-selected="false">Exception ledger</button>
    <button role="tab" id="tab-passes"     aria-controls="panel-passes"     aria-selected="false">Pass ladder</button>
    <button role="tab" id="tab-records"    aria-controls="panel-records"    aria-selected="false">Record lookup</button>
    <button role="tab" id="tab-ask"        aria-controls="panel-ask"        aria-selected="false">Ask</button>
    <button role="tab" id="tab-integrity"  aria-controls="panel-integrity"  aria-selected="false">Integrity</button>
  </nav>

  <section role="tabpanel" id="panel-overview" aria-labelledby="tab-overview">
    <h2>The three numbers</h2>
    <div class="cards" id="tierCards"></div>
    <p class="sub" id="rateLine"></p>

    <h2>Scored against ground truth</h2>
    <p class="sub">
      The generator built this data forwards and recorded the correct answer as it
      went, so these are measured rather than asserted. A false positive is worse
      than a miss: it closes a break nobody will look at again.
    </p>
    <div class="tablewrap"><table>
      <caption>Precision and recall per correspondence</caption>
      <thead><tr>
        <th scope="col">Correspondence</th><th scope="col" class="num">Expected</th>
        <th scope="col" class="num">Correct</th><th scope="col" class="num">False positive</th>
        <th scope="col" class="num">Missed</th><th scope="col" class="num">Precision</th>
        <th scope="col" class="num">Recall</th>
      </tr></thead>
      <tbody id="qualityBody"></tbody>
    </table></div>
    <p class="sub" id="fpLine"></p>

    <h2>Money</h2>
    <div class="tablewrap"><table>
      <caption>Batch totals, recomputed from line items</caption>
      <thead><tr><th scope="col">Line</th><th scope="col" class="num">Amount</th></tr></thead>
      <tbody id="moneyBody"></tbody>
    </table></div>

    <h2>Difficulty injected into this batch</h2>
    <p class="sub">
      Listed so a reader can judge whether the difficulty is representative, and
      disagree. A clean run against easy data proves nothing.
    </p>
    <div class="tablewrap"><table>
      <caption>Scenarios present in this batch</caption>
      <thead><tr><th scope="col">Scenario</th><th scope="col" class="num">Instances</th></tr></thead>
      <tbody id="injectedBody"></tbody>
    </table></div>
  </section>

  <section role="tabpanel" id="panel-exceptions" aria-labelledby="tab-exceptions" hidden>
    <h2>Exception ledger</h2>
    <p class="sub">
      Every record the pipeline could not resolve, with the reason and what a human
      should do next. Sorted by value. Click any identifier to see the underlying
      records and, for a settlement, the netting arithmetic behind it.
    </p>
    <div class="filters" id="reasonFilters" role="group" aria-label="Filter by reason code"></div>
    <div class="filters">
      <label for="excSearch" class="visually-hidden">Search the exception ledger</label>
      <input type="search" id="excSearch" class="search" placeholder="Search id, reason or text">
      <span class="sub" id="excCount" aria-live="polite"></span>
    </div>
    <div id="excDetail" aria-live="polite"></div>
    <div class="tablewrap"><table>
      <caption>Unresolved findings</caption>
      <thead><tr>
        <th scope="col">Record</th><th scope="col">Reason</th>
        <th scope="col" class="num">Amount</th><th scope="col">Detail</th>
        <th scope="col">Suggested action</th>
      </tr></thead>
      <tbody id="excBody"></tbody>
    </table></div>
  </section>

  <section role="tabpanel" id="panel-passes" aria-labelledby="tab-passes" hidden>
    <h2>Pass ladder</h2>
    <p class="sub">
      Strictest evidence first, each later pass relaxing exactly one dimension. By
      the time a looser pass runs, everything stricter evidence could explain is
      already consumed, so relaxing a constraint can only add matches rather than
      steal better ones. No pass ever picks the closest of several candidates: it
      finds exactly one explanation or declines.
    </p>
    <div class="tablewrap"><table>
      <caption>Per-pass outcome</caption>
      <thead><tr>
        <th scope="col">Pass</th><th scope="col" class="num">Considered</th>
        <th scope="col" class="num">Accepted</th><th scope="col" class="num">Rejected by gate</th>
        <th scope="col" class="num">Declined ambiguous</th><th scope="col" class="num">ms</th>
      </tr></thead>
      <tbody id="passBody"></tbody>
    </table></div>

    <h2>Proposals the verification gate rejected</h2>
    <p class="sub" id="gateIntro"></p>
    <div id="gateList"></div>
  </section>

  <section role="tabpanel" id="panel-records" aria-labelledby="tab-records" hidden>
    <h2>Record lookup</h2>
    <p class="sub">
      Any record in the batch. Settlements show the full netting identity, so the
      arithmetic behind a match can be checked by hand.
    </p>
    <div class="filters">
      <label for="recSearch" class="visually-hidden">Record identifier</label>
      <input type="search" id="recSearch" class="search" placeholder="setl_00016, bank_00041, pay_00149">
      <span class="sub" id="recHint"></span>
    </div>
    <div id="recResults" aria-live="polite"></div>
  </section>

  <section role="tabpanel" id="panel-ask" aria-labelledby="tab-ask" hidden>
    <h2>Ask about this run</h2>
    <p class="sub">
      The agent reads verified output through nine read-only tools. It cannot match,
      re-match, alter the ledger or move money. Every record identifier in an answer
      is checked back against what the tools actually returned, so a fabricated
      identifier is reported rather than presented as fact.
    </p>
    <div id="askOffline" class="offline" hidden>
      This page was opened as a file, so there is no server to answer questions.
      Run <code>python -m recon.serve</code> and open the address it prints, or use
      <code>python -m recon.ask "your question"</code> in a terminal.
    </div>
    <div class="chat" id="askLive" hidden>
      <div class="chatlog" id="chatLog" aria-live="polite" aria-atomic="false"></div>
      <form class="chatform" id="chatForm">
        <label for="chatInput" class="visually-hidden">Your question</label>
        <input type="text" id="chatInput" placeholder="Why is bank_00042 unmatched?" autocomplete="off">
        <button type="submit" id="chatSend">Ask</button>
      </form>
      <div class="suggestions" id="suggestions"></div>
    </div>
  </section>

  <section role="tabpanel" id="panel-integrity" aria-labelledby="tab-integrity" hidden>
    <h2>Audit trail</h2>
    <p class="sub">
      Every decision is one append-only event, each carrying a SHA-256 hash of its
      own content plus the previous event's hash. The run then replays the stream and
      reconstructs the final state from the events alone, and compares it to live
      state. An audit trail that cannot reproduce the outcome it describes is
      decoration.
    </p>
    <div class="cards" id="auditCards"></div>

    <h2>Declared tolerances</h2>
    <p class="sub">
      Printed because a wider tolerance would raise the match rate and lower
      precision. The trade-off should be visible rather than buried.
    </p>
    <div class="tablewrap"><table>
      <caption>Configuration in force for this run</caption>
      <thead><tr><th scope="col">Setting</th><th scope="col" class="num">Value</th></tr></thead>
      <tbody id="tolBody"></tbody>
    </table></div>

    <h2>Cost and latency</h2>
    <div class="tablewrap"><table>
      <caption>Residue classification cost</caption>
      <thead><tr><th scope="col">Measure</th><th scope="col" class="num">Value</th></tr></thead>
      <tbody id="costBody"></tbody>
    </table></div>
  </section>

  <footer>
    Generated by <code>recon.html_report</code>. Reproduce every figure with
    <code>python -m recon.cli --seed __SEED__</code>. Synthetic data only; not
    affiliated with Razorpay.
  </footer>
</div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script>
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("payload").textContent);
  var LIVE = __LIVE__;

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    if (attrs) { Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); }); }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  /* ---------- tabs ---------- */
  var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
  function selectTab(tab) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    });
    tab.focus();
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { selectTab(tab); });
    tab.addEventListener("keydown", function (event) {
      var delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
      if (!delta) { return; }
      event.preventDefault();
      selectTab(tabs[(index + delta + tabs.length) % tabs.length]);
    });
  });

  /* ---------- meta ---------- */
  var m = D.meta;
  document.getElementById("metaLine").textContent =
    m.records + " records | " + m.wall_seconds + "s wall | " +
    m.records_per_second + " records/second | residue backend " +
    m.classifier_backend + " | generated " + m.generated_at;

  /* ---------- overview ---------- */
  var tierSpec = [
    { cls: "card good",   label: "Auto, deterministic",      n: D.tiers.auto,       money: D.tiers.auto_value,       how: "Reference, amount and window arithmetic only" },
    { cls: "card accent", label: "Model-assisted, verified", n: D.tiers.assisted,   money: D.tiers.assisted_value,   how: "Model proposed; the deterministic gate re-derived and accepted it" },
    { cls: "card warn",   label: "Unresolved, raised",       n: D.tiers.unresolved, money: D.tiers.unresolved_value, how: "Surfaced to the exception ledger with a reason code" }
  ];
  var tierCards = document.getElementById("tierCards");
  tierSpec.forEach(function (spec) {
    var card = el("div", { "class": spec.cls });
    card.appendChild(el("div", { "class": "label" }, spec.label));
    card.appendChild(el("div", { "class": "big" }, spec.n));
    card.appendChild(el("div", { "class": "money" }, spec.money));
    card.appendChild(el("div", { "class": "how" }, spec.how));
    tierCards.appendChild(card);
  });
  document.getElementById("rateLine").textContent =
    "Combined match rate against ground truth: " + D.tiers.combined_match_rate +
    "% of " + D.tiers.correspondences + " correspondences that actually exist.";

  var qualityBody = document.getElementById("qualityBody");
  D.quality.forEach(function (row) {
    var tr = el("tr");
    tr.appendChild(el("th", { scope: "row" }, row.label));
    [row.expected, row.correct, row.false_positives, row.missed].forEach(function (v) {
      tr.appendChild(el("td", { "class": "num" }, v));
    });
    var p = el("td", { "class": "num" });
    p.appendChild(el("span", { "class": "pill " + (row.precision >= 100 ? "good" : "bad") }, row.precision + "%"));
    tr.appendChild(p);
    var r = el("td", { "class": "num" });
    r.appendChild(el("span", { "class": "pill " + (row.recall >= 100 ? "good" : "warn") }, row.recall + "%"));
    tr.appendChild(r);
    qualityBody.appendChild(tr);
  });

  var fps = D.false_positive_detail.settlement_to_bank.concat(D.false_positive_detail.payment_to_order);
  document.getElementById("fpLine").textContent = fps.length
    ? "False positives: " + fps.map(function (p) { return p.join(" -> "); }).join(", ")
    : "No false positives in any correspondence.";

  var moneyBody = document.getElementById("moneyBody");
  Object.keys(D.money).forEach(function (key) {
    var tr = el("tr");
    tr.appendChild(el("th", { scope: "row" }, key.replace(/_/g, " ")));
    tr.appendChild(el("td", { "class": "num" }, D.money[key]));
    moneyBody.appendChild(tr);
  });

  var injectedBody = document.getElementById("injectedBody");
  Object.keys(D.injected).forEach(function (key) {
    var tr = el("tr");
    tr.appendChild(el("th", { scope: "row" }, key.replace(/_/g, " ")));
    tr.appendChild(el("td", { "class": "num" }, D.injected[key]));
    injectedBody.appendChild(tr);
  });

  /* ---------- record detail rendering ---------- */
  function idButton(id) {
    var button = el("button", { "class": "rowbtn", type: "button" }, id);
    button.addEventListener("click", function () { showRecord(id, "excDetail"); });
    return button;
  }

  function renderRecord(id) {
    var rec = D.records[id];
    var box = el("div", { "class": "detail" });
    if (!rec) {
      box.appendChild(el("p", null, id + " is not a record in this batch."));
      return box;
    }
    var head = el("h3", null, rec.type.replace(/_/g, " ") + " " + rec.id);
    box.appendChild(head);

    if (rec.type === "settlement") {
      var ledger = el("div", { "class": "ledger" });
      rec.breakdown.forEach(function (line) {
        var row = el("div");
        row.appendChild(el("span", null, line.label));
        row.appendChild(el("span", null, line.value));
        ledger.appendChild(row);
      });
      var total = el("div", { "class": "total" });
      total.appendChild(el("span", null, "Recomputed net from line items"));
      total.appendChild(el("span", null, rec.recomputed_net));
      ledger.appendChild(total);
      var reported = el("div");
      reported.appendChild(el("span", null, "Gateway reported net"));
      reported.appendChild(el("span", null, rec.reported_net));
      ledger.appendChild(reported);
      box.appendChild(ledger);

      var verdict = el("p");
      verdict.appendChild(el("span",
        { "class": "pill " + (rec.header_agrees ? "good" : "bad") },
        rec.header_agrees ? "header agrees with line items"
                          : "header disagrees by " + rec.difference));
      box.appendChild(verdict);
    }

    var dl = el("dl", { "class": "kv" });
    function pair(label, value) {
      if (value === null || value === undefined || value === "" ) { return; }
      dl.appendChild(el("dt", null, label));
      var dd = el("dd");
      if (typeof value === "string" && D.records[value]) { dd.appendChild(idButton(value)); }
      else if (Array.isArray(value)) {
        var list = el("span", { "class": "idlist" });
        value.forEach(function (v) {
          if (D.records[v]) { list.appendChild(idButton(v)); }
          else { list.appendChild(el("span", null, v)); }
        });
        dd.appendChild(list);
      } else { dd.textContent = String(value); }
      dl.appendChild(dd);
    }

    if (rec.type === "settlement") {
      pair("Settled on", rec.settled_on);
      pair("Kind", rec.kind);
      pair("Gateway reference", rec.reference);
      pair("Matched bank credit", rec.matched_credit);
      pair("Matched by pass", rec.match_pass);
      pair("Payments", rec.line_ids.payments);
      pair("Refunds", rec.line_ids.refunds);
      pair("Chargebacks", rec.line_ids.chargebacks);
      pair("Adjustments", rec.line_ids.adjustments);
    } else if (rec.type === "bank_txn") {
      pair("Value date", rec.value_date);
      pair("Direction", rec.direction);
      pair("Amount", rec.amount);
      pair("Narration", rec.narration);
      pair("Reference parsed from narration", rec.parsed_reference || "none found");
      pair(rec.is_sweep ? "Sweep covering settlements" : "Explains settlement", rec.explains);
      pair("Matched by pass", rec.match_pass);
    } else if (rec.type === "payment") {
      pair("Captured at", rec.captured_at);
      pair("Method", rec.method);
      pair("Gross", rec.gross);
      pair("Fee", rec.fee);
      pair("GST on fee", rec.gst);
      pair("Net to merchant", rec.net);
      pair("Order reference on payment", rec.order_reference || "none");
      pair("Matched order", rec.matched_order);
      pair("In settlement", rec.settlement);
    } else if (rec.type === "order") {
      pair("Created at", rec.created_at);
      pair("Amount", rec.amount);
      pair("Status in merchant ledger", rec.status);
      pair("Customer", rec.customer);
      pair("Merchant reference", rec.merchant_ref);
    } else {
      Object.keys(rec).forEach(function (key) {
        if (["type", "id", "findings", "amount_paise"].indexOf(key) === -1) {
          pair(key.replace(/_/g, " "), rec[key]);
        }
      });
    }
    box.appendChild(dl);

    (rec.findings || []).forEach(function (finding) {
      var note = el("div", { "class": "finding" });
      note.appendChild(el("div", { "class": "code" }, finding.reason_code));
      note.appendChild(el("div", null, finding.reason));
      if (finding.suggested_action) {
        note.appendChild(el("div", { "class": "action" }, "Next: " + finding.suggested_action));
      }
      box.appendChild(note);
    });
    return box;
  }

  function showRecord(id, targetId) {
    var target = document.getElementById(targetId);
    clear(target);
    target.appendChild(renderRecord(id));
    target.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /* ---------- exception ledger ---------- */
  var activeReason = null;
  var reasons = {};
  D.exceptions.forEach(function (e) { reasons[e.reason_code] = (reasons[e.reason_code] || 0) + 1; });

  var filterBox = document.getElementById("reasonFilters");
  function makeFilter(code, count) {
    var button = el("button", { type: "button", "aria-pressed": code === activeReason ? "true" : "false" },
                    (code || "All") + " (" + count + ")");
    button.addEventListener("click", function () {
      activeReason = (activeReason === code) ? null : code;
      Array.prototype.forEach.call(filterBox.children, function (child) {
        child.setAttribute("aria-pressed", "false");
      });
      if (activeReason !== null) { button.setAttribute("aria-pressed", "true"); }
      else { filterBox.firstChild.setAttribute("aria-pressed", "true"); }
      drawExceptions();
    });
    return button;
  }
  filterBox.appendChild(makeFilter(null, D.exceptions.length));
  filterBox.firstChild.setAttribute("aria-pressed", "true");
  Object.keys(reasons).sort().forEach(function (code) {
    filterBox.appendChild(makeFilter(code, reasons[code]));
  });

  var excBody = document.getElementById("excBody");
  var excSearch = document.getElementById("excSearch");
  function drawExceptions() {
    var query = excSearch.value.trim().toLowerCase();
    clear(excBody);
    var shown = 0, total = 0;
    D.exceptions.forEach(function (e) {
      if (activeReason && e.reason_code !== activeReason) { return; }
      var haystack = (e.subject_id + " " + e.reason_code + " " + e.reason + " " + e.suggested_action).toLowerCase();
      if (query && haystack.indexOf(query) === -1) { return; }
      shown += 1;
      total += e.amount_paise;
      var tr = el("tr");
      var th = el("th", { scope: "row" });
      th.appendChild(idButton(e.subject_id));
      tr.appendChild(th);
      var reason = el("td");
      reason.appendChild(el("span", { "class": "pill flat" }, e.reason_code));
      tr.appendChild(reason);
      tr.appendChild(el("td", { "class": "num" }, e.amount));
      tr.appendChild(el("td", null, e.reason));
      tr.appendChild(el("td", null, e.suggested_action));
      excBody.appendChild(tr);
    });
    document.getElementById("excCount").textContent =
      shown + " of " + D.exceptions.length + " findings shown";
  }
  excSearch.addEventListener("input", drawExceptions);
  drawExceptions();

  /* ---------- passes ---------- */
  var passBody = document.getElementById("passBody");
  D.passes.forEach(function (p) {
    var tr = el("tr");
    tr.appendChild(el("th", { scope: "row" }, p.name.replace(/_/g, " ")));
    [p.considered, p.accepted].forEach(function (v) { tr.appendChild(el("td", { "class": "num" }, v)); });
    var rej = el("td", { "class": "num" });
    if (p.rejected) { rej.appendChild(el("span", { "class": "pill bad" }, p.rejected)); }
    else { rej.textContent = "0"; }
    tr.appendChild(rej);
    var dec = el("td", { "class": "num" });
    if (p.declined) { dec.appendChild(el("span", { "class": "pill warn" }, p.declined)); }
    else { dec.textContent = "0"; }
    tr.appendChild(dec);
    tr.appendChild(el("td", { "class": "num" }, p.ms));
    passBody.appendChild(tr);
  });

  document.getElementById("gateIntro").textContent = D.gate_rejections.length
    ? D.gate_rejections.length + " proposals looked correct to the pass that produced them and did not survive re-derivation from the ledger. Each one would otherwise have been a false positive."
    : "No proposals were rejected in this run.";
  var gateList = document.getElementById("gateList");
  D.gate_rejections.forEach(function (r) {
    var box = el("div", { "class": "detail" });
    var head = el("h3");
    head.appendChild(idButton(r.subject));
    head.appendChild(document.createTextNode(" rejected in " + r.pass.replace(/_/g, " ")));
    box.appendChild(head);
    box.appendChild(el("p", { "class": "mono" }, r.failure));
    gateList.appendChild(box);
  });

  /* ---------- record lookup ---------- */
  var recSearch = document.getElementById("recSearch");
  var recResults = document.getElementById("recResults");
  document.getElementById("recHint").textContent =
    Object.keys(D.records).length + " records available";
  recSearch.addEventListener("input", function () {
    var query = recSearch.value.trim().toLowerCase();
    clear(recResults);
    if (query.length < 3) { return; }
    var hits = Object.keys(D.records).filter(function (id) { return id.indexOf(query) !== -1; }).slice(0, 6);
    if (!hits.length) {
      recResults.appendChild(el("p", { "class": "sub" }, "No record matches that identifier."));
      return;
    }
    hits.forEach(function (id) { recResults.appendChild(renderRecord(id)); });
  });

  /* ---------- ask ---------- */
  document.getElementById(LIVE ? "askLive" : "askOffline").hidden = false;
  if (LIVE) {
    var chatLog = document.getElementById("chatLog");
    var chatForm = document.getElementById("chatForm");
    var chatInput = document.getElementById("chatInput");
    var chatSend = document.getElementById("chatSend");

    function addMessage(cls, text, meta) {
      var msg = el("div", { "class": "msg " + cls }, text);
      if (meta) { msg.appendChild(el("div", { "class": "meta" }, meta)); }
      chatLog.appendChild(msg);
      chatLog.scrollTop = chatLog.scrollHeight;
      return msg;
    }

    var examples = [
      "How did the reconciliation run go overall?",
      "What is the breakdown of setl_00016?",
      "Which proposals did the verification gate reject?",
      "Which bank credits are still unmatched?",
      "How much money is sitting in unresolved exceptions?"
    ];
    var suggestions = document.getElementById("suggestions");
    examples.forEach(function (text) {
      var button = el("button", { type: "button" }, text);
      button.addEventListener("click", function () { chatInput.value = text; chatForm.requestSubmit(); });
      suggestions.appendChild(button);
    });

    chatForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var question = chatInput.value.trim();
      if (!question) { return; }
      addMessage("you", question);
      chatInput.value = "";
      chatSend.disabled = true;
      var pending = addMessage("bot", "Calling tools...");

      fetch("/api/ask", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: question })
      }).then(function (response) { return response.json(); }).then(function (data) {
        pending.textContent = data.text || "(no answer)";
        var bits = [
          "backend " + data.backend,
          data.steps + " tool call(s)" + (data.tools && data.tools.length ? " [" + data.tools.join(", ") + "]" : ""),
          Math.round(data.latency_ms) + "ms",
          data.grounded ? "all identifiers traced to tool output"
                        : "UNGROUNDED: " + (data.ungrounded || []).join(", ")
        ];
        if (data.cited && data.cited.length) { bits.push("cited " + data.cited.join(", ")); }
        pending.appendChild(el("div", { "class": "meta" }, bits.join(" | ")));
        chatLog.scrollTop = chatLog.scrollHeight;
      }).catch(function (error) {
        pending.textContent = "Request failed: " + error;
      }).then(function () {
        chatSend.disabled = false;
        chatInput.focus();
      });
    });
  }

  /* ---------- integrity ---------- */
  var auditCards = document.getElementById("auditCards");
  [
    { label: "Audit events", value: D.audit.events, how: "Append-only, hash chained", cls: "card" },
    { label: "Hash chain", value: D.audit.chain_intact ? "intact" : "BROKEN", how: "Each event hashes its own content plus the previous hash", cls: D.audit.chain_intact ? "card good" : "card warn" },
    { label: "Replay", value: D.audit.replay_ok ? "reproduces state" : "MISMATCH", how: "Final state rebuilt from events alone, compared to live state", cls: D.audit.replay_ok ? "card good" : "card warn" },
    { label: "Coverage holes", value: D.audit.coverage_holes, how: "Records neither matched, suppressed, nor raised", cls: D.audit.coverage_holes ? "card warn" : "card good" }
  ].forEach(function (spec) {
    var card = el("div", { "class": spec.cls });
    card.appendChild(el("div", { "class": "label" }, spec.label));
    card.appendChild(el("div", { "class": "big" }, spec.value));
    card.appendChild(el("div", { "class": "how" }, spec.how));
    auditCards.appendChild(card);
  });

  var tolBody = document.getElementById("tolBody");
  var tolLabels = {
    amount_paise: "Amount tolerance (paise)",
    settlement_lag_days: "Settlement lag window (days)",
    max_sweep_size: "Max sweep size",
    subset_sum_node_budget: "Subset-sum node budget",
    match_payments_to_inactive_orders: "Match payments to inactive orders"
  };
  Object.keys(D.tolerance).forEach(function (key) {
    var tr = el("tr");
    tr.appendChild(el("th", { scope: "row" }, tolLabels[key] || key));
    tr.appendChild(el("td", { "class": "num" }, String(D.tolerance[key])));
    tolBody.appendChild(tr);
  });

  var costBody = document.getElementById("costBody");
  var costRows = [
    ["Residue backend", D.cost.backend],
    ["Items classified", D.cost.items],
    ["Input tokens", D.cost.input_tokens],
    ["Output tokens", D.cost.output_tokens],
    ["Mean latency per item", D.cost.mean_latency_ms + " ms"],
    ["Estimated cost", "$" + D.cost.usd]
  ];
  costRows.forEach(function (row) {
    var tr = el("tr");
    tr.appendChild(el("th", { scope: "row" }, row[0]));
    tr.appendChild(el("td", { "class": "num" }, String(row[1])));
    costBody.appendChild(tr);
  });
})();
</script>
</body>
</html>
"""


def render_html(result: PipelineResult, *, live: bool = False) -> str:
    """Render the whole report as one self-contained HTML document.

    ``live`` enables the question box, which needs a server behind it. The static
    file written by ``recon.cli`` sets it false and shows instructions instead, so
    the page never presents a control that cannot work.
    """
    payload = json.dumps(_build_payload(result), default=str, separators=(",", ":"))
    # A literal "</script>" inside the JSON would terminate the containing script
    # element early. Escaping the slash keeps the JSON valid and the element intact.
    payload = payload.replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__PAYLOAD__", payload)
        .replace("__LIVE__", "true" if live else "false")
        .replace("__SEED__", str(result.report.seed))
    )
