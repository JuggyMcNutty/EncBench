# AGENTS.md

Working notes for AI agents modifying `encbench`. Read the **Invariants** section
before changing anything in `runner.py`, `quality.py`, `bench.py` or `probe.py` —
most of those rules exist because the obvious implementation produced numbers that
looked plausible and were wrong.

## Orientation

`encbench` is a stdlib-only Python benchmark that discovers a Linux system's usable
video encoders and measures their throughput, scaling, concurrency and quality via
ffmpeg. It ships as a directory you copy; there is no install step and no packaging.

```
encbench              bash launcher: sets PYTHONPATH=src, execs python3 -m encbench
src/encbench/
  __main__.py         CLI, top-level flow, signal handling, JSON payload assembly
  ffmpeg_setup.py     locate/validate/download ffmpeg; scratch-dir selection
  probe.py            system inventory, encoder discovery + live validation,
                      diagnostics, execution-order ranking
  sources.py          deterministic test clips, decode baselines, cache
  matrix.py           profiles, bitrate ladder, axis-sweep test plan generation
  bench.py            orchestration: calibrate, size, project, schedule, resume
  runner.py           process execution and measurement; concurrency ramp
  latency.py          pipeline delay under paced input; startup cost
  quality.py          PSNR/SSIM/VMAF, with a mandatory self-check
  report.py           terminal tables, JSON, comparison view
  html_report.py      self-contained HTML with inline SVG charts
  util.py             subprocess spawning + the shared process registry, ANSI
                      colour, width-aware padding, formatting
results/              run outputs (.json, .html, .jsonl) - gitignored territory
```

**Data flow:** `acquire ffmpeg → probe system+encoders → build plan → for each case:
calibrate, size, run, record → concurrency ramps → latency pass → quality pass →
render + write`.

## Commands

```bash
./encbench --list-encoders            # fastest way to see if discovery still works
./encbench --profile quick --no-report-files --encoders libx264 --resolutions 720p
python3 -m py_compile src/encbench/*.py
./encbench --compare a.json b.json
```

There is no test suite. Verification is empirical — see **Verification** below.

## Invariants

Each of these encodes a bug that already happened once. Do not "simplify" them away.

### Measurement

1. **Never trust `ffmpeg -encoders`.** Hardware encoders are listed on machines with
   no such silicon and fail only at runtime. Every candidate must pass a live
   encode (`probe._validate`), and the *exact invocation that worked* —
   `pre_input`, `filters`, `extra_args` — is stored on the spec and reused verbatim
   by the runner. Do not reconstruct hardware arguments anywhere else. The probe
   clip is **640x360x5**, not a token 320x240x2: RADV's Vulkan encoder passes a
   tiny clip and then dies with `VK_ERROR_DEVICE_LOST` at >=640x360, so too small
   a probe green-lights an encoder that cannot encode. Do not shrink it back.

2. **Calibration is keyed on `(encoder, resolution, preset)`.** x264 `ultrafast` is
   ~80x faster than `veryslow`; one estimate per encoder sizes fast runs so short
   they measure process startup. See `bench.Orchestrator._calibrate`.

3. **Calibration estimates are biased low; never gate a decision on them.**
   A calibration run is a handful of frames, so fixed startup cost (hardware
   device init, filter graph setup) dominates it — measured on a Gemini Lake
   iGPU, 16 frames reads 75 fps where 300 frames sustains 154. That bias is
   tolerable for *sizing* a run (which has floors) but not for yes/no decisions.
   `Orchestrator._measured_fps` prefers a real measurement, and the concurrency ramp's
   "is this encoder above realtime" gate must use it. Gating on calibration once
   skipped every ramp on a machine whose encoders comfortably exceeded realtime.

4. **GOP is pinned to `2 * fps`** in `runner.build_command`. Encoder defaults vary
   enormously (x264 250, libvpx 9999) and unpinned they are not comparable.

5. **Output goes to a real muxer at `/dev/null`**, not `-f null -`. Same measured
   speed, but the null muxer reports `total_size=N/A`, losing achieved bitrate.

