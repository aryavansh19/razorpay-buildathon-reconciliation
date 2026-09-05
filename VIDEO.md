# Pitch video: shot list and script

Five minutes. Two windows: a terminal and a browser. Nothing else.

The terminal segment is scripted and paced, so it records in one take:

```powershell
.\tools\record_walkthrough.ps1 -Pace 1.8
```

## Current state

| File | What it is | Length |
|---|---|---|
| `media/terminal.mp4` | walkthrough, silent, intro and outro cards included | 3:49 |
| `media/browser.mp4` | dashboard section, silent | 0:44 |
| `media/terminal.timeline.json` | exact beat timings, written by the demo itself | |
| `media/cues.json` | 16 narration windows, measured | |
| `media/vo/` | drop narration clips here, named by cue id | empty |

Assembled length will be about 4:36, inside the five-minute limit.

Remaining step: generate the sixteen narration clips listed in
[VOICEOVER.md](VOICEOVER.md), then run

```powershell
python tools/assemble.py
```

The assembly path is already tested end to end with placeholder audio: it produced a
4:36 file that decoded clean, with the browser section landing where predicted.

| Property | Value |
|---|---|
| Duration | 3:39 (219.3s) |
| Resolution | 1920x1080, 5,242 frames |
| Size | 6.9 MB, H.264 |
| Audio | none, single video stream |
| Content | all seven beats, real pipeline output |

Decodes clean end to end. Drop it into any editor and record over it.

### Four things that had to be fixed to get a usable take

Worth reading before you re-record, because three of the four fail silently.

**Output arrived under the wrong heading.** Python only line-buffers stdout when it
is a tty, and launched through a PowerShell pipeline it is not. So the demo's own
narration sat in a block buffer while the child processes wrote straight to the
console, and a beat's closing line could surface two beats later. Fixed by forcing
line buffering and running children with `-u`. Invisible when testing in an ordinary
terminal, obvious in the footage.

**Long output jumped to the tail.** A child writing 43 lines at once fills the window
in a single frame, so the headline numbers are gone before anyone reads them. Output
is now revealed line by line at a pace-scaled delay, so the terminal scrolls at a
readable rate. The child still runs at full speed; only the presentation is paced,
and `--fast` collapses it to nothing.

**A stray click froze the recording.** The classic console has QuickEdit on by
default. One click inside the window puts it in selection mode, which blocks the
program the next time it writes and renames the window to `Select <title>`. The demo
hung with no error and ffmpeg lost the window it was capturing by title, failing
every frame. QuickEdit is now disabled for the recording console, and a watchdog
aborts the take within 15 seconds if it happens anyway instead of running forever.

**Sizing.** The console is 92 columns by 46 rows at Consolas 22pt, not maximised. The
demo formats to 74 columns, so maximising only adds empty space. The row count is the
real constraint: the longest beat needs 55 lines, so a short window scrolls its own
opening away. The window is letterboxed into 1920x1080 by padding rather than
scaling, because upscaling a terminal blurs glyphs. The pad colour matches the
console background, so the bars are invisible.

ffmpeg captures the console *window* by title, not the desktop, so your IDE, browser,
taskbar and notifications stay out of frame without tidying anything.
`-draw_mouse 0` keeps the pointer out. Pass `-FullDesktop` for the whole screen.

`tools/capture.ps1` is the simpler desktop-capture variant, for filming yourself
driving the browser.

### Recording it yourself

Nothing stops you doing this manually with OBS or Game Bar, and the demo is built for
it:

```powershell
python -m recon.demo --countdown 10 --pace 1.6
```

Start your recorder, run that, and it waits ten seconds before the first beat. The
only thing you lose is the automatic window-only framing, so full-screen the terminal
first and close anything you do not want in shot.

Paste-ready ElevenLabs input for both model families is in
[VOICEOVER.md](VOICEOVER.md).

## On synthesised narration

Record it yourself. Not because a hosted voice would sound bad, but because of who is
watching.

