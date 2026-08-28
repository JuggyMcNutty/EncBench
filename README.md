# encbench

**Find out what your Linux box can actually do as a video encoder.**

`encbench` discovers every video encoder your system can genuinely use, then measures
throughput, resolution scaling, preset tradeoffs, concurrent stream capacity and
encode quality — and prints it as readable tables plus a self-contained HTML report.

One file to copy, no dependencies beyond Python 3.8+. If ffmpeg is missing it fetches
a static build into scratch space and uses that.

```bash
./encbench --list-encoders     # what can this machine do?
./encbench                     # full benchmark, ~15-25 min
./encbench --profile quick     # ~3-5 min
```

---

## What you get

```
HEADLINE
  Fastest at 1080p30:     libx264  836 fps  (27.9x realtime)  preset ultrafast
  Fastest hardware:       hevc_vulkan  362 fps  (12.1x realtime)
  Most CPU-efficient:     hevc_vulkan  390 fps per core used
  Most parallel streams:  hevc_vaapi  12 simultaneous 1080p30 streams

THROUGHPUT BY RESOLUTION
  encode fps and multiple of realtime, at 30 fps, high-complexity content, target bitrate

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
```

Plus a `--quality` pass that answers the question raw speed can't:

```
  ENCODER         TARGET     ACTUAL  VMAF    SSIM  PSNR dB   FPS
  ───────────  ─────────  ─────────  ────  ──────  ───────  ────  ──────────────
               12.0 Mbps  11.7 Mbps  89.2  0.8750    36.41   198  ██████████████
               12.0 Mbps  11.8 Mbps  89.2  0.8750    36.41   211  ██████████████
               12.0 Mbps  13.6 Mbps  91.0  0.8770    36.89  89.3  ██████████████
               12.0 Mbps  13.7 Mbps  92.4  0.8780    37.10   314  ██████████████
               12.0 Mbps  13.7 Mbps  92.4  0.8780    37.10   345  ██████████████
               12.0 Mbps  13.5 Mbps  91.3  0.8780    36.82  43.0  ██████████████
```

Hardware is ~2.5x faster here and slightly *worse* per bit. That tradeoff is the
whole point of measuring it.

---

## Why not just run ffmpeg yourself

Three things quietly make a hand-rolled benchmark wrong.

### `ffmpeg -encoders` lies about your hardware

`h264_nvenc`, `h264_qsv`, `h264_amf` and `h264_v4l2m2m` are listed by any full ffmpeg
build whether or not the silicon exists. encbench runs a real two-frame encode against
every candidate and believes only the survivors — and when a failure is *fixable*, it
says how:

```
! Hardware encoding is available but not usable yet (VA-API)
    An AMD GPU is present at /dev/dri/renderD128, but ffmpeg cannot load the
    VA-API runtime (libva). The VA driver itself is already installed, so this
    is the only missing piece.
    Fix:  sudo pacman -S libva libva-mesa-driver
```

It also separates "your hardware can't do this" from "you're missing a package".
An AMD RDNA2 card reports *"VAAPI: av1 not supported by this hardware"* — because
that engine really has no AV1 encoder — rather than an opaque linker error.

### Fixed-length runs measure the wrong thing

x264 `ultrafast` is roughly **eighty times** faster than `veryslow`. Any fixed frame
count is either instantaneous for one or interminable for the other. Every timed run
here is calibrated first, then sized so the measurement lasts a consistent wall time.

### Encoder defaults aren't comparable

GOP length is pinned to twice the framerate, so encoders with wildly different
defaults (x264 250, libvpx 9999) are judged on the same keyframe cadence.

---

## What it measures

| | |
|---|---|
| **Throughput** | encode fps and multiple of realtime |
| **Resolution scaling** | 360p → 2160p; where each encoder drops below realtime |
| **Preset tradeoff** | each encoder's own speed knob, sampled fast to slow |
| **Bitrate & framerate** | whether either actually moves throughput |
| **Content complexity** | smooth/low-motion vs dense/high-motion footage |
| **Concurrent capacity** | most streams where *every* stream holds realtime |
| **Quality** (`--quality`) | PSNR, SSIM and VMAF against the source |
| **CPU cost** | CPU seconds, cores used, peak RSS, fps per core |

Instead of a full cross product (thousands of runs, many hours), the plan is a set of
**axis sweeps around a baseline**: hold everything at 1080p30 target bitrate, then vary
one dimension at a time. Each result isolates one variable, and the run actually finishes.

---

## Usage

**Depth**

```
--profile quick|standard|deep   quick ~3-5 min, standard ~15-25 min (default), deep hours
--time-budget MINUTES           stop starting new tests after this long
--repeats N                     timed runs per config; the median is reported
--presets N                     how many speed-knob values to sample per encoder
--cooldown SECONDS              pause between tests (kind to passively-cooled boxes)
```

