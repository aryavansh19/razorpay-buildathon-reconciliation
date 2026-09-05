"""Three-way payment reconciliation with measured, verifiable output.

Built for the Razorpay AI Buildathon, Track 04 (AI Finance Controller).

Architecture in one line: deterministic code proposes and disposes of the bulk,
a language model only classifies the residue, and every model proposal is
re-verified by the same deterministic gate before it is allowed to count as a
match.

All data in this project is synthetic and generated locally. No real payment
data, customer identifier or credential is used, stored or transmitted.
"""

__version__ = "0.1.0"