6. **`-nostdin` is mandatory.** Without it parallel ffmpeg processes fight over the
   terminal and the concurrency ramp deadlocks.

7. **Read frame/size counters from `-progress`, never scrape stderr — and send
   `-progress` to a private file, not `pipe:1`.** `-benchmark` supplies
   utime/stime/rtime/maxrss on stderr; the two sources are parsed separately
   (`parse_progress`, `parse_benchmark`). `-progress` shared stdout with the
   encoder's own output, and Fedora's libx265 (linked against libvmaf) prints
   `problem loading model file:` to stdout once per frame; it interleaved
   mid-line with the counters (`problem loading model file: frame=364`), so
   `parse_progress` lost the frame count and a healthy encode was recorded as
   "no usable progress output" and skipped ~50% of the time. `build_command`
   now writes `-progress file:<tmp>` via `runner.progress_file` /
   `drain_progress` — one sink per encode (per stream in the concurrency ramp),
   removed after it is read. `parse_progress` also hard-filters to the keys
   `-progress` actually emits.

8. **Throughput = frames / ffmpeg's own `rtime`**, falling back to wall time. Do not
   substitute the `fps=` field from progress — it is a rolling average.

9. **`-benchmark` writes at `AV_LOG_INFO`, so the encode must not run at
   `-loglevel error`.** It shipped that way once and every `bench:` line was
   discarded: `rtime`/`utime`/`stime`/`maxrss` were `None` in 231/231 saved
   results, `cpu_seconds`/`cpu_cores` never populated, and every throughput
   figure silently fell back to wall clock — folding process startup, hardware
   init and filter setup into the measurement, understating a short ultrafast
   1080p run by ~36%. `build_command` uses `-loglevel info -nostats`. If you
   touch it, assert `cpu_seconds` is non-null in the output.

10. **Any ffmpeg output whose filename lacks a real extension needs `-f`.**
   Source generation writes `<name>.mkv.partial`; without an explicit muxer
   ffmpeg cannot infer one, and the entire lossless fallback was dead code that
   would only ever surface on a machine short of scratch space.

### Reporting honesty

11. **Bitrate accuracy is only valid at >= `CONVERGENCE_SECONDS` (10s) of encoded
   video.** Measured curve for libx264 1080p30 on this content: +70% at 4s, +18% at
   8s, +9% at 10s, +4% at 15s. Shorter runs render `short run`. If you change
   measurement lengths, re-measure this curve rather than adjusting the constant.

12. **A test that approaches the source's decode rate is `decode-bound`** and must be
   flagged, not reported as the encoder's limit. `sources` measures a decode
   baseline per clip for exactly this.

13. **"Throttled" means the clock fell *and stayed down*, not that boost was not
   sustained, and not that the clock jittered.** Comparing against the CPU's
   *rated maximum* flagged 24/67 tests on a 70 C Ryzen and 40/41 on a Pentium
   Silver whose base clock is by design. Comparing the run's own min against its
   own max (`min < 0.65 * max`) then flagged every long software encode on a
   powersave box: averaged per-core `scaling_cur_freq` swings ~3x between work
   units under a bursty encode with the governor perfectly healthy (seen: min
   1.5 GHz, max 4.2 GHz, 81 C, no throttle). The flag now needs a *sustained
   decline* — `Sampler.sustained_drop` compares an early-window mean against a
   late-window mean and fires at >=35% — or a sensor above 95 C. A flag that
   fires on healthy hardware is worse than no flag.

14. **Concurrency "realtime" means the *minimum* per-stream fps held the target**,
    never the mean — an average hides streams that fell behind.

15. **A ramp has three distinct endings; never conflate them:** hardware session
   limit, genuine saturation, and *streams that crashed*. A level where streams
   failed for a non-session reason is not a capacity measurement —
   `RampStep.failed_streams` carries it and the report must say so.

16. **Never present a configured ramp ceiling as hardware saturation.** If the ramp
    stopped because it hit `--concurrency-max`, say so (`report.render_concurrency`).

