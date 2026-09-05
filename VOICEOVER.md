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

Positions are in the finished cut, `media/video_clean.mp4`, so they can be checked by
scrubbing to a timecode and seeing whether the screen matches the line. Regenerate this
table rather than editing it, because an earlier hand-copied version silently went stale
when the cue origin was recalibrated:

```
python tools/vo_table.py
```

| id | clip | window in the finished cut | length | words at 145 wpm |
|---|---|---|---|---|
| `00a` | intro card | 0:06.0 - 0:30.1 | 24.1s | 58 |
| `01a` | the problem | 0:30.1 - 0:43.2 | 13.1s | 31 |
| `02a` | reconcile, setup | 0:43.3 - 0:50.4 | 7.1s | 17 |
| `02b` | reconcile, payoff | 0:54.7 - 1:06.7 | 12.0s | 29 |
| `03a` | arithmetic, setup | 1:06.7 - 1:14.3 | 7.6s | 18 |
| `03b` | arithmetic, payoff | 1:19.1 - 1:31.1 | 12.0s | 29 |
| `04a` | gate, setup | 1:31.1 - 1:38.7 | 7.6s | 18 |
| `04b` | gate, payoff | 1:43.0 - 1:55.0 | 12.0s | 29 |
| `05a` | false positive, setup | 1:55.0 - 2:03.1 | 8.1s | 19 |
| `05b` | false positive, payoff | 2:11.4 - 2:24.4 | 13.0s | 31 |
| `06a` | generalise, setup | 2:24.4 - 2:31.5 | 7.1s | 17 |
| `06b` | generalise, payoff | 2:48.5 - 3:02.5 | 14.0s | 33 |
| `07a` | agent, setup | 3:02.6 - 3:10.7 | 8.1s | 19 |
| `07b` | agent, payoff | 3:13.8 - 3:24.3 | 10.5s | 25 |
| `08a` | outro card | 3:24.3 - 3:48.9 | 24.6s | 59 |
| `99` | browser section | 3:49.0 - 4:33.0 | 44.1s | 106 |

Terminal footage is 3:49, browser is 0:44. Total 4:33, inside the five-minute limit.

The gaps between windows are not spare narration room. Those are the stretches where a
command is running and output is scrolling, and anything said over them is the specific
fault this layout exists to avoid.

The word counts assume 145 wpm and are tight rather than generous. A human read of `02b`
came in at 15.6s against its 12.0s window and `07a` at 8.3s against 8.1s, so if a
generated clip overruns, shorten the wording rather than relying on the speed-up.

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

## The single paste

Generating sixteen clips by hand is tedious. This is the whole script as one block,
followed by a step that cuts it back into the sixteen clips automatically, so the
convenience of one generation does not cost the sync.

It works by transcribing the generated file and aligning the transcript against the
clips below, then cutting at the word boundaries where one clip ends and the next
begins. Cutting on silence instead does not work, because the pause between two clips
is indistinguishable from the pause between two sentences inside one. Padding the script
with `<break>` tags to make them distinguishable is worse: ElevenLabs documents that many
break tags in a single generation make the model speed up or add artefacts, and v3 does
not support them at all. Aligning to the words needs no marker.

```powershell
python tools/split_voiceover.py media/narration.mp3 --model small.en
python tools/sync_voiceover.py --no-trim --extend 02b=2.7 --max-tempo 1.2
python tools/transcribe.py --video media/final_tts.mp4 --model small.en
```

Those are the settings a real generation needed, not defaults. A v3 read of this script
came back at 222s against 225s of total window room, so it fits overall but leaves almost
nothing spare in any one window, and two clips wanted slightly more than the default
1.15x. `02b` was given real room instead, taken from the gap before it where the screen
already shows the record count and throughput, which left `01a` as the only clip needing
much: 1.16x. Everything else came in at 1.11x or below.

Result: nine of sixteen cues matched their script exactly, the rest scored 74 to 87
percent on recognition noise alone, and the track matched the picture to 0.00s.

`--no-trim` is not optional. The split clips are already cut tight to the first and last
word, and letting the placement step trim them again removes real speech: it cut "did the
work" off the end of `02b` before this was caught.

If the script exceeds the character limit for your model, generate it in two parts and
pass both, in order. Split after the outro paragraph, before "The same output":

```powershell
python tools/split_voiceover.py media/part1.mp3 media/part2.mp3
```

Two notes on wording for a synthesised read. `setl_00016` is written "settlement sixteen"
below, because the identifier itself reads badly aloud. And check `lakhs`, `paise` and
`GST` in a preview.

