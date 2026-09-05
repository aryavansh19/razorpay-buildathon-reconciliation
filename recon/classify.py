"""Residue classification.

Only records the deterministic ladder could not resolve reach this module, and
nothing here can create a match. A classifier returns a *proposal*, which is then
re-derived by ``verify.Verifier`` against the ledger. If the arithmetic does not
close, the proposal is rejected regardless of how confident the classifier was.

Two interchangeable backends
----------------------------
``HeuristicClassifier``
    Offline, deterministic, no credentials. Reads a batch reference out of a
    narration with a regex and otherwise triages by shape.

``LLMClassifier``
    Calls a hosted model over plain ``urllib``, so the project keeps zero
    third-party dependencies. Falls back to the heuristic on any error, including
    a missing key, an unknown model name, or unparseable output.

Both are run over the same residue by ``evals.py``. That comparison is the honest
answer to the obvious question about a project like this: the offline backend is a
real baseline, not a stub, and where the model does not beat it the report says so.
A regex that reads ``BATCH SETL00023`` handles the narration shapes it was written
for and nothing else; the value of the model is the shapes nobody enumerated in
advance. Publishing both numbers is the only way to show which is doing the work.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from .models import MatchKind, ReasonCode

# Declared prices per million tokens. These are configuration, not measurements:
# the pipeline multiplies reported token counts by these figures to produce the
# cost line in the report. They are stated openly so the cost number can be
# recomputed or corrected rather than taken on trust.
USD_PER_MILLION_INPUT_TOKENS = float(os.environ.get("RECON_USD_PER_MTOK_IN", "3.00"))
USD_PER_MILLION_OUTPUT_TOKENS = float(os.environ.get("RECON_USD_PER_MTOK_OUT", "15.00"))

DEFAULT_MODEL = os.environ.get("RECON_LLM_MODEL", "claude-sonnet-4-5")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


# ---------------------------------------------------------------------------
# Data passed to and from a classifier
# ---------------------------------------------------------------------------


@dataclass
class ResidueItem:
    """One unresolved record, with everything a classifier may consider.

    ``candidates`` is pre-filtered by the pipeline to records that are still open
    and inside the plausible window. A classifier can only ever choose from this
    list, which means it cannot invent an identifier, and a hallucinated id fails
    the ``ids_exist`` check in the verifier anyway.
    """

    kind: MatchKind
    subject_id: str
    amount_paise: int
    description: str
    candidates: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=dict)


@dataclass
class Proposal:
    """A classifier's suggestion. Advisory only."""

    action: str  # "match" | "exception" | "abstain"
    right_ids: tuple[str, ...] = ()
    reason_code: ReasonCode | None = None
    rationale: str = ""
    suggested_action: str = ""
    confidence: float = 0.0
    backend: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def is_match(self) -> bool:
        return self.action == "match" and bool(self.right_ids)


@dataclass
class ClassifierUsage:
    """Aggregate cost and latency, reported per run."""

    backend: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_latency_ms: float = 0.0
    failures: int = 0
    fallbacks: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * USD_PER_MILLION_INPUT_TOKENS
            + self.output_tokens / 1_000_000 * USD_PER_MILLION_OUTPUT_TOKENS
        )

    @property
    def mean_latency_ms(self) -> float:
        return self.total_latency_ms / self.calls if self.calls else 0.0

    def record(self, proposal: Proposal) -> None:
        self.calls += 1
        self.input_tokens += proposal.input_tokens
        self.output_tokens += proposal.output_tokens
        self.total_latency_ms += proposal.latency_ms


class Classifier(Protocol):
    name: str
    usage: ClassifierUsage

    def classify(self, item: ResidueItem) -> Proposal: ...


# ---------------------------------------------------------------------------
# Offline backend
# ---------------------------------------------------------------------------

# Matches a batch reference written in prose: "BATCH SETL00023", "BATCH NO
# SETL00023", "SETL 00023". Deliberately narrow, because the point of this
# baseline is to be predictable, not clever.
_BATCH_REFERENCE = re.compile(r"\bSETL[\s#_-]?0*(\d{1,6})\b", re.IGNORECASE)

_GATEWAY_MARKERS = ("RAZORPAY", "RZP", "SETTLEMENT", "PAYOUT")


