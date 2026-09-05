# Three-way settlement reconciliation

**Razorpay AI Buildathon 2026, Track 04 (AI Finance Controller).**

Reconciles a merchant's order ledger, a payment gateway settlement report, and a
bank statement against each other, then answers questions about the result.

Deterministic arithmetic resolves the bulk. A language model only ever looks at what
the arithmetic could not resolve, and every proposal it makes is re-derived from the
ledger before it is allowed to count. The same discipline applies to the Q&A agent:
it answers only through read-only tools, and every record it cites is checked back
against what those tools actually returned.

## Result

One batch of 1,165 records, seed `20260829`, no credentials required:

| Tier | Matches | Value | Decided by |
|---|---|---|---|
| Auto, deterministic | 481 | 58,77,413.51 | Reference, amount and window arithmetic |
| Model-assisted, verified | 5 | 2,81,746.83 | Model proposed, deterministic gate accepted |
| Unresolved, raised | 47 | 3,63,154.47 | Exception ledger, with a reason code each |

Scored against ground truth the generator recorded while building the data:

| Correspondence | Expected | Correct | False positives | Precision | Recall |
|---|---|---|---|---|---|
| Settlement to bank | 45 | 45 | 0 | 100.00% | 100.00% |
| Payment to order | 445 | 445 | 0 | 100.00% | 100.00% |
| Exception ledger, (record, reason) pairs | 47 | 47 | 0 | 100.00% | 100.00% |

Across **200 independently generated batches, 230,638 records**, precision and
recall stay at 100.00% (min, mean and max) with zero false positives, zero
coverage holes and zero audit failures. Throughput is 15,000 to 32,000 records per
second. Reproduce with `python -m recon.evals sweep --runs 200`.

The verification gate rejected 3 proposals in the single run and 446 across the
sweep. Every one of those would otherwise have been a false positive.

## Run it

Python 3.11 or newer. No dependencies to install, no API key, no network.

```bash
python -m recon.cli
```

That generates the dataset, reconciles it, scores the result, and writes:

```
data/                         the three sources as separate CSVs
data/ground_truth.json        the correct answer, which the reconciler never reads
reports/report.md             the full report
reports/report.html           the same report as a browsable dashboard
reports/report.json           the same figures, machine readable
reports/exception_ledger.csv  every unresolved item, with a suggested action
reports/audit.jsonl           hash-chained event log of every decision
```

`reports/report.html` is one self-contained file. No build step, no server, no
dependencies. Open it and you get the three numbers, the exception ledger as a
filterable and sortable table, click-through into any record, and for a settlement
the full netting arithmetic so the match can be checked by hand. A published copy
lives at `docs/index.html`.

For the dashboard with the question box live:

```bash
python -m recon.serve
```

Reconciles once, then serves on `localhost:8420` using `http.server` from the
standard library. Localhost only, read-only, synthetic data.

Or ask from a terminal:

```bash
python -m recon.ask "what is the breakdown of setl_00016?"
python -m recon.ask --show-tools "which bank credits are still unmatched?"
python -m recon.ask                      # interactive
```

Other entry points, via `make`, `.\run.ps1` on Windows, or directly:

```bash
python -m recon.evals sweep --runs 200   # does it generalise across seeds
python -m recon.evals policy             # measure one policy decision both ways
python -m recon.evals backends           # offline baseline vs hosted model
python -m recon.evals qa                 # grade the Q&A agent, 10 golden questions
python -m recon.cli --strict             # exit non-zero on any false positive
python -m recon.cli --publish            # refresh docs/index.html for GitHub Pages
python -m recon.demo                     # paced seven-beat walkthrough
```

`python -m recon.demo` is a scripted walkthrough for screen recording. It runs the
real pipeline, streams the real output, and paces itself so a viewer can read it.
`--fast` removes the pauses for rehearsal, `--beats 2,5,6` records a subset.
`tools\record_walkthrough.ps1` records it to a silent 1080p mp4 with ffmpeg. The shot
list, timings and narration script are in [VIDEO.md](VIDEO.md).

Setting `ANTHROPIC_API_KEY` switches the residue classifier and the Q&A agent to a
hosted model. Without it both use offline backends and everything still runs end to
end.

## Why this problem is not a join

A naive reconciler matches a bank credit to a payment amount. That fails
immediately, because one bank credit does not correspond to one payment. It
corresponds to a netting identity:

```
net = sum(payment.gross - fee - tax)
    - sum(refund.amount)
    - sum(chargeback.amount + chargeback.fee)
    + sum(adjustment.amount)
```

On top of that, the data carries the things that make this manual work in
practice. The generator injects all of them and reports how many of each appeared:

sweep credits that collapse several settlements into one line, settlement nets
that go negative and carry into the next cycle, refunds that slip a cycle, T+2
lags that stretch over weekends and bank holidays, sub-rupee rounding drift,
narrations with no reference, narrations whose reference was truncated by the
remitting bank, bank re-posts of a credit already posted, credits that are not
from the gateway at all, statement debits that must be ignored rather than
explained, payments with no order behind them, orders paid at a different amount
than written, instant settlements with their own charge, and gateway headers that
disagree with the gateway's own line items.