```
[matter-of-fact] This is my submission for Track 4, AI Finance Controller. The brief asks for an agent that closes one finance-ops loop across a fifty-plus record batch, reporting its match rate and the exceptions it could not resolve. [pause] The bar is throughput, measured accuracy, and an honest exception list. So I built a three-way reconciler, and the rest of this is evidence.

A merchant has three sources of truth that never agree. [pause] One bank credit does not match one payment. It matches a netting identity: payments less fees and GST, less refunds, less chargebacks, plus adjustments.

One command. No dependencies, no API key. Watch what it reports, because that is the whole argument.

Eleven sixty-five records in forty-four milliseconds. [pause] Three numbers, not one. Four eighty-one by arithmetic, five model-assisted and verified, forty-seven raised. A blended rate would hide which did the work.

That identity is why matching is hard. Here it is for one settlement, computed from its own line items.

Recomputed net, nineteen twenty and nine paise. The gateway reported seventeen thirty-seven twenty-two. [pause] [reflective] The money is right. The report is wrong. Nothing here trusts that header.

[matter-of-fact] Every match passes one function before it counts, whether arithmetic proposed it or a language model did.

Three rejected. A sweep credit echoes one settlement's reference, so the match looks right... and is wrong by lakhs. [pause] Subset-sum then resolved all three properly.

[reflective] Now a mistake of mine, rather than only results. I let payments with no order reference match on amount alone.

[matter-of-fact] One false positive in forty-six thousand records. [pause] [drawn out] A wrong match closes a break permanently. Declining costs one exception. [pause] Measured both ways, declining removed it and cost zero correct matches.

One good run proves nothing, and the track says so. Two hundred independently generated batches.

Two hundred and thirty thousand records. One hundred percent precision and recall... minimum, mean and maximum. [pause] [reflective] And I wrote the generator too, so that table lists every difficulty I thought of.

[matter-of-fact] The output is only useful if someone can interrogate it, so there is an agent with nine read-only tools.

Ten graded questions, one adversarial. Every identifier is checked against what the tools returned, so a fabricated one is reported, not presented as fact.

Everything is in the repo: the architecture, the scored report, the exception ledger as a CSV, and a hash-chained audit log that replays to reproduce its own outcome. [pause] One command reproduces every figure you have seen. [pause] [reflective] And the limitation, stated plainly: I wrote both sides, so the test set only contains difficulties I thought of.

[matter-of-fact] The same output, for the person who actually has to clear it. Three numbers, precision and recall against ground truth, and the money. [pause] Then the exception ledger. Forty-seven findings, each with a reason code and a suggested action, filterable. Isolate the misreported settlements and there are five. [pause] Open settlement sixteen and you get the arithmetic behind it: payments net of fees, refunds out, adjustments applied, recomputed nineteen twenty and nine, against a reported seventeen thirty-seven twenty-two. Header disagrees by a hundred and eighty-two eighty-seven. [pause] And a live question, answered through read-only tools, with every cited identifier traced back to what those tools returned.
```

This route was tested by feeding it the continuous human take: all sixteen clips were
found, every cut landed within about a second of its true window, and the assembled track
matched the picture to 0.00s.

## What to do with the files

Save them into `media/vo/` named by id, for example `media/vo/02b.mp3`. Any of mp3, wav,
m4a, aac, flac, ogg or opus is fine. Then:

```powershell
python tools/sync_voiceover.py
python tools/sync_voiceover.py --allow-missing    # while only some clips exist
```

That writes `media/final_tts.mp4`, leaving the spoken cut at `media/final.mp4` alone so
the two can be compared.

Each clip is placed at its window and pinned to exactly the window length, so one clip
running long cannot push the next fifteen out of sync. Leading and trailing silence is
measured and trimmed first, against each file's own noise floor rather than a fixed
threshold. If the words still overrun, the clip is sped up, and the tool refuses past
1.15x instead of quietly making the narration sound hurried. Where a clip runs short the
remaining stillness is simply quiet, which is invisible because the terminal is static
there by design.

Check the result the same way as the spoken cut, rather than assuming it landed:

```powershell
python tools/transcribe.py --video media/final_tts.mp4
```

That buckets recognised words into cue windows and diffs them against the clips above, so
a clip saved under the wrong id shows up as a mismatch instead of shipping.

## One caveat worth weighing

Razorpay ships ElevenLabs voice agents, so reviewers on this panel are unusually likely
to recognise a synthesised read. A spoken cut already exists at `media/final.mp4`,
verified line by line against this script. Generating this version to compare is
reasonable; replacing a working human read with a synthetic one for a judged submission is
a trade worth making deliberately rather than by default.

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