class HeuristicClassifier:
    """Deterministic triage. The baseline the model has to beat."""

    name = "heuristic"

    def __init__(self) -> None:
        self.usage = ClassifierUsage(backend=self.name)

    def classify(self, item: ResidueItem) -> Proposal:
        started = time.perf_counter()
        proposal = self._classify(item)
        proposal.backend = self.name
        proposal.latency_ms = (time.perf_counter() - started) * 1000
        self.usage.record(proposal)
        return proposal

    def _classify(self, item: ResidueItem) -> Proposal:
        if item.kind is MatchKind.PAYMENT_ORDER:
            return self._classify_payment(item)
        if item.context.get("subject_type") == "settlement":
            return self._classify_settlement(item)
        return self._classify_credit(item)

    def _classify_credit(self, item: ResidueItem) -> Proposal:
        narration = item.context.get("narration", "")

        match = _BATCH_REFERENCE.search(narration)
        if match:
            wanted = f"setl_{int(match.group(1)):05d}"
            for candidate in item.candidates:
                if candidate["id"] == wanted:
                    return Proposal(
                        action="match",
                        right_ids=(wanted,),
                        confidence=0.93,
                        rationale=(
                            f"Narration names batch {match.group(0).strip()}, which "
                            f"resolves to {wanted}. That settlement is among the open "
                            f"candidates and its recomputed net equals this credit."
                        ),
                    )

        if not any(marker in narration.upper() for marker in _GATEWAY_MARKERS):
            return Proposal(
                action="exception",
                reason_code=ReasonCode.UNATTRIBUTED_BANK_CREDIT,
                confidence=0.88,
                rationale=(
                    "Narration carries no gateway marker, so this credit most "
                    "likely originates outside the payment gateway."
                ),
                suggested_action=(
                    "Route to the receivables owner to identify the remitter. Do not "
                    "treat as gateway revenue."
                ),
            )

        return Proposal(
            action="exception",
            reason_code=ReasonCode.UNATTRIBUTED_BANK_CREDIT,
            confidence=0.55,
            rationale=(
                "Narration looks gateway-related but no open settlement explains "
                "the amount within tolerance and window."
            ),
            suggested_action=(
                "Request the settlement breakdown for this reference from the gateway."
            ),
        )

    def _classify_settlement(self, item: ResidueItem) -> Proposal:
        carried_forward = item.amount_paise <= 0
        return Proposal(
            action="exception",
            reason_code=ReasonCode.NO_BANK_CREDIT,
            confidence=0.9,
            rationale=(
                "Settlement net is zero or negative, so no payout was due and the "
                "deficit carries into the next cycle."
                if carried_forward
                else "No bank credit in the statement explains this settlement "
                "within the tolerance and lag window."
            ),
            suggested_action=(
                "No action; confirm the carry-forward appears in the next cycle."
                if carried_forward
                else "Check whether the payout is in transit past the statement "
                "cut-off before escalating to the gateway."
            ),
        )

    def _classify_payment(self, item: ResidueItem) -> Proposal:
        return Proposal(
            action="exception",
            reason_code=ReasonCode.ORPHAN_PAYMENT,
            confidence=0.85,
            rationale=(
                "Payment carries no order reference and no open order matches its "
                "amount inside the capture window."
            ),
            suggested_action=(
                "Trace the checkout session. A payment with no order usually means "
                "the order write failed after the payment succeeded."
            ),
        )


# ---------------------------------------------------------------------------
# Hosted model backend
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a reconciliation analyst working on a payment settlement break.

You will be given one unresolved record and a list of candidate records it might
correspond to. Deterministic matching has already failed on this record, usually
because two or more candidates are equally consistent on amount and date.

Your job is to decide, using every field including narration text, whether one
candidate is the correct correspondence.

Rules you must follow:
- You may only choose candidate identifiers that appear in the provided list.
- Prefer abstaining over guessing. A wrong match silently closes a break that a
  human would otherwise investigate, which is worse than leaving it open.
- You do not approve or move money. Your proposal is independently re-verified
  against the ledger arithmetic and will be rejected if the amounts do not close.

Reply with a single JSON object and no other text:
{"action": "match" | "exception" | "abstain",
 "right_ids": ["<candidate id>"],
 "reason_code": "<one of NO_BANK_CREDIT, UNATTRIBUTED_BANK_CREDIT, ORPHAN_PAYMENT, AMBIGUOUS_CANDIDATES, AMOUNT_MISMATCH>",
 "rationale": "<one or two sentences citing the specific evidence>",
 "suggested_action": "<what a human should do next, if an exception>",
 "confidence": <0.0 to 1.0>}

