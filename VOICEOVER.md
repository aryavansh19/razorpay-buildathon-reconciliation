# Voiceover input for ElevenLabs

Narration for the pitch video, cut into clips that map to exact windows in the footage.

## Why this is 16 clips and not one paste

The first attempt was a single continuous take laid over the video. It went out of sync
in a way no amount of stretching fixes: within each beat the narration was still
explaining what was about to happen while the terminal was already scrolling past it,
and by the end the voice was describing a screen that had changed thirty seconds
earlier.

The footage is now built for narration. Each beat holds still while its setup line is
spoken, *then* runs its command, *then* holds still again while the numbers are read.
The demo writes down exactly when each of those happens, so the windows below are
measured rather than estimated:

```
python tools/cue_sheet.py
```

Generate one clip per row. Name the files by the id column. Assembly then places each
clip at its window with no guesswork.

## The windows

| id | clip | window in `terminal.mp4` | length | words |
|---|---|---|---|---|
| `00a` | intro card | 0:16.2 - 0:40.3 | 24.1s | 58 |
| `01a` | the problem | 0:40.3 - 0:53.4 | 13.1s | 31 |
| `02a` | reconcile, setup | 0:53.4 - 1:00.5 | 7.1s | 17 |
| `02b` | reconcile, payoff | 1:04.9 - 1:16.9 | 12.0s | 29 |
| `03a` | arithmetic, setup | 1:16.9 - 1:24.5 | 7.6s | 18 |
| `03b` | arithmetic, payoff | 1:29.2 - 1:41.3 | 12.0s | 29 |
| `04a` | gate, setup | 1:41.3 - 1:48.9 | 7.6s | 18 |
| `04b` | gate, payoff | 1:53.2 - 2:05.2 | 12.0s | 29 |
| `05a` | false positive, setup | 2:05.2 - 2:13.3 | 8.1s | 19 |
| `05b` | false positive, payoff | 2:21.6 - 2:34.6 | 13.0s | 31 |
| `06a` | generalise, setup | 2:34.6 - 2:41.7 | 7.1s | 17 |
| `06b` | generalise, payoff | 2:58.7 - 3:12.7 | 14.0s | 33 |
| `07a` | agent, setup | 3:12.7 - 3:20.8 | 8.1s | 19 |
| `07b` | agent, payoff | 3:24.0 - 3:34.5 | 10.5s | 25 |
| `08a` | outro card | 3:34.5 - 3:59.1 | 24.6s | 59 |
| `99` | browser section | `browser.mp4`, full | 44.1s | 106 |

Terminal footage is 3:49, browser is 0:44. Total 4:33, inside the five-minute limit.

## Settings

Same voice as before. If your model exposes a stability setting, take the middle
option; leave speed at default. Check `lakhs`, `paise` and `GST` in a preview before
generating all sixteen, and write them `laakhs` or `G S T` if they come out wrong.

Eleven v3 uses `[audio tags]` and does not support `<break>`. Every other model is the
reverse. Tags below are v3; if you use v2, delete the bracketed tags.

## The clips

### `00a` intro card, 58 words

```
[matter-of-fact] This is my submission for Track 4, AI Finance Controller. The brief asks for an agent that closes one finance-ops loop across a fifty-plus record batch, reporting its match rate and the exceptions it could not resolve. [pause] The bar is throughput, measured accuracy, and an honest exception list. So I built a three-way reconciler, and the rest of this is evidence.
```

### `01a` the problem, 31 words

```
[matter-of-fact] A merchant has three sources of truth that never agree. [pause] One bank credit does not match one payment. It matches a netting identity: payments less fees and GST, less refunds, less chargebacks, plus adjustments.
```

### `02a` reconcile, setup, 17 words

```
[matter-of-fact] One command. No dependencies, no API key. Watch what it reports, because that is the whole argument.
```

### `02b` reconcile, payoff, 29 words

```
[matter-of-fact] Eleven sixty-five records in forty-four milliseconds. [pause] Three numbers, not one. Four eighty-one by arithmetic, five model-assisted and verified, forty-seven raised. A blended rate would hide which did the work.
```

### `03a` arithmetic, setup, 18 words

