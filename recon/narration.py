"""Bank narration parsing.

A bank statement does not hand you a UTR in a column. It hands you a free-text
narration string whose format depends on the bank, the transfer rail, and
occasionally the decade the branch software was written in. Extracting a
reference from it is a genuine parsing problem, and it is the first place a
reconciliation pipeline silently loses accuracy.

This module is deliberately separate from the matching passes so that the
extraction can be tested on its own, and so that the pipeline treats the result
as what it is: a *hint* that is frequently absent and occasionally wrong.
"""

from __future__ import annotations

import re

# Reference formats keyed by rail. Tried in order, most specific first, because a
# labelled reference is far more trustworthy than a token that merely looks like
# one.
_LABELLED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bUTR[:\s#-]*([A-Z0-9]{10,22})\b"),
    re.compile(r"\bREF[:\s#-]*([A-Z0-9]{10,22})\b"),
    re.compile(r"\bIMPS[/\s-]+([A-Z0-9]{10,22})[/\s-]"),
    re.compile(r"\bRTGS\s+(?:CR\s+)?([A-Z0-9]{10,22})\b"),
    re.compile(r"\bNEFT\s+(?:CR[\s-]+)?([A-Z]{3}[0-9]{8,18})\b"),
    re.compile(r"\bUPI[/\s-]+([A-Z0-9]{10,22})[/\s-]"),
)

# An IFSC code looks enough like a reference to fool a naive token scan:
# four letters, a literal zero, then six alphanumerics. Bank narrations are full
# of them because they name the remitting branch. Matching one as a UTR produces
# a reference that is stable across many unrelated credits, which is the worst
# possible failure mode here: it would make several distinct settlements look
# like they share a reference.
_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

# Fallback: a reference-shaped token. A short alphabetic prefix followed by a
# long digit run. Pure-digit tokens are excluded because bank account numbers
# and card BINs match that shape and are not references.
_TOKEN = re.compile(r"\b([A-Z]{2,6}[0-9]{8,18})\b")

# Tokens that match the shapes above but are never references.
_STOPWORDS = frozenset(
    {
        "RAZORPAY",
        "SETTLEMENT",
        "TRANSFER",
        "PAYMENT",
        "CREDIT",
        "SOFTWARE",
    }
)


def extract_utr(narration: str) -> str | None:
    """Best-effort extraction of a settlement reference from a bank narration.

    Returns ``None`` when nothing reference-shaped is present, which is a normal
    and frequent outcome. Callers must treat ``None`` as "fall through to the
    amount and date passes", never as "no match exists".
    """
    if not narration:
        return None
    text = narration.upper()

    for pattern in _LABELLED_PATTERNS:
        match = pattern.search(text)
        if match:
            candidate = match.group(1)
            if _is_plausible(candidate):
                return candidate

    # Unlabelled fallback. Only accepted when exactly one reference-shaped token
    # is present; two or more means the narration is ambiguous and guessing which
    # one is the reference would be worse than declining.
    candidates = [tok for tok in _TOKEN.findall(text) if _is_plausible(tok)]
    unique = sorted(set(candidates))
    if len(unique) == 1:
        return unique[0]
    return None


def _is_plausible(token: str) -> bool:
    """Reject tokens that are shaped like a reference but are known not to be."""
    if token in _STOPWORDS:
        return False
    if _IFSC.match(token):
        return False
    if not any(ch.isdigit() for ch in token):
        return False
    return True


def looks_like_settlement_credit(narration: str) -> bool:
    """Whether a credit plausibly originates from the payment gateway.

    Used only to explain an unattributed credit in the exception ledger. It never
    gates a match: a credit that fails this check but reconciles cleanly on
    reference and amount is still a match, because narration text is the least
    reliable field on the statement.
    """
    text = narration.upper()
    return any(marker in text for marker in ("RAZORPAY", "SETTLEMENT", "RZP"))
