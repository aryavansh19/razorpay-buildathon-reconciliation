"""Integer money arithmetic.

Every function here operates on and returns ``int`` paise. Nothing in this
module accepts or produces a float, which is what makes the netting identity in
``Ledger.computed_net_paise`` an exact equality rather than an approximate one.
"""

from __future__ import annotations


def bps(amount_paise: int, basis_points: int) -> int:
    """Apply a rate expressed in basis points, rounding half away from zero.

    ``bps(199_00, 200)`` is 2% of INR 199.00, which is 398 paise.

    Rounding half *away from zero* rather than Python's default half-to-even is
    deliberate: it is what payment gateways and tax engines do, so matching it
    keeps recomputed fees byte-identical to the ones in the settlement report.
    Half-to-even would leave a one-paise residue on roughly half of all
    exact-half cases, and a one-paise residue on a fee line is indistinguishable
    from a genuine break.
    """
    if amount_paise < 0:
        return -bps(-amount_paise, basis_points)
    return (amount_paise * basis_points + 5_000) // 10_000


# GST on payment gateway fees, 18 percent.
GST_BPS = 1_800

# Merchant discount rate by instrument, in basis points on the gross amount.
# UPI carries zero MDR for merchants in India, which is why a majority of the
# generated settlements have clean zero-fee payment lines. That is realistic and
# it matters: a reconciler that only works when a fee is present is a reconciler
# that fails on most Indian volume.
MDR_BPS: dict[str, int] = {
    "upi": 0,
    "card": 200,
    "netbanking": 190,
    "wallet": 210,
    "emi": 250,
}

# Instant settlement carries an extra charge on the settled amount.
INSTANT_SETTLEMENT_BPS = 12

# Flat fee a bank levies when a customer raises a dispute, in paise.
DISPUTE_FEE_PAISE = 150_000


def fee_and_tax(gross_paise: int, method: str) -> tuple[int, int]:
    """Return ``(fee_paise, tax_paise)`` for a payment.

    The tax is charged on the fee, not on the gross. Getting that wrong inflates
    the recomputed net by roughly the GST on the whole transaction, which is
    large enough to break every match and small enough to look like a plausible
    tolerance problem.
    """
    fee = bps(gross_paise, MDR_BPS.get(method, 200))
    tax = bps(fee, GST_BPS)
    return fee, tax