Razorpay shipped a voice-led Subscription Recovery Agent built with ElevenLabs at
FTX'26. The people reviewing this submission integrate synthetic voice for a living.
They will identify it in the first sentence, and the question it raises is not "is
this good audio", it is "why did this candidate not explain their own work". The
buildathon's own framing is that shortlisted builders go straight to a panel, so the
video is the only place a reviewer hears you reason before meeting you. Handing that
to a voice model spends your one differentiator to save twenty minutes.

An accent, a stumble, or a re-take is not a negative. Every strong engineering video
has them. What does not survive is a polished voice with nobody behind it, especially
on beat 5, where the whole point is that you found your own mistake and argued about
the cost of it. That reasoning has to sound like it belongs to someone.

If your recording environment genuinely makes clean audio impossible, synthesised
narration is better than unusable audio or no video. In that case say so in the
submission rather than letting a reviewer discover it, and keep beat 5 in your own
voice if you can manage nothing else.

## Timing, measured from the footage

Not estimated. `tools/analyse_footage.py` samples per-frame luma at 10 fps and finds
the seven screen clears, and each boundary below was then confirmed by extracting the
frame and reading its heading.

```
python tools/analyse_footage.py media/walkthrough.mp4
```

| Segment | From | To | Length | Word budget |
|---|---|---|---|---|
| Title card | 0:00 | 0:07 | 7.5s | 18 |
| 1 The problem | 0:07 | 0:28 | 21.0s | 50 |
| 2 Reconcile a batch | 0:28 | 0:56 | 27.8s | 67 |
| 3 Arithmetic by hand | 0:56 | 1:25 | 29.2s | 70 |
| 4 Verification gate | 1:25 | 1:54 | 28.5s | 68 |
| 5 The false positive | 1:54 | 2:28 | 34.2s | 82 |
| 6 Does it generalise | 2:28 | 3:14 | 46.1s | 111 |
| 7 Agent is graded | 3:14 | 3:39 | 25.0s | 60 |

Word budgets assume 145 wpm, an unhurried pace for technical material. The script
below is written to fit them, so you can read straight through without racing or
stalling.

The terminal footage is 3:39. The submission allows five minutes, so there is room
for roughly 80 seconds of browser shots: the exception ledger in `docs/index.html`
and one live question in `python -m recon.serve`. Put them at the front and the back.

### Measured footage lengths

Timed on this machine, so plan against these rather than guessing:

| Command | Footage |
|---|---|
| `python -m recon.demo` (all 7 beats, `--pace 1.0`) | 2:03 |
| `python -m recon.demo --beats 2,5,6` | 1:06 |
| `python -m recon.demo --pace 1.8` (all 7) | about 3:40 |
| `python -m recon.demo --fast` | 40s, rehearsal only, unwatchable |

The narration above runs longer than the footage at `--pace 1.0`, because reading
aloud is slower than a terminal printing. Two ways to fix that, both fine:

Record at `--pace 1.8`, which stretches the pauses and gives you room to talk over
the whole thing in one pass. Or record at `--pace 1.0` and hold the frame in your
editor where you need more time. The first is easier if you are not comfortable in an
editor.

For a retake of one beat: `.\tools\capture.ps1 -Beats 5 -Out media\beat5.mp4`.

## Script, timed to the footage

Read as prose. Each block is written to its segment's word budget, so if you start a
block when the heading appears you will finish as the next one does. Where a number is
on screen, let it appear before you say it.

### 0:00 - 0:07, title card (16 words)

> Three-way settlement reconciliation, for Track 04. Deterministic code proposes and
> disposes. The model only ever proposes.

### 0:07 - 0:28, the problem (48 words)

> A merchant has three sources of truth that never agree. Reconciling them is manual
> work. And one bank credit does not match one payment. It matches a netting identity:
> payments less fees and GST, less refunds, less chargebacks, plus adjustments. Sweeps,
> carry-forwards and truncated references make it worse.

### 0:28 - 0:56, reconcile a batch (62 words)

