# encbench

**Find out what your Linux box can actually do as a video encoder.**

`encbench` probes your system for every video encoder ffmpeg can genuinely use —
validating each with a real encode, not just reading `ffmpeg -encoders` — then
measures throughput, resolution scaling, preset tradeoffs, concurrent-stream
capacity and encode quality. Output is readable tables plus a self-contained HTML
report.

Stdlib-only Python 3.8+, one directory to copy, no install step. If ffmpeg is
missing it fetches a static build into scratch space and uses that.

## Quickstart

```bash
./encbench --list-encoders     # what can this machine do?
./encbench                     # full benchmark, 20-40 min
./encbench --profile quick     # 5-15 min
```

## Example output

```
HEADLINE
  Fastest at 1080p30:     libx264  836 fps  (27.9x realtime)  preset ultrafast
  Fastest hardware:       hevc_vulkan  362 fps  (12.1x realtime)
  Most CPU-efficient:     hevc_vulkan  390 fps per core used
  Most parallel streams:  hevc_vaapi  12 simultaneous 1080p30 streams

THROUGHPUT BY RESOLUTION
  encode fps and multiple of realtime, at 30 fps, high-complexity content

  ENCODER      TYPE  CODEC        360P       480P       720P      1080P      1440P      2160P
  ───────────  ────  ─────  ──────────  ─────────  ─────────  ─────────  ─────────  ─────────
  h264_vaapi   HW    h264    841 28.0x  597 19.9x  366 12.2x   200 6.7x   125 4.2x  60.1 2.0x
  h264_vulkan  HW    h264    961 32.0x  754 25.1x  424 14.1x   214 7.1x   126 4.2x  59.3 2.0x
  libx264      SW    h264    843 28.1x  516 17.2x   246 8.2x   116 3.9x  59.4 2.0x  23.1 0.8x
  hevc_vaapi   HW    hevc    976 32.5x  896 29.9x  489 16.3x  321 10.7x   206 6.9x  95.9 3.2x
  hevc_vulkan  HW    hevc   1110 37.0x  968 32.3x  650 21.7x  361 12.0x   204 6.8x  94.2 3.1x
  libx265      SW    hevc     154 5.1x   108 3.6x  72.2 2.4x  51.9 1.7x  33.5 1.1x  15.4 0.5x

CONCURRENT STREAM CAPACITY
  identical 1080p30 streams launched together; 'realtime streams' is the most that ALL held 30 fps

  ENCODER      TYPE  REALTIME STREAMS  PEAK AGG FPS                    LIMIT
  ───────────  ────  ────────────────  ────────────  ────────────────  ───────────────
  hevc_vaapi   HW                  12           411  ████████████████  saturated at 16
  hevc_vulkan  HW                  12           417  ████████████████  saturated at 16
  h264_vaapi   HW                   6           238  █████████·······  saturated at 8
  h264_vulkan  HW                   6           242  █████████·······  saturated at 8
  libx264      SW                   4           173  ███████·········  saturated at 6
  libx265      SW                   2          80.0  ███·············  saturated at 3

ENCODE LATENCY
  input paced at realtime; delay is how many frames the encoder holds before its
  first packet emerges - lookahead plus frame reordering

  ENCODER      TYPE  POINT               MODE         DELAY fr    DELAY  WORST fr  FRAME TIME
  ───────────  ────  ──────────────────  ───────────  ────────  ───────  ────────  ──────────
  h264_vaapi   HW    1080p30             default           4.0    133 ms      +0.4     2.81 ms
               HW    1080p30             low-latency       1.0     33 ms      +0.2     3.02 ms
  libx264      SW    1080p30  fast       default          41.8   1393 ms      +3.2    11.29 ms
               SW    1080p30  fast       low-latency       1.0     33 ms      +0.2    16.30 ms
               SW    1080p30  ultrafast  default          10.5    350 ms      +1.0     1.75 ms

QUALITY  (--quality)
  ENCODER         TARGET     ACTUAL  VMAF    SSIM  PSNR dB   FPS
  ───────────  ─────────  ─────────  ────  ──────  ───────  ────
  libx264      12.0 Mbps  11.7 Mbps  89.2  0.8750    36.41   198
  libx265      12.0 Mbps  13.6 Mbps  91.0  0.8770    36.89  89.3
  h264_vaapi   12.0 Mbps  13.7 Mbps  92.4  0.8780    37.10   314
  hevc_vaapi   12.0 Mbps  13.5 Mbps  91.3  0.8780    36.82  43.0
```