## Architecture

The claim is one sentence: **deterministic code proposes and disposes; the model
only ever proposes.**

```
                three sources (order ledger, settlement report, bank statement)
                                        |
                    +-------------------+-------------------+
                    |     deterministic pass ladder         |
                    |     strictest evidence first          |
                    +-------------------+-------------------+
                                        |
                        residue: what arithmetic cannot settle
                                        |
                    +-------------------+-------------------+
                    |  classifier: offline baseline or LLM  |
                    |  returns a PROPOSAL, never a match    |
                    +-------------------+-------------------+
                                        |
                    +-------------------+-------------------+
                    |   Verifier.accept  <-- single gate    |
                    |   re-derives from the ledger          |
                    +-------------------+-------------------+
                            |                       |
                        accepted                rejected
                    (tier: assisted)      (exception, with the reason)
```

`verify.Verifier.accept` is the only function in the codebase that can turn a
candidate into an accepted match. A deterministic pass and a language model reach
it by the same path and are checked identically: the records exist, neither side is
already consumed, the recomputed amounts close within a declared tolerance, the
dates are causally possible.

That is what makes the model safe to use here. It is good at reading
`TRANSFER RAZORPAY - PAYOUT AGAINST BATCH SETL00023 - NET OF FEES` and knowing
which settlement that is. It cannot guarantee the money adds up. So it never gets
to decide.

### The pass ladder

Strictest first, each later pass relaxing exactly one dimension. By the time a
looser pass runs, everything stricter evidence could explain is already consumed,
so relaxing a constraint can only add matches, never steal better ones.

| Pass | Considered | Accepted | Rejected by gate | Declined |
|---|---|---|---|---|
| 0a settlement net identity | 48 | 5 | 0 | 0 |
| 0 suppress duplicate credits | 53 | 3 | 0 | 0 |
| 1 reference and amount exact | 31 | 27 | 0 | 0 |
| 2 reference, amount within tolerance | 4 | 1 | 3 | 0 |
| 3 amount exact, in window | 6 | 2 | 0 | 4 |
| 4 amount within tolerance, in window | 7 | 2 | 0 | 5 |
| 5 sweep, bounded subset-sum | 14 | 4 | 0 | 0 |
| A payment to order, by reference | 445 | 445 | 0 | 0 |
| B payment to order, amount and window | 8 | 0 | 0 | 0 |

Pass 2's three rejections are the architecture working in the open. A sweep
credit's narration echoes one member settlement's reference, so a reference match
looks correct and the amount is wrong by lakhs. The gate caught all three, and
pass 5 then resolved them properly by subset-sum.

### Sweep credits are an NP-complete search, bounded on purpose

Recovering which settlements a sweep credit covers is subset-sum. It is bounded
three ways so it terminates: a candidate window, a cardinality cap of 4, and a node
budget of 20,000 expansions. When the budget is exhausted the pipeline says so
rather than reporting "no match".

The search looks for up to two solutions. Finding exactly one is a match. Finding
two means the credit is genuinely explainable more than one way, and the output is
an exception naming both combinations. **Ambiguity is a finding, never a
tiebreak.** Picking the first subset found would produce a match that is plausible
and wrong, and a wrong match is worse than no match because it closes a break
nobody will look at again.

### What the model actually contributes

The generator forces three pairs of settlements to identical nets and gives their
credits a narration that names the batch in prose rather than as a reference token.
Amount is then not discriminating, two candidates fit equally, and the
deterministic passes correctly decline all six credits. The narration text is the
only thing that separates them. That is a language problem, and the arithmetic
still checks the answer.

Those 6 credits are where the 5 assisted matches come from. Everything else was
already settled by arithmetic.

## The Q&A agent, and grounding as a checkable property

`python -m recon.ask` puts a tool-calling agent on top of a finished run. It has
nine read-only tools and no ability to match, re-match, mutate the ledger or move
money. Ask it why a credit is unmatched, what a settlement is composed of, what the
verification gate rejected, or how much is sitting in exceptions, and it answers by
calling tools rather than by generating figures.

The interesting part is that **groundedness is verified, not trusted.** Every tool
records which record identifiers it surfaced. After the agent answers, every
identifier in its prose is checked against that set. An identifier the model
produced without a tool having returned it is a fabrication and is reported as one.

That is deliberately the same shape as the arithmetic gate. There, a model may
propose a match and deterministic code confirms the amounts close. Here, a model may
propose an explanation and deterministic code confirms the explanation refers to
records that actually exist in the run.

`python -m recon.evals qa` grades this against 10 pinned questions, checking four
things each: that a relevant tool was called, that the required records were cited,
that required figures appear verbatim, and that nothing was ungrounded. One question
is adversarial. It asks about `setl_99999`, which does not exist, and the agent
passes only if it declines rather than inventing a record.

```
10/10 passed
grounding failures: 0
```