**Scope**

```
--encoders libx264,libsvtav1    only these
--exclude libaom-av1            skip these
--hw-only / --sw-only           hardware or software only
--resolutions 720p,1080p        360p 480p 720p 1080p 1440p 2160p
--fps 30,60                     framerate targets
--bitrates low,target,high      rungs of the bitrate ladder
--complexity low,high           content classes
--all-encoders                  every video encoder, not just delivery codecs
--extended-codecs               also include mpeg2/mpeg4/vvc/theora/prores
```

**Measurements**

```
--quality                       add PSNR/SSIM/VMAF (slower, opt-in)
--no-concurrency                skip the parallel-stream ramp
--concurrency-max N             highest stream count to try
--source FILE                   use your own footage
--source-format raw|lossless    raw removes the decode ceiling, lossless saves space
--frames N                      fixed frame count instead of auto-sizing
```

**ffmpeg and scratch**

```
--ffmpeg PATH                   use a specific binary
--download / --no-download      force or forbid fetching a static build
--scratch-dir DIR               where clips and the ffmpeg cache live
--clean                         delete cached clips when finished
```

**Output**

```
--json PATH / --html PATH       where to write results
--compare A.json B.json         diff two runs
--resume FILE.jsonl             continue an interrupted run
--no-report-files               terminal output only
--version
-v / --verbose, -q / --quiet, --no-color
```

---

## Output files

Every run writes three files to `results/`:

- **`<host>-<timestamp>.json`** — full results, system profile, ffmpeg identity
- **`<host>-<timestamp>.html`** — self-contained report with charts, opens anywhere
- **`<host>-<timestamp>.jsonl`** — appended as each test finishes, so an interrupted
  run loses nothing and `--resume` picks it up

Compare two machines, or the same machine before and after a driver change:

```bash
./encbench --compare results/amd.json results/intel.json
```

---

## Requirements

- Linux, Python 3.8+
- ffmpeg — or network access once, to fetch a static build
- Scratch space: ~1 GB for `quick`, ~3 GB for `standard`

Scratch defaults to `$TMPDIR` or `/tmp`. **If that's a small tmpfs it relocates to
`/var/tmp` rather than eating your RAM**, and refuses with a clear message rather than
filling a filesystem. (On a 7.6 GB test box with a 3.8 GB `/tmp` tmpfs, it correctly
moved to btrfs on `/var/tmp`.)

---

## How to read the results

- **Realtime multiple beats raw fps.** `2.0x` at 1080p30 means the box can do that
  stream twice over. It's the number that tells you how many cameras, or how much
  faster than playback your transcode will run.
- **Hardware is not automatically better.** It's usually far faster and often worse
  per bit. `--quality` settles it for *your* content.
- **Multiple API paths may be the same silicon.** On AMD, VAAPI and Vulkan produce
  byte-identical quality and saturate at the same stream count — they're two
  front-ends to one VCN engine. The tool shows both; the choice is about integration,
  not capability.
- **Watch the notes section.** A `powersave` governor, thermal throttling or a
  container CPU quota all mean the numbers understate the machine, and each is
  reported alongside the results.
- **Cross-machine numbers are only comparable on the same ffmpeg build**, which is why
  the build's version and origin are recorded in every report.

---

## Methodology

Documented in full in [AGENTS.md](AGENTS.md#invariants), but the short version:

Test footage is generated locally from deterministic filter graphs with fixed seeds —
no downloads, no licensing, identical on every machine. Sources are stored **raw** when
there's room, because a compressed source imposes a decode ceiling and a fast hardware
encoder can end up measuring the *decoder*; anything that approaches that ceiling is
flagged `decode-bound` rather than reported as the encoder's limit.

Throughput is frames divided by ffmpeg's own `-benchmark` run time, with counters read
from `-progress` rather than scraped from stderr. Output is muxed to `/dev/null`, so
storage speed never enters the measurement.

Bitrate accuracy is only reported for runs of **at least 10 seconds of encoded video**.
Below that one-pass rate control hasn't converged — measured on this content, a libx264
1080p30 run lands +70% at 4s, +18% at 8s, +9% at 10s and +4% at 15s. Shorter runs show
`short run` instead of a misleading number.

Quality compares against the exact clip that fed the encoder, pairing streams **by frame
index rather than timestamp** — the default pairing silently compares neighbouring frames
and returns plausible-but-wrong numbers. The pipeline self-checks before every quality
pass by encoding losslessly and confirming a perfect score; if that fails, the quality
pass is skipped rather than reporting bad data.