Here hardware is ~2.5x faster than software and slightly worse per bit; `--quality`
measures that tradeoff for your own content. And the fastest encoder in the first
table adds 1.4 seconds of delay at its default settings, which is the whole reason
the latency table exists.

## What it measures

| | |
|---|---|
| **Throughput** | encode fps and multiple of realtime |
| **Resolution scaling** | 360p → 2160p; where each encoder drops below realtime |
| **Preset tradeoff** | each encoder's own speed knob, sampled fast to slow |
| **Bitrate & framerate** | whether either actually moves throughput |
| **Content complexity** | smooth/low-motion vs dense/high-motion footage |
| **Concurrent capacity** | most streams where *every* stream holds realtime |
| **Latency** | frames held before a packet comes out, and what low-latency mode costs — for slow encoders too |
| **Startup cost** | fixed overhead of one ffmpeg invocation |
| **Quality** (`--quality`) | PSNR, SSIM and VMAF against the source |
| **CPU cost** | CPU seconds, cores used, peak RSS, fps per core |

It holds a 1080p30 target-bitrate baseline and varies one dimension at a time, so
each result isolates one variable and the run actually finishes. An encoder far
below realtime at that baseline — libaom-av1 at 1.1 fps, say — gets **light
testing**: the whole resolution ladder, so you still see how it scales, and none
of the preset, bitrate, framerate or complexity sweeps. Asking whether bitrate
moves the throughput of a 1.1 fps encoder costs minutes and answers nothing. The
report names every encoder treated that way, with the rate that decided it.

## Requirements

- Linux, Python 3.8+
- ffmpeg — or network access once, to fetch a static build
- Scratch space: ~1 GB for `quick`, ~3 GB for `standard`

Scratch defaults to `$TMPDIR` or `/tmp`; if that is a small tmpfs it relocates to
`/var/tmp` rather than consuming RAM, and refuses with a clear message rather than
filling a filesystem.

## Usage

**Profiles** — three scopes, not three stopwatches:

| | measures | typical |
|---|---|---|
| `--profile quick` | 720p/1080p, 30 fps, one bitrate, 2 presets | 5-15 min |
| `standard` (default) | 360p→2160p, 30/60 fps, 3 bitrates, 3 presets, both content classes | 20-40 min |
| `deep` | the above as a full cross product, 3 repeats, plus quality | hours |

None of them stops on a clock, so runtime depends on how many encoders the
machine turns out to have and how fast it is — `quick` measured 6 min on a
laptop with 9 encoders and 11 min on a 4-core box with 13. Each prints a projected total once it has calibrated
them, and **encoders run in order of how widely they are used** — hardware
h264/hevc first, then x264/x265, then AV1, with the reference encoders last. Stop
a run early with Ctrl-C and it has already covered what most people came for; the
report still renders and the `.jsonl` is resumable with `--resume`.

Use `--time-budget MINUTES` to impose a wall. Anything dropped is then named in
the report rather than silently missing.

**ffmpeg / scratch**

```
--ffmpeg PATH                  use this ffmpeg binary
--download / --no-download     force, or forbid, fetching a static build
--scratch-dir DIR              where clips and the ffmpeg cache live
--clean                        delete cached clips when finished
```

**Discovery & scope**

```
--list-encoders               probe, print the inventory, and exit
--encoders A,B / --exclude A,B  restrict to, or skip, named encoders
--hw-only / --sw-only          hardware or software only
--all-encoders                 every video encoder, not just delivery codecs
--extended-codecs              also include mpeg2/mpeg4/vvc/theora/prores
```