17. **Report tables must filter to the baseline preset** (`report._baseline_preset`,
    `payload["baseline_presets"]`). The preset sweep adds extra rows at the baseline
    resolution; without the filter a resolution column can be filled by an unrelated
    preset's row.

18. **A baseline preset of `None` is a value, not an absence.** Hardware encoders
   anchored on the driver default have `preset=None`; testing `is not None`
   disables the filter for exactly those encoders and lets preset- and
   bitrate-sweep rows overwrite the resolution curve. Filter on **both** preset
   and bitrate — several rows otherwise match each cell and whichever landed
   first silently wins.

19. **Error extraction skips banner/separator lines** and prefers lines containing
    words like `error`/`failed`/`cannot`. SVT encoders print a decorative rule as
    their first stderr line, which once became the user-visible failure reason.

### Quality measurement

20. **Comparison streams are paired by frame index, not timestamp.**
    `quality._align` emits `settb=1/<fps>,setpts=N` on *both* inputs. With default
    framesync the two streams carry different timebases and each frame is compared
    against a neighbour — a bit-exact lossless encode scored 36 dB instead of
    infinity, and every metric was quietly wrong.

21. **The self-check gates the whole quality pass.** `quality.self_check` encodes
    losslessly and requires `inf` (or >= 60 dB). On failure the pass is *skipped*
    with a warning. Never report metrics that did not pass this gate.

22. **VBV constraints are used in the quality pass only, with per-encoder fallback.**
    Without them one-pass rate control overshoots wildly on short clips and encoders
    get compared at different real bitrates. SVT-AV1 rejects a hard cap outside CRF
    mode, so `quality.measure` retries unconstrained and marks the row
    `rate_capped=False`; the report flags those as not rate-matched.
    **Do not add VBV to the throughput matrix** — it costs libx264 ~16% and conflates
    rate-control work with encode work.

### Resources and lifecycle

23. **Scratch must not fill a tmpfs.** `ffmpeg_setup.choose_scratch` skips a tmpfs
    candidate when the estimate exceeds ~25% of RAM and falls back to `/var/tmp`.
    Verified in the wild: a 7.6 GB box with a 3.8 GB `/tmp` correctly relocated.

24. **Every child goes through `util.spawn`/`util.run`**, which registers it in
   the shared process table and starts a new session, so `kill_group` /
   `terminate_all` can tear down the whole tree. Source generation (up to 30 min)
   and encoder probes once bypassed the runner's registry and survived SIGTERM
   entirely; interactive Ctrl-C only appeared to work because the TTY signals the
   whole foreground group — the very property `start_new_session=True` exists not
   to rely on. A signal must leave no orphan ffmpeg (`pgrep ffmpeg` clean) and
   still render partial results.

25. **Results are appended to `.jsonl` as each test finishes.** `--resume` restores
    prior results, ramps and skips into the report — skipping completed work is only
    half of resume; a resumed finished run must still produce a full report.

26. **Nothing silently disappears.** An encoder too slow to measure inside the
    per-test cap is recorded as a `Skip` with a reason and its higher resolutions are
    dropped for that encoder.

27. **Skips must be recorded, not just remembered.** They go through
   `Orchestrator._skip`, which appends *and* `_record`s, so they survive
   `--resume`. Restored quality records must also be de-duplicated against the
   plan, or a resumed run re-measures them and lists every encoder twice.

### Plan generation

28. **Axis sweeps, not a cross product.** `matrix.build_plan` varies one dimension at
    a time around a baseline. A full cross product is thousands of runs.

29. **`balanced_preset` returns the middle of the encoder's *full* range**, not the
    middle of the sampled subset — and returns `None` for non-ladder knobs
    (`knob_is_ladder=False`) so the baseline stays on the driver default.
    VAAPI's `-quality` is non-monotonic on AMD (levels 1-3 and 8 run ~2x faster than
    4-7); treating it as an ordered ladder anchored the baseline on the slow mode.

