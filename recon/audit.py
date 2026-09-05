"""Append-only, hash-chained audit trail.

Three properties make this an audit trail rather than a log file.

**Append-only.** Events are never edited or removed. A correction is a new event
that supersedes an earlier one, so the history of what the pipeline believed at
each point survives.

**Hash-chained.** Each event carries the hash of the previous event plus its own
canonical content. Altering any historical event invalidates every hash after it,
so ``verify_chain`` can detect tampering or accidental truncation. This is cheap
to compute and it converts "trust the log" into "check the log".

**Replayable.** The log is complete enough to reconstruct the final set of
matches and exceptions from the events alone. ``replay`` does exactly that, and
the pipeline asserts the reconstruction equals its live state. An audit trail that
cannot reproduce the outcome it describes is decoration, and this assertion is
what stops it quietly becoming decoration as the pipeline changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

GENESIS_HASH = "0" * 64


class Action:
    """Vocabulary of audited actions.

    A closed vocabulary rather than free text, because the replay logic switches
    on these and a typo in an action name would silently drop an event from the
    reconstruction.
    """

    MATCH_ACCEPTED = "match_accepted"
    MATCH_REJECTED = "match_rejected"
    EXCEPTION_RAISED = "exception_raised"
    CREDIT_SUPPRESSED = "credit_suppressed"
    PASS_COMPLETED = "pass_completed"
    CLASSIFIER_PROPOSED = "classifier_proposed"
    CLASSIFIER_ABSTAINED = "classifier_abstained"
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: str
    actor: str
    action: str
    subject: str
    payload: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    event_hash: str = ""

    def canonical_body(self) -> str:
        """Deterministic serialisation of everything the hash covers.

        ``sort_keys`` and a fixed separator matter: dict ordering or whitespace
        differences would produce a different hash for identical content, which
        would make the chain fail verification on a round trip through JSON.
        """
        body = {
            "seq": self.seq,
            "at": self.at,
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_body().encode("utf-8")).hexdigest()


@dataclass
class ReplayState:
    """Final state reconstructed from the event stream alone."""

    accepted_matches: set[tuple] = field(default_factory=set)
    raised_exceptions: set[tuple[str, str]] = field(default_factory=set)
    suppressed_credits: set[str] = field(default_factory=set)
    rejected_proposals: int = 0


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    # -- writing -----------------------------------------------------------

    def record(
        self,
        actor: str,
        action: str,
        subject: str,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        prev_hash = self._events[-1].event_hash if self._events else GENESIS_HASH
        event = AuditEvent(
            seq=len(self._events) + 1,
            at=datetime.now(timezone.utc).isoformat(),
            actor=actor,
            action=action,
            subject=subject,
            payload=payload or {},
            prev_hash=prev_hash,
        )
        # ``event_hash`` is excluded from the hashed body, so it is filled in after
        # construction. dataclasses.replace keeps the record frozen everywhere else.
        event = AuditEvent(**{**asdict(event), "event_hash": event.compute_hash()})
        self._events.append(event)
        return event

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def write_jsonl(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in self._events:
                handle.write(
                    json.dumps(asdict(event), sort_keys=True, default=str) + "\n"
                )
        return path

    # -- reading and checking ---------------------------------------------

    @staticmethod
    def load_jsonl(path: str | Path) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(AuditEvent(**json.loads(line)))
        return events

    @staticmethod
    def verify_chain(events: Iterable[AuditEvent]) -> tuple[bool, str | None]:
        """Confirm sequence numbers and the hash chain are intact."""
        prev_hash = GENESIS_HASH
        expected_seq = 1
        for event in events:
            if event.seq != expected_seq:
                return False, f"sequence gap at {event.seq}, expected {expected_seq}"
            if event.prev_hash != prev_hash:
                return False, f"broken chain at seq {event.seq}: prev_hash mismatch"
            if event.event_hash != event.compute_hash():
                return False, f"content altered at seq {event.seq}: hash mismatch"
            prev_hash = event.event_hash
            expected_seq += 1
        return True, None

    @staticmethod
    def replay(events: Iterable[AuditEvent]) -> ReplayState:
        """Rebuild the outcome from the events, ignoring all live state."""
        state = ReplayState()
        for event in events:
            if event.action == Action.MATCH_ACCEPTED:
                state.accepted_matches.add(
                    (
                        event.payload["kind"],
                        event.payload["left_id"],
                        tuple(event.payload["right_ids"]),
                    )
                )
            elif event.action == Action.MATCH_REJECTED:
                state.rejected_proposals += 1
            elif event.action == Action.EXCEPTION_RAISED:
                state.raised_exceptions.add((event.subject, event.payload["reason_code"]))
            elif event.action == Action.CREDIT_SUPPRESSED:
                state.suppressed_credits.add(event.subject)
        return state