```
[matter-of-fact] That identity is why matching is hard. Here it is for one settlement, computed from its own line items.
```

### `03b` arithmetic, payoff, 29 words

```
[matter-of-fact] Recomputed net, nineteen twenty and nine paise. The gateway reported seventeen thirty-seven twenty-two. [pause] [reflective] The money is right. The report is wrong. Nothing here trusts that header.
```

### `04a` gate, setup, 18 words

```
[matter-of-fact] Every match passes one function before it counts, whether arithmetic proposed it or a language model did.
```

### `04b` gate, payoff, 29 words

```
[matter-of-fact] Three rejected. A sweep credit echoes one settlement's reference, so the match looks right... and is wrong by lakhs. [pause] Subset-sum then resolved all three properly.
```

### `05a` false positive, setup, 19 words

```
[reflective] Now a mistake of mine, rather than only results. I let payments with no order reference match on amount alone.
```

### `05b` false positive, payoff, 31 words

```
[matter-of-fact] One false positive in forty-six thousand records. [pause] [drawn out] A wrong match closes a break permanently. Declining costs one exception. [pause] Measured both ways, declining removed it and cost zero correct matches.
```

### `06a` generalise, setup, 17 words

```
[matter-of-fact] One good run proves nothing, and the track says so. Two hundred independently generated batches.
```

### `06b` generalise, payoff, 33 words

```
[matter-of-fact] Two hundred and thirty thousand records. One hundred percent precision and recall... minimum, mean and maximum. [pause] [reflective] And I wrote the generator too, so that table lists every difficulty I thought of.
```

### `07a` agent, setup, 19 words

```
[matter-of-fact] The output is only useful if someone can interrogate it, so there is an agent with nine read-only tools.
```

### `07b` agent, payoff, 25 words

```
[matter-of-fact] Ten graded questions, one adversarial. Every identifier is checked against what the tools returned, so a fabricated one is reported, not presented as fact.
```

### `08a` outro card, 59 words

```
[matter-of-fact] Everything is in the repo: the architecture, the scored report, the exception ledger as a CSV, and a hash-chained audit log that replays to reproduce its own outcome. [pause] One command reproduces every figure you have seen. [pause] [reflective] And the limitation, stated plainly: I wrote both sides, so the test set only contains difficulties I thought of.
```

### `99` browser section, 106 words

```
[matter-of-fact] The same output, for the person who actually has to clear it. Three numbers, precision and recall against ground truth, and the money. [pause] Then the exception ledger. Forty-seven findings, each with a reason code and a suggested action, filterable. Isolate the misreported settlements and there are five. [pause] Open setl_00016 and you get the arithmetic behind it: payments net of fees, refunds out, adjustments applied, recomputed nineteen twenty and nine, against a reported seventeen thirty-seven twenty-two. Header disagrees by a hundred and eighty-two eighty-seven. [pause] And a live question, answered through read-only tools, with every cited identifier traced back to what those tools returned.
```

## What to do with the files

Save them into `media/vo/` named by id, for example `media/vo/02b.mp3`. Then:

```powershell
python tools/sync_voiceover.py --cues media/cues.json --vo media/vo
```

Each clip is placed at its window. Where a clip runs longer than its window the video
freezes on that frame, which is invisible because the terminal is static there by
design. Where it runs short the remaining stillness is simply quiet.

## Lessons already paid for

Recorded here so they are not repeated.

**Do not join separately-encoded MP4 segments with `-c copy`.** MP4 stores NAL units
length-prefixed and each encode carries its own parameter set, so the result is a
bitstream no decoder can split. It reports a correct duration and correct frame count
and plays nothing. The build now does one encode via the concat filter, and decodes the
finished file end to end, failing if the decoder emits anything at all. Container
metadata is not evidence of playability.

**Do not detect beat boundaries from pixels when the program knows them.** Luma-trough
detection mislabelled beats whose output is sparse, finding twelve clears where there
were nine. The demo now writes its own timings and only one calibration point is
recovered from the footage.

**Disable QuickEdit on the recording console.** One stray click puts it in selection
mode, which blocks the program on its next write and renames the window, so the demo
hangs and ffmpeg loses the window it was capturing. A watchdog now aborts the take
within fifteen seconds instead of running forever.