**Workload**

```
--time-budget MINUTES         stop starting new tests after this long
                              (no profile sets one; this is the only limit)
--resolutions 720p,1080p      360p 480p 720p 1080p 1440p 2160p
--fps 30,60                   framerate targets
--bitrates low,target,high    rungs of the bitrate ladder
--complexity low,high         content classes
--presets N                   speed-knob values sampled per encoder
--repeats N                   timed runs per config; the median is reported
--frames N                    fixed frame count instead of auto-sizing
--cooldown SECONDS            pause between tests
```

**Measurements**

```
--quality                     add PSNR/SSIM/VMAF (slower, opt-in)
--no-concurrency              skip the parallel-stream ramp
--concurrency-max N           highest stream count to try
--no-latency                  skip the encode-latency pass
--source FILE                 use your own footage
--source-format raw|lossless  raw removes the decode ceiling, lossless saves space
```

**Output**

```
--json PATH / --html PATH     where to write results
--no-report-files             terminal output only
--resume FILE.jsonl           continue an interrupted run
--compare A.json B.json       diff two runs
-v / -q / --no-color / --version
```

## Output files

Every run writes to `results/`:

- **`<host>-<timestamp>.json`** — full results, system profile, ffmpeg identity
- **`<host>-<timestamp>.html`** — self-contained report with charts
- **`<host>-<timestamp>.jsonl`** — appended per test, so `--resume` loses nothing

```bash
./encbench --compare results/amd.json results/intel.json
```

## Interpreting the results

- **Realtime multiple beats raw fps.** `2.0x` at 1080p30 means the box can run
  that stream twice over — the number that maps to camera count or transcode
  headroom.
- **Hardware is not automatically better.** Usually far faster, often worse per
  bit. `--quality` settles it for your content.
- **Fast and low-latency are different questions.** Delay is a frame count, not a
  time: an encoder holding 40 frames adds 1.3 s at 30 fps and 0.7 s at 60. It is
  paid on top of the encode, and again by the decoder, so for anything live read
  the latency table before the throughput one.
- **Encoders missing from the latency table are missing on cost, not on
  principle.** Delay comes from the difference between a paced and an unpaced
  run, so the input is paced slowly enough that the two stay distinguishable —
  slower than realtime for a slow encoder, which is why even sub-realtime
  encoders get a figure. What that buys is wall time, and past a cap the point
  is skipped and named in the notes. Delay is a frame count either way; the
  milliseconds are what that many frames cost at the framerate you actually run.
- **VAAPI and Vulkan on AMD are one engine.** Byte-identical quality, identical
  saturation point — the choice is about integration, not capability.
- **`short run`** in the bitrate column means under 10s of encoded video, where
  one-pass rate control hasn't converged; not a real measurement.
- **`decode-bound`** means the test approached the source's decode rate and is
  measuring the decoder, not the encoder.
- **Cross-machine numbers only compare on the same ffmpeg build** — its version
  and origin are recorded in every report.
- **Watch the warnings block.** A `powersave` governor, thermal throttling or a
  container CPU quota all mean the numbers understate the machine.

## Methodology

- Test footage is generated locally from deterministic filter graphs — no
  downloads, identical on every machine.
- Sources are stored raw where there's room, so a compressed source's decode
  ceiling never caps a fast hardware encoder.
- Throughput is frames ÷ ffmpeg's own `-benchmark` run time, output muxed to
  `/dev/null` so storage speed never enters the measurement.
- Latency is the difference between two runs of the same command, one with its
  input paced. Startup cost cancels rather than being estimated, and the whole
  pass is gated on reproducing two delays that are already known. The pacing
  rate adapts to the encoder, which is what lets a sub-realtime encoder be
  measured at all — verified against libx264 at three pacing rates, where the
  frame delay held to within ~10%.

Full methodology and the reasoning behind each rule:
[AGENTS.md](AGENTS.md#invariants).