Building this also caught two real bugs in the offline router that the golden set
would have let through if it only checked for a non-empty answer. The router matched
keywords by substring, so `"gate" in "gateway"` sent "which settlements did the
gateway misreport" to the verification-gate tool. And the findings summary reported
a per-reason split with no total, forcing the reader to add up the numbers by hand.
Both are fixed.

## Honest limitations

**The generator and the reconciler were written by the same person.** The test set
can only contain difficulties I thought of. 100% on this data means the pipeline
handles the enumerated failure modes, not that it handles a real bank statement.
The list of injected scenarios in every report is there so a reader can judge
whether the difficulty is representative, and disagree.

**The offline baseline is a regex.** It reads `BATCH SETL00023` because I wrote it
to. On this dataset it matches the hosted model, so the honest reading is that the
model is not earning its cost on the matching residue. `python -m recon.evals
backends` prints both side by side rather than implying otherwise. The model's value
would appear on narration formats nobody enumerated in advance, which is precisely
what this generator cannot produce.

**The Q&A agent's offline backend is a keyword router, and it does not understand
questions.** It recognises about a dozen shapes. It exists so the agent is
demonstrable without credentials and so the hosted model has a real baseline to be
measured against. Both pass the same 10 golden questions, which says more about the
questions being answerable from tools than about the router being clever. With a key
set, the same tools and the same grounding check apply to the model.

**A measured false positive, and what was done about it.** With pass B allowed to
match payments to orders the merchant marked abandoned, a sweep produced exactly
one false positive in 46,191 records: an orphan payment attached to an abandoned
order two days older with an identical amount of 2,406.00. Both readings were
possible, the consequences were not symmetric, so the default is now to decline and
raise it instead. `python -m recon.evals policy` shows the trade-off cost zero
correct matches. The flag survives so it can be re-measured elsewhere.

**Tolerances are declared, not discovered.** One rupee on amounts, three days on
settlement lag. A wider tolerance would raise the match rate and lower precision.
Both are printed in every report so the trade-off is visible.

**Scale is untested beyond ~1,300 records per batch.** Throughput is comfortable,
but pass A is O(n) per payment against an in-memory dict, and the sweep search's
candidate window is what keeps subset-sum viable. A million-record month would need
the candidate selection indexed by date rather than scanned.

## Audit trail

Every decision is one append-only event: pass completions, accepted matches,
rejected proposals, suppressed duplicates, classifier proposals with the candidates
they were offered, and exceptions raised. Each event carries a SHA-256 hash of its
own content plus the previous event's hash, so altering history invalidates every
hash after it.

The run then replays the event stream and reconstructs the final set of matches,
exceptions and suppressions from the events alone, and asserts the reconstruction
equals live state. If the log cannot reproduce the outcome it describes, the report
says so. An audit trail that cannot do that is decoration.

## Repository layout

```
recon/
  models.py      domain model; all money is int paise, never float
  money.py       basis-point arithmetic, MDR and GST
  narration.py   bank narration parsing; a reference is a hint, not a key
  generate.py    synthetic ledger built forwards, with ground truth
  subsetsum.py   bounded subset-sum for sweep credits
  passes.py      the deterministic pass ladder
  verify.py      the single verification gate
  classify.py    residue classifier: offline baseline and hosted model
  pipeline.py    orchestration
  metrics.py     scoring against ground truth, report rendering
  store.py       CSV and JSON artefacts
  audit.py       hash-chained, replayable event log
  agent.py       Q&A agent: 9 read-only tools, verified groundedness
  html_report.py self-contained HTML dashboard, no build step
  cli.py         python -m recon.cli
  ask.py         python -m recon.ask
  serve.py       python -m recon.serve, stdlib http.server
  demo.py        paced walkthrough for screen recording
  evals.py       seed sweep, policy and backend comparison, Q&A golden set
docs/index.html  published report, readable without cloning
tools/record_walkthrough.ps1  silent 1080p capture of the walkthrough
tools/record_browser.py   scripted capture of the dashboard section
tools/cue_sheet.py    narration windows, from the demo's own timings
tools/assemble.py     final cut from footage, cues and narration clips
tools/analyse_footage.py  beat boundaries by per-frame luma
tools/find_anchors.py     output start and end inside each beat
VIDEO.md         shot list, measured timings and narration script
VOICEOVER.md     ElevenLabs input, cut into per-window clips
```

The `tools/` scripts produce the pitch video and are not part of the pipeline.
`record_browser.py` needs playwright, which is deliberately not in
`requirements.txt`.

## Data and safety

All data is synthetic, generated locally from a seed. No real payment data,
customer identifier, card number or credential is used, stored or transmitted. The
project does not call the Razorpay API and does not move money. It is not
affiliated with Razorpay.

The optional hosted-model backend sends only synthetic record fields, and only when
`ANTHROPIC_API_KEY` is explicitly set.

`python -m recon.serve` binds to `127.0.0.1` and has no authentication, which is
deliberate rather than an oversight: it serves a read-only view of one in-memory run
of locally generated synthetic data, and neither endpoint can mutate anything. It
warns if you bind it to another interface. It is a demo surface, not a service.