Use "right_ids" only for action "match". Use "reason_code" only for action
"exception"."""


class LLMClassifier:
    """Hosted-model backend with a hard fallback to the offline baseline."""

    name = "llm"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        max_output_tokens: int = 600,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.timeout_s = timeout_s
        self.max_output_tokens = max_output_tokens
        self.usage = ClassifierUsage(backend=f"{self.name}:{model}")
        self._fallback = HeuristicClassifier()
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def classify(self, item: ResidueItem) -> Proposal:
        if not self.available:
            proposal = self._fallback.classify(item)
            proposal.backend = f"{self.name}:unavailable->heuristic"
            self.usage.fallbacks += 1
            return proposal

        started = time.perf_counter()
        try:
            raw, input_tokens, output_tokens = self._call(item)
            proposal = self._parse(raw, item)
            proposal.input_tokens = input_tokens
            proposal.output_tokens = output_tokens
            proposal.backend = self.usage.backend
        except Exception as exc:  # noqa: BLE001 - any failure must degrade, not crash
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.usage.failures += 1
            self.usage.fallbacks += 1
            proposal = self._fallback.classify(item)
            proposal.backend = f"{self.name}:error->heuristic"
            proposal.rationale = (
                f"{proposal.rationale} [model call failed: {self.last_error}]"
            )
            return proposal

        proposal.latency_ms = (time.perf_counter() - started) * 1000
        self.usage.record(proposal)
        return proposal

    def _call(self, item: ResidueItem) -> tuple[str, int, int]:
        payload = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": _SYSTEM_PROMPT,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "unresolved_record": {
                                "id": item.subject_id,
                                "type": item.context.get("subject_type", "bank_credit"),
                                "amount_paise": item.amount_paise,
                                "description": item.description,
                                **{
                                    key: value
                                    for key, value in item.context.items()
                                    if key != "subject_type"
                                },
                            },
                            "candidates": item.candidates,
                        },
                        indent=2,
                        default=str,
                    ),
                }
            ],
        }
        request = urllib.request.Request(
            ANTHROPIC_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key or "",
                "anthropic-version": ANTHROPIC_VERSION,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))

        text = "".join(
            block.get("text", "") for block in body.get("content", []) if isinstance(block, dict)
        )
        usage = body.get("usage", {})
        return text, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    def _parse(self, raw: str, item: ResidueItem) -> Proposal:
        """Extract the JSON object, tolerating surrounding prose or fences."""
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object in model output: {raw[:200]!r}")
        data = json.loads(raw[start : end + 1])

        action = str(data.get("action", "abstain")).lower()
        if action not in ("match", "exception", "abstain"):
            raise ValueError(f"unrecognised action {action!r}")

        right_ids: tuple[str, ...] = ()
        if action == "match":
            allowed = {candidate["id"] for candidate in item.candidates}
            proposed = [str(value) for value in data.get("right_ids") or []]
            invalid = [value for value in proposed if value not in allowed]
            if invalid or not proposed:
                # A proposal naming records outside the candidate list is discarded
                # here rather than passed on. The verifier would reject it too, but
                # failing early keeps the rejection attributable to the model.
                raise ValueError(f"proposed ids not in candidate list: {invalid or proposed}")
            right_ids = tuple(proposed)

        reason_code = None
        if action == "exception":
            try:
                reason_code = ReasonCode(str(data.get("reason_code", "")))
            except ValueError:
                reason_code = ReasonCode.CLASSIFIER_ABSTAINED

        confidence = data.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0

        return Proposal(
            action=action,
            right_ids=right_ids,
            reason_code=reason_code,
            rationale=str(data.get("rationale", ""))[:600],
            suggested_action=str(data.get("suggested_action", ""))[:400],
            confidence=confidence,
        )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def build_classifier(backend: str = "auto") -> Classifier:
    """Choose a backend.

    ``auto`` prefers the hosted model when a key is present and silently uses the
    offline baseline otherwise, so the pipeline runs end to end on a machine with
    no credentials. The report always names the backend that actually ran, because
    a match rate produced by a regex and one produced by a model are different
    claims and must not be reported identically.
    """
    backend = (backend or "auto").lower()
    if backend == "heuristic":
        return HeuristicClassifier()
    if backend == "llm":
        classifier = LLMClassifier()
        if not classifier.available:
            raise RuntimeError(
                "backend 'llm' requires ANTHROPIC_API_KEY. Use --classifier auto to "
                "fall back to the offline baseline."
            )
        return classifier
    if backend == "auto":
        classifier = LLMClassifier()
        return classifier if classifier.available else HeuristicClassifier()
    raise ValueError(f"unknown classifier backend {backend!r}")