> One command, no dependencies, no API key. The generator builds the data forwards and
> records the correct answer as it goes, so this gets scored rather than demonstrated.
> Eleven sixty-five records in forty milliseconds. Three numbers, not one: four
> eighty-one matched by arithmetic, five where a model proposed and the gate accepted,
> forty-seven raised. A blended rate hides which did the work.

### 0:56 - 1:25, arithmetic by hand (71 words)

> Here is that identity for one settlement. Payments net of fees, refunds out,
> chargebacks out, adjustments in. Recomputed net, nineteen twenty and nine paise. And
> the gateway's own reported figure beside it, wrong by a hundred and eighty-two rupees
> eighty-seven. The money is correct. The bank credit follows the line items. It is the
> report that is wrong. Nothing here reads that header when matching, which is why this
> gets caught.

### 1:25 - 1:54, the verification gate (70 words)

> Every match passes one function before it is accepted, whether an exact reference
> lookup proposed it or a language model did. It re-derives the amounts from the ledger
> and rejects anything that does not close. These three are that gate working. A sweep
> credit covers several settlements and echoes just one reference, so the match looks
> right and is wrong by lakhs. All three rejected, then subset-sum resolved them
> properly.

### 1:54 - 2:28, the false positive (84 words)

> Now a mistake, rather than only results. Payments sometimes arrive with no order
> reference, and a real controller does try to place them by amount and date. I allowed
> that. Across forty-six thousand records it produced exactly one false positive: an
> orphan payment attached to an abandoned order with an identical amount. Both readings
> were possible, but the consequences are not symmetric. A wrong match closes a break
> permanently. Declining costs one exception. Measured both ways, declining removed it
> and cost zero correct matches.

### 2:28 - 3:14, does it generalise (111 words)

> One good run proves nothing, and the track says so. So this is two hundred
> independently generated batches, reporting the distribution rather than the best
> case. Two hundred and thirty thousand records. Precision and recall at one hundred
> percent, minimum, mean and maximum. Zero false positives, zero coverage holes, zero
> audit failures. Four hundred and forty-six proposals rejected by the gate along the
> way. And the honest caveat: I wrote both the generator and the reconciler, so the
> test set only contains difficulties I thought of. That table lists exactly which
> ones, so you can decide whether it is representative and disagree with me. That is
> the point of printing it.

### 3:14 - 3:39, the agent is graded (62 words)

> Last thing. The output is only useful if someone can interrogate it, so there is an
> agent over the finished run with nine read-only tools. It cannot match, re-match, or
> move money. Every identifier in an answer is checked back against what the tools
> actually returned, so a fabricated one is reported rather than presented as fact.
> Ten graded questions, one adversarial.

### Optional browser bookend, about 80 seconds

If you want the full five minutes, open on `docs/index.html` with the exception ledger
visible and say what the problem is, then close on `python -m recon.serve` asking
"which proposals did the verification gate reject?" and let the grounded answer with
cited record identifiers land. Then: "Repo has the architecture, the limitations, and a
one-command reproduction."

## Before you hit record

- Terminal font up to 18-20pt. A reviewer may watch this on a laptop.
- Terminal window at 1920x1080 or full screen, not a narrow strip.
- `python -m recon.demo --fast` once first, so nothing is cold and no path errors surprise you.
- Close Slack, mail, and notifications. One toast is one retake.
- Have the browser already on `docs/index.html`, Exception ledger tab selected.
- Have `python -m recon.serve` already running in a second terminal for the last shot.
- Record video and voice separately if you can. Reading while watching your own
  terminal is harder than it sounds.

## If you are short on time

Cut in this order. Keep beats 2, 5 and 6.

Beat 2 is the result. Beat 5 is the false positive, which is the single most
credible thing in the submission because almost nobody volunteers one. Beat 6 is the
generalisation claim that separates you from a submission that ran once and
screenshotted it.

Beats 1, 3, 4 and 7 are the ones to drop first if you run long.