30. **Tiers execute lowest-first, interleaved across encoders in
    `probe.popularity` order** (`Orchestrator._in_plan_order`, feeding on
    `matrix.build_plan`'s sort). Nothing bounds a run by the clock any more, so
    order is what decides whether an *interrupted* run is useful, and the answer
    is the encoders people actually deploy: hardware h264/hevc, then
    libx264/libx265, then shipped AV1, then vp9/vp8, then the reference encoders.
    The default sort was alphabetical by codec, which put `av1` first and so led
    every tier with libaom-av1 at 1.1 fps and librav1e at 0.4 — the two slowest
    things on the box, ahead of everything anyone was benchmarking for.
    **The quality pass applies the same ordering** (`run_quality` sorts by
    `probe.popularity`): `quality_plan` yields encoders in codec order, which put
    libaom-av1 first and — at ~2 fps — let it eat the entire quality pass while
    every mainstream encoder got no numbers.

    There is exactly **one** ordering rule. Tiers above 0 were separately
    re-sorted fastest-measured-first, to get the most measurements out of an
    expiring budget; with no budget that rule only fought this one. Do not
    reintroduce it.

### Latency measurement

31. **Delay is measured against paced input, never a free-running encode.**
   On a free run a 40-frame lookahead at 800 fps is 50 ms -- smaller than the
   spread of VAAPI device init, so the figure would be mostly noise. Paced at
   30 fps the same 40 frames are 1333 ms. `latency.measure` runs the *same*
   command twice, once paced, and solves `delta = D * (1/R - 1/E)` for D, with R
   the pacing rate. The fixed pre-first-frame cost cancels, which is the entire
   point: it is the largest and least repeatable term, and nothing that
   estimates it can be trusted at this scale.

32. **Pace with `-readrate`, expressed against the clip's *stored* rate, and
   disable the initial burst.** Three traps in one line of arguments:
   * The pacer runs at the demuxer, *below* the `-r` that reinterprets the
     clip's rate, so it never sees the target framerate. Measured: 600 frames
     of a 60 fps clip with `-re -r 30` takes 9.5s, not the 20s that pacing at
     30 would give. `-readrate (target / clip.rate)` is what actually paces at
     the target, which is why `SourceClip.rate` exists and is detected rather
     than assumed -- `--source` footage carries its own rate.
   * `-readrate_initial_burst` defaults to about 0.5s of media handed over for
     free. That is fifteen frames at 30 fps: enough to fill a lookahead before
     pacing starts, so a deep pipeline reads as a shallow one. It must be set.
   * A `0` there means "use the default", not "no burst". It takes a small
     positive value (0.001).

33. **The pacing rate is chosen, not assumed -- it is not the content rate.**
   The difference of the two runs is amplified by `E/(E-R)`: ill-conditioned as
   the encoder nears the pacing rate, *undefined* at `E = R`, and inverted below
   it. Pacing every encoder at the content framerate therefore made the method
   unusable below ~2x realtime, which on a four-core box is most encoders
   including the `libx264 medium` the self-check itself depends on -- the pass
   simply refused to run there. No threshold fixes that; only a slower pacer
   does. `latency.pace_for` returns `min(fps, E/MIN_REALTIME_X)`, so the margin
   is satisfied by construction for any encoder.

   This is sound because **D is a frame count and R only sets how fast frames
   are offered.** Verified before relying on it, on libx264 720p at R = 30, 15
   and 7.5 fps: `medium` read 62.5 / 60.0 / 56.3 frames and `-tune zerolatency`
   read -0.4 / -0.3 / -0.0. Re-run that sweep if you touch the pacing; a depth
   that moves with R means the premise has broken. Milliseconds convert at the
   *content* rate -- that is what a live pipeline pays.

   What remains is a **cost** limit, not a conditioning one: a paced run costs
   its wall time and pacing slower costs proportionally more, so past
   `MAX_PACED_SECONDS` the point is skipped as too expensive and said to be.
   `Orchestrator._latency_gated` prices it from the rate for *that preset* and
   falls back to lower bounds -- that preset's calibration, then the slowest
   preset measured there. Deliberately lower bounds, and the one place
   calibration may decide something: invariant 3 forbids gating on it because
   its low bias would wrongly call an encoder incapable, whereas here the bias
   errs towards skipping an expensive measurement rather than paying for one
   that is refused anyway, and the question is budget rather than capability.
   Gating on the *fastest* preset instead let libx265 veryslow at 1080p time out
   on four cores, proving over several minutes what its own calibration already
   knew.
   Separately, if output falls behind the pacing, the lag measured is the
   shortfall and not the pipeline (`latency._sustained`). Both endings are
   reported as text, never as a figure.

34. **The latency pass is gated on a two-sided self-check.** Like
   `quality.self_check` (invariant 21), and for the same reason. Both bounds
   are needed: `libx264 -tune zerolatency` must read <= 2 frames *and* plain
   `libx264` must read >= 10, because a one-sided check is passed by a
   measurement stuck at zero and the other by one that has drifted high. With
   no anchor encoder on the box the pass is skipped, not run unchecked.

35. **The progress period must be finer than a frame.** Delay is read from when
   `-progress` blocks arrive (`runner.ProgressTailer` stamps each one), so the
   block period is the measurement quantum, and ffmpeg's 0.5s default is
   fifteen frames at 30 fps. `-stats_period`, `-readrate` and
   `-readrate_initial_burst` are each probed with `FFmpeg.has_option` and the
   pass is skipped if any is missing -- every one of them fails quietly rather
   than loudly. Note the paced side cannot resolve finer than one frame period
   whatever the setting: with the reader blocked in the pacer, ffmpeg emits one
   block per input frame -- one *pacing* frame, so the floor grows as the pacer
   slows. That floor is in the reported error bars.

36. **Startup cost is derived from runs already made, and never used to derive
   delay.** Every configuration is run at two lengths already -- calibration
   and the timed run -- so the intercept of `rtime = startup + n/rate` is free
   (`latency.startup_costs`). Two points cannot separate init from teardown, so
   it is the whole per-invocation overhead and is reported as such. Delay
   cancels this term rather than subtracting an estimate of it; do not "simplify"
   the paired run into one run minus a startup estimate.

### Scope and budget

37. **A profile is a scope, not a stopwatch. None of them sets
   `budget_minutes`.** Plan cost scales with how many encoders the box turns out
   to have; a constant budget does not. `standard` had 25 minutes, and on a
   15-encoder box the plan came to 164 cases: 99 ran, the bitrate, framerate and
   complexity tiers never started, and because `report.render_axis` returns
   silently with fewer than two columns, three whole tables vanished from the
   report with the only trace on stderr during the run. `quick` had 6, and on a
   4-core box completed 8 of 46. A profile that is cheap only until the machine
   is slow is not cheap, it is arbitrary -- what makes `quick` quick is its axes.

   The per-test caps (`TEST_CAP`) still bound any single measurement, and
   `--time-budget` restores a wall for anyone who wants one, at which point the
   run reports what it dropped. What replaces the wall by default is a projection
   printed at the tier 0 -> 1 boundary, which is the first moment every encoder
   has been *measured* rather than merely calibrated
   (`Orchestrator._announce_projection`), and an order (invariant 30) that makes
   Ctrl-C a reasonable way to stop.

38. **A projection is for sizing, never for gating** -- the same split invariant 3
   draws for calibration, and for the same reason: `_estimate_fps` composes a
   calibration estimate biased low with `PIXEL_EXPONENT`, a fit, not a law
   (median 0.76 across 64 points, p10 0.48, p90 1.01). `_projected_seconds` must
   go through the real `_size_frames`; a projection built from its own sizing
   rule describes a run that will not happen. Decisions -- demotion, the latency
   gate, the concurrency ramp -- take `_measured_fps` and skip only on evidence.

   It must cost **everything a phase spends, not just its measurements**. Two
   omissions each moved it by a third: the calibration run that precedes every
   new `(encoder, resolution, preset)` -- and the resolution and preset sweeps
   are nothing but new keys -- and `latency.self_check`, which is two more paired
   measurements paid before a single latency row exists. Measured after both were
   added: -20% on a 24-test run against -33% before. The residual is the
   concurrency ramp under contention and source generation, neither modelled; if
   you change what a phase does, re-measure rather than trusting the number.

39. **Never spend a measurement on a number the method will already refuse to
   report.** `latency.measure` needs the encoder above `MIN_REALTIME_X` and finds
   out only after paying for a full free-running encode: 10 of 27 latency results
   on one run came back "not separable", and they were the expensive ones --
   libaom-av1 at 0.038x realtime burned ~110s per case to say it could not be
   measured. `Orchestrator._latency_gated` decides up front on a real
   measurement, the way `run_concurrency` gates its ramp. The low-latency mode
   gets a looser threshold (`LATENCY_LOWLAT_GATE_X`) because `-deadline realtime`
   and friends genuinely change throughput; the default-mode figure is only
   evidence about them when it is far below. The same argument gives an encoder
   below `LIGHT_FRACTION` of realtime **light testing** -- the full resolution
   ladder at the baseline preset, and none of the preset, bitrate, framerate or
   complexity sweeps -- **in the latency pass too** (`LIGHT_LATENCY_TIERS`). The
   two must agree: light testing is what removes the per-preset measurements,
   so a latency preset sweep on a light encoder has neither a measurement nor a
   calibration to be priced from, and runs until it times out. Seen on libx265
   at 1080p on four cores. "Does bitrate move throughput" has no audience at 1.1 fps,
   and costs six capped tests to answer. The ladder stays because how a batch
   encoder scales is the interesting thing about it, and it must stay at the
   *baseline* preset: `report._baseline_preset` filters the resolution table on
   that (invariants 17/18), so measured anywhere else it would drop out of the
   one table it is still in.

40. **Every gate above is an absence someone has to be told about.**
   `Orchestrator.coverage()` carries planned-against-ran per axis, the latency
   totals, the lightly-tested encoders and any early stop; `axis_incomplete` turns that
   into the line `render_axis` prints instead of rendering nothing, and
   `render_warnings` leads with an `incomplete run` note. This is invariant 26
   applied to whole tables rather than single tests. A skip that only reaches
   stderr has not been reported.

## Conventions

- **Stdlib only.** No pip, no venv, no third-party imports. Target **Python 3.8+**:
  no `match`, no `X | Y` annotations. `from __future__ import annotations` is fine.
- `%`-style string formatting throughout (not f-strings), for consistency with 3.8.
- Terminal output: tables and results to **stdout**, progress and diagnostics to
  **stderr**. Keep that split — piping to a file must yield a readable report.
- All ANSI colour goes through `util` helpers; width math uses `util.visible_len`
  so escape sequences don't break alignment.
- Comments explain *why*, especially where a rule looks arbitrary — most of them
  encode a measurement trap.

## Extending

**A new hardware family:** add its suffix to `probe.HW_SUFFIXES`, a variant recipe to
`probe._variants` (device init + filter chain), a diagnostic branch in
`probe.diagnose` with package hints in `PKG_HINTS`, and — if its speed knob isn't a
documented ladder — an entry in `probe.HW_SPEED_KNOB` with `knob_is_ladder=False`.

**A new metric:** add detection in `quality.detect`, a pass in `quality._score`, and
columns in both `report.render_quality` and `html_report`. It must go through
`_align` and be covered by the self-check.

**A low-latency configuration for a new encoder:** add it to
`latency.LOW_LATENCY_ARGS` (by name) or `latency.FAMILY_LOW_LATENCY_ARGS` (by
hardware family), most aggressive first. Each candidate is tried with a real
encode and the first that works is reused verbatim, so a list may end in a
narrower fallback; a row that fell back is marked partial rather than presented
as the encoder's best.

**A new profile:** add a `matrix.Profile` to `PROFILES` and a `bench.TEST_CAP` entry.
Set `latency_axes` and `latency_seconds` too: a paced run costs its wall time no
matter how fast the encoder is, so it is the one knob that does not scale with
the machine.
Per-test caps matter: an uncapped slow encoder consumes the entire budget (libaom-av1
at 2 fps once projected 2h17m for a profile advertising 25 minutes).
Leave `budget_minutes` at `None` (invariant 37): make the profile cheap through
its axes -- resolutions, framerates, bitrates, complexities, `preset_count`,
`measure_seconds` -- not through a clock. A budget on a profile whose plan grows
with the encoder count buys a predictable runtime with unpredictable coverage,
which is the worse of the two trades.

## Verification

No unit tests; verify empirically. Minimum bar after touching measurement code:

1. `./encbench --list-encoders` — hardware absent from the machine must be **rejected**
   (the single most important correctness check), and fixable failures must produce an
   actionable hint.
2. `./encbench --profile quick --encoders libx264,libsvtav1 --resolutions 1080p
   --no-concurrency --no-report-files` — presets must span a wide fps range and
   converged runs must land within ~10% of the target bitrate.
3. `--quality` on 2-3 encoders — the self-check must pass, and VMAF must rise
   monotonically with bitrate. If it doesn't, alignment is broken again.
4. Concurrency ramp on one software and one hardware encoder.
5. Ctrl-C mid-run: `pgrep ffmpeg` clean, partial report renders, `--resume` continues.
6. `--scratch-dir` on a small filesystem: refuses clearly instead of filling it.
7. Latency, against the two things the measurement claims to be true. Delay in
   *frames* must be roughly constant across resolutions while delay in *ms*
   halves from 30 to 60 fps — measured on libx264 medium at 720p: 54.0 frames /
   1801 ms at 30 fps against 53.3 frames / 888 ms at 60. If the frame count
   moves with resolution or the milliseconds do not move with framerate, the
   pacing is not doing what it is supposed to. `-tune zerolatency` must read ~0
   and `ultrafast` ~10 (frame threads alone), against ~55 for `medium`.
8. **Assert `cpu_seconds` is non-null in the JSON.** It is the cheapest possible
   canary for invariant 9: if `-benchmark` output is being swallowed again, every
   throughput figure silently reverts to wall clock and nothing else complains.
   `wall_fallback` should likewise be false for every result.
9. `python3 -m py_compile src/encbench/*.py`.

Cross-machine checks are worth the effort — deploying to a different vendor's hardware
(Intel QSV/VAAPI vs AMD VAAPI/Vulkan) and a different ffmpeg build has surfaced real
bugs that a single machine could not.

## Known quirks

- VAAPI and Vulkan on AMD drive the *same* VCN engine: identical quality scores and
  identical concurrency saturation. Not a bug — worth stating in output so users
  don't read it as a coincidence.
- `hevc_vaapi` exposes no `-quality` option even though `h264_vaapi` does. The
  "no speed knob" result is correct, not a detection failure.
- johnvansickle static builds may lack hardware encoders; BtbN builds include them.
  Never assume — `probe` re-derives capability from whichever binary is in use.
- ffmpeg `-fps_mode` requires >= 5.0; `FFmpeg.fps_mode_args` falls back to `-vsync`.
- `-r` before `-i` *does* reinterpret a raw clip's rate correctly, for the encoder
  and for the output timestamps both. It is the input *pacer* that ignores it
  (invariant 32). Worth stating because the opposite conclusion is easy to reach
  from a shell test: in zsh an unquoted `$opts` does not word-split, so a loop
  comparing `-r 30` against no options silently runs neither and compares a
  stale output file against itself.
- `-r` on the output is rejected outright alongside `-fps_mode passthrough`
  ("contradictory"), so the output framerate cannot be declared that way.
- Benchmark numbers taken on battery power are not comparable with numbers taken
  on AC: firmware and the governor cap sustained clocks harder when discharging.
  The report surfaces the governor but does not yet read `/sys/class/power_supply`
  — worth adding before trusting a cross-machine comparison from a laptop.
- Cross-machine testing earns its keep. The calibration-bias bug (invariant 3) was
  invisible on a fast workstation, where every encoder cleared realtime even with
  the bias, and only appeared on a low-power Intel box.
