"""Encode latency: the delay between a frame entering the pipeline and its
packet leaving it.

Throughput and latency are different questions and the fastest encoder is
routinely the wrong answer to the second one. libx264 at default settings holds
roughly forty frames of lookahead, so at 1080p30 it adds well over a second of
delay while measuring 800 fps; a hardware encoder a third as fast may add four
frames. Nothing in a throughput table distinguishes them.

**Delay is measured against paced input.** On a free-running encode a
forty-frame lookahead at 800 fps is fifty milliseconds -- smaller than the
run-to-run spread of VAAPI device initialisation, so the number measured would
be mostly noise. Pacing the input stretches those same forty frames to well over
a second, an order of magnitude clear of it.

The measurement is a *pair* of runs of the same command, one paced and one not.
With P the fixed pre-first-frame cost (spawn, device init, filter graph), D the
delay in frames, E the encoder's sustained rate and R the pacing rate:

    t_unpaced = P + D/E
    t_paced   = P + D/R
    delta     = t_paced - t_unpaced = D * (1/R - 1/E)
    =>  D = delta / (1/R - 1/E)

P cancels exactly. That is the whole point of running the pair: no init estimate
is needed, and hardware device-init cost -- the largest and least repeatable
term -- cannot contaminate the result. The price is that the difference is
ill-conditioned as E approaches R (the error in delta is amplified by E/(E-R)),
is undefined at E = R, and inverts below it.

**R is not necessarily the content framerate.** It was, and that made the method
unusable for any encoder below ~2x realtime -- which on a four-core box is most
of them, including the libx264 `medium` the self-check depends on. Since D is a
frame count and R only sets how fast frames are offered, pacing *slower* buys
back the margin for any encoder at all (`pace_for`). Verified on libx264 720p at
R = 30, 15 and 7.5 fps: `medium` read 62.5, 60.0 and 56.3 frames and
`-tune zerolatency` read -0.4, -0.3 and -0.0. Milliseconds still convert at the
content rate -- that is what a live pipeline actually pays.
"""

from __future__ import annotations

import time

from . import runner, util
from .util import detail

# How aggressively each encoder can be told to stop buffering, most aggressive
# first. Tried in order and validated with a real encode before use, because an
# encoder that rejects an option fails at runtime, not at parse time -- the same
# reason probe.py never trusts `ffmpeg -encoders`. A row that fell back to a
# narrower set is marked partial rather than presented as a full low-latency
# configuration.
LOW_LATENCY_ARGS = {
    "libx264":    [["-tune", "zerolatency"]],
    "libx265":    [["-tune", "zerolatency"]],
    "libx262":    [["-tune", "zerolatency"]],
    "libsvtav1":  [["-svtav1-params", "pred-struct=1:lookahead=0"],
                   ["-svtav1-params", "pred-struct=1"]],
    "libvpx":     [["-lag-in-frames", "0", "-deadline", "realtime"],
                   ["-lag-in-frames", "0"]],
    "libvpx-vp9": [["-lag-in-frames", "0", "-deadline", "realtime"],
                   ["-lag-in-frames", "0"]],
    "libaom-av1": [["-lag-in-frames", "0", "-usage", "realtime"],
                   ["-lag-in-frames", "0"]],
    "librav1e":   [["-rav1e-params", "low_latency=true"]],
}

FAMILY_LOW_LATENCY_ARGS = {
    "vaapi": [["-async_depth", "1", "-bf", "0"], ["-bf", "0"]],
    "nvenc": [["-delay", "0", "-rc-lookahead", "0", "-bf", "0"], ["-bf", "0"]],
    "qsv":   [["-async_depth", "1", "-look_ahead", "0", "-bf", "0"],
              ["-async_depth", "1", "-bf", "0"], ["-bf", "0"]],
    "amf":   [["-usage", "lowlatency", "-bf", "0"], ["-bf", "0"]],
}

DEFAULT_LOW_LATENCY_ARGS = [["-bf", "0"]]

# Self-check anchors. libx264 -tune zerolatency has no frame delay by
# construction (no lookahead, no B-frames); plain libx264 has rc-lookahead 40
# and B-frames enabled. Both bounds are checked -- a one-sided check is passed
# by a measurement that always returns zero.
ANCHOR_ENCODERS = ("libx264", "libx265")
# -tune zerolatency alone is enough for both; x264 also exposes the two knobs
# directly, so pin them there and leave x265's inside its own params string.
ANCHOR_ZERO_ARGS = {
    "libx264": ["-tune", "zerolatency", "-bf", "0", "-rc-lookahead", "0"],
    "libx265": ["-tune", "zerolatency"],
}
ANCHOR_ZERO_MAX_FRAMES = 2.0
ANCHOR_DEEP_MIN_FRAMES = 10.0

# The encoder must outrun the *pacing* rate by this much, or the paced and
# unpaced runs are too alike for their difference to mean anything (see the
# module docstring on conditioning). It is a ratio against the pacing rate, not
# against realtime: pacing is chosen to satisfy it (see `pace_for`).
MIN_REALTIME_X = 2.0
# Above 2x but below this, report the number and say it is soft.
CONFIDENT_REALTIME_X = 4.0

# The paced run costs its wall time no matter how fast the encoder is, and
# pacing below the content rate makes it cost more. Past this the measurement is
# not worth what it costs, which is a budget decision and reported as one --
# unlike conditioning, which is the method saying it cannot answer at all.
MAX_PACED_SECONDS = 60.0

# The paced run must actually track the pacing; if output falls behind, the lag
# measured is the shortfall, not the pipeline.
SUSTAINED_SLOPE = 0.98


def pace_for(encode_fps, fps):
    """Input rate for the paced run: the content rate, or slow enough to measure.

    Delay is solved from `(1/pace - 1/E)`, which is ill-conditioned as E nears
    the pacing rate and *undefined* at or below it -- so an encoder that cannot
    reach realtime cannot be measured against realtime-paced input at all, and
    no threshold makes it so. Nothing requires the pacer to run at the content
    rate, though: pacing slower restores the margin for any encoder.

    Delay is a frame count either way. Measured on libx264 720p at pacing rates
    of 30, 15 and 7.5 fps: `medium` read 62.5, 60.0 and 56.3 frames, and
    `-tune zerolatency` read -0.4, -0.3 and -0.0. Depth is a property of the
    configuration, not of how fast frames arrive.
    """
    if encode_fps <= 0:
        return fps
    return min(float(fps), encode_fps / MIN_REALTIME_X)


def stats_period_for(fps):
    """Progress-block period: a quarter of a frame, and never coarser.

    Delay is read from the arrival time of progress blocks, so the block period
    is the measurement quantum. ffmpeg's 0.5s default is fifteen frames at
    30 fps -- larger than most of the delays being measured.
    """
    return min(0.02, 1.0 / (8.0 * float(fps)))


class LatencyResult(object):
    def __init__(self, case, mode="default", axis="baseline"):
        self.case = case
        self.mode = mode                 # 'default' | 'lowlat'
        self.axis = axis
        self.ok = False
        self.error = ""
        self.mode_args = []
        self.partial_mode = False        # a fallback arg set was used
        self.encode_fps = None
        self.realtime_x = None
        self.frame_time_ms = None
        self.delay_frames = None
        self.delay_ms = None
        self.delay_frames_raw = None
        self.delay_error_frames = None
        self.worst_excursion_frames = None
        self.worst_excursion_ms = None
        self.low_confidence = False
        self.stats_period = None
        self.paced_seconds = None
        self.pace_fps = None             # input rate of the paced run

    def as_dict(self):
        d = self.case.as_dict()
        d.update({
            "mode": self.mode, "axis": self.axis,
            "ok": self.ok, "error": self.error,
            "mode_args": self.mode_args, "partial_mode": self.partial_mode,
            "encode_fps": _round(self.encode_fps, 2),
            "realtime_x": _round(self.realtime_x, 3),
            "frame_time_ms": _round(self.frame_time_ms, 3),
            "delay_frames": _round(self.delay_frames, 2),
            "delay_ms": _round(self.delay_ms, 1),
            "delay_frames_raw": _round(self.delay_frames_raw, 2),
            "delay_error_frames": _round(self.delay_error_frames, 2),
            "worst_excursion_frames": _round(self.worst_excursion_frames, 2),
            "worst_excursion_ms": _round(self.worst_excursion_ms, 1),
            "low_confidence": self.low_confidence,
            "stats_period": self.stats_period,
            "paced_seconds": self.paced_seconds,
            "pace_fps": _round(self.pace_fps, 3),
        })
        return d


def _round(v, n):
    return None if v is None else round(v, n)


# --------------------------------------------------------------------------
# one run of the pair
# --------------------------------------------------------------------------

class _Run(object):
    def __init__(self):
        self.ok = False
        self.error = ""
        self.samples = []
        self.frames = 0
        self.rtime = None
        self.wall = None


def _run_once(ff, case, clip, extra_args, paced, stats_period, timeout,
              pace_fps=None):
    """One encode, following -progress live so output timing is observable."""
    run_info = _Run()
    prog = runner.progress_file(clip)
    cmd = runner.build_command(ff, case, clip, extra_args=extra_args,
                               progress_path=prog, paced=paced,
                               stats_period=stats_period, pace_fps=pace_fps)
    start = time.monotonic()
    tailer = runner.ProgressTailer(
        prog, start, interval=min(0.004, (stats_period or 0.5) / 2.0))
    tailer.start()
    try:
        proc = runner.launch(cmd)
        rc, _out, err, timed_out = runner.collect(proc, timeout)
    finally:
        # Stop the tailer before draining: drain_progress removes the file.
        tailer.stop()
        progress_text = runner.drain_progress(prog)
    run_info.wall = time.monotonic() - start
    run_info.samples = tailer.samples

    if timed_out:
        run_info.error = "timed out"
        return run_info
    if rc != 0:
        run_info.error = runner.first_error_line(err) or ("exit %s" % rc)
        return run_info

    progress = runner.parse_progress(progress_text)
    bench = runner.parse_benchmark(err)
    try:
        run_info.frames = int(progress.get("frame", 0))
    except ValueError:
        run_info.frames = 0
    run_info.rtime = bench.get("rtime")
    if not run_info.frames:
        run_info.error = "no usable progress output"
        return run_info
    run_info.ok = True
    return run_info


def _first_output(samples):
    """(seconds since launch, frames out) of the first block reporting output."""
    for seconds, frames in samples:
        if frames >= 1:
            return seconds, frames
    return None, None


def _steady_fps(samples):
    """Sustained output rate from the second half of a run's samples.

    Preferred over frames/rtime because rtime includes ffmpeg's own startup,
    which drags the average down on a short run -- the same bias invariant 3
    describes for calibration.
    """
    points = [(t, f) for (t, f) in samples if f >= 1]
    if len(points) < 6:
        return None
    window = points[len(points) // 2:]
    if len(window) < 3:
        return None
    span = window[-1][0] - window[0][0]
    produced = window[-1][1] - window[0][1]
    if span <= 0 or produced <= 0:
        return None
    return produced / span


def _excursion(samples, first_t, first_frames, pace):
    """Worst lag behind the run's own steady schedule, in frames.

    Once paced, output advances at exactly fps; a stall (an I-frame burst, a
    driver hiccup) shows up as the counter falling behind and catching up. That
    transient is what a live pipeline sees as a glitch, and it is independent of
    the absolute delay, so it needs no knowledge of when input started.
    """
    worst = 0.0
    for seconds, frames in samples:
        if frames < 1 or seconds <= first_t:
            continue
        expected = first_frames + pace * (seconds - first_t)
        worst = max(worst, expected - frames)
    return worst


def _sustained(samples, first_t, pace):
    """Did output track the pacing all the way to the end?"""
    points = [(t, f) for (t, f) in samples if f >= 1 and t >= first_t]
    if len(points) < 8:
        return None                     # too few samples to judge either way
    window = points[len(points) // 2:]
    span = window[-1][0] - window[0][0]
    produced = window[-1][1] - window[0][1]
    if span <= 0:
        return None
    return (produced / span) >= (SUSTAINED_SLOPE * pace)


# --------------------------------------------------------------------------
# the measurement
# --------------------------------------------------------------------------

def measure(ff, case, clip, mode="default", mode_args=None, paced_seconds=4.0,
            axis="baseline", partial_mode=False):
    """Run the paced/unpaced pair and turn it into a LatencyResult."""
    fps = float(case.fps)
    period = stats_period_for(fps)
    result = LatencyResult(case, mode, axis)
    result.mode_args = list(mode_args or [])
    result.partial_mode = partial_mode
    result.stats_period = period
    result.paced_seconds = paced_seconds

    # Both runs use the same frame count so the two commands differ only by
    # -re; that identity is what lets the fixed startup cost cancel.
    case.frames = max(90, int(round(fps * paced_seconds)))

    free = _run_once(ff, case, clip, mode_args, False, period,
                     runner.estimate_timeout(case, clip))
    if not free.ok:
        result.error = free.error
        return result

    encode_fps = _steady_fps(free.samples)
    if encode_fps is None and free.rtime:
        encode_fps = free.frames / free.rtime
    if not encode_fps:
        result.error = "could not measure sustained encode rate"
        return result
    result.encode_fps = encode_fps
    result.realtime_x = encode_fps / fps
    result.frame_time_ms = 1000.0 / encode_fps

    # Pace the input slowly enough that the difference of the two runs stays
    # separable. For anything comfortably above realtime this is the content
    # rate and nothing changes; below it, the pacer slows down instead of the
    # measurement giving up.
    pace = pace_for(encode_fps, fps)
    result.pace_fps = pace
    if pace <= 0:
        result.error = "could not choose a pacing rate"
        return result
    result.low_confidence = (encode_fps / pace) < CONFIDENT_REALTIME_X

    # A paced run costs its wall time, and pacing below the content rate costs
    # proportionally more. Refuse on price rather than on principle, and say so.
    paced_seconds_needed = case.frames / pace
    if paced_seconds_needed > MAX_PACED_SECONDS:
        result.error = ("would need %s of realtime-paced input at %.2g fps to "
                        "measure (cap %s)"
                        % (util.human_time(paced_seconds_needed), pace,
                           util.human_time(MAX_PACED_SECONDS)))
        return result

    paced = _run_once(ff, case, clip, mode_args, True, period,
                      paced_seconds_needed * 2.0 + 90.0, pace_fps=pace)
    if not paced.ok:
        result.error = paced.error
        return result

    t_free, n_free = _first_output(free.samples)
    t_paced, n_paced = _first_output(paced.samples)
    if t_free is None or t_paced is None:
        result.error = "no output frame observed during the run"
        return result
    # If the first block reporting output already reports most of the run, the
    # encoder emitted everything at flush: the delay is longer than the window
    # measured, and the arithmetic below would be measuring the window instead.
    if n_paced > case.frames * 0.5 or n_free > case.frames * 0.5:
        result.error = "delay exceeds the measurement window"
        return result

    ok_sustained = _sustained(paced.samples, t_paced, pace)
    if ok_sustained is False:
        result.error = "output fell behind the pacing"
        return result

    denom = (1.0 / pace) - (1.0 / encode_fps)
    if denom <= 0:
        result.error = "cannot sustain the pacing rate"
        return result

    raw = (t_paced - t_free) / denom
    result.delay_frames_raw = raw
    # A genuinely zero-delay encoder scatters either side of zero; clamp for
    # display but keep the raw value in the JSON, because a large negative one
    # means the method broke, not that the encoder is prescient.
    result.delay_frames = max(0.0, raw)
    # Milliseconds convert at the *content* rate, not the pacing rate: the delay
    # is a frame count, and what a live pipeline pays for it is that many frames
    # at the framerate it actually runs.
    result.delay_ms = 1000.0 * result.delay_frames / fps
    # Error bars. The unpaced side is quantised by the progress period, but the
    # paced side cannot beat one frame period however small that is: with -re,
    # ffmpeg's main loop blocks in the input reader, so it emits a progress
    # block once per input frame and no more often.
    result.delay_error_frames = (max(period, 1.0 / pace) + period) / denom

    result.worst_excursion_frames = _excursion(paced.samples, t_paced, n_paced, pace)
    result.worst_excursion_ms = 1000.0 * result.worst_excursion_frames / fps
    result.ok = True
    return result


# --------------------------------------------------------------------------
# low-latency configuration
# --------------------------------------------------------------------------

def low_latency_candidates(spec):
    if spec.name in LOW_LATENCY_ARGS:
        return LOW_LATENCY_ARGS[spec.name]
    if spec.family in FAMILY_LOW_LATENCY_ARGS:
        return FAMILY_LOW_LATENCY_ARGS[spec.family]
    return DEFAULT_LOW_LATENCY_ARGS


def validate_low_latency(ff, spec, case, clip):
    """The most aggressive low-latency arguments this encoder actually accepts.

    Validated with a real encode and then reused verbatim, the same discipline
    probe._validate applies to the encoder itself: an unsupported option is a
    runtime failure, not a parse error, so trying it is the only way to know.
    Returns (args, partial) or (None, False) when nothing is accepted.
    """
    candidates = low_latency_candidates(spec)
    probe_case = runner.TestCase(
        encoder=spec, res_key=case.res_key, width=case.width, height=case.height,
        fps=case.fps, complexity=case.complexity, frames=12,
        bitrate=case.bitrate, preset=case.preset, rate_mode="bitrate",
        kind="latency-probe")
    for index, args in enumerate(candidates):
        attempt = _run_once(ff, probe_case, clip, args, False, None, 120.0)
        if attempt.ok:
            if index:
                detail("%s rejected the full low-latency set; using %s"
                       % (spec.name, " ".join(args)))
            return args, bool(index)
    detail("%s accepts no low-latency configuration" % spec.name)
    return None, False


# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------

def self_check(ff, specs, clip, fps, paced_seconds):
    """Verify the method against two known answers before trusting any of it.

    Mirrors quality.self_check: a measurement that cannot reproduce a value we
    already know must not be reported. Both ends are checked -- a lower bound
    alone is satisfied by a measurement stuck at zero, and an upper bound alone
    by one that has drifted high.
    """
    anchor = None
    for name in ANCHOR_ENCODERS:
        for spec in specs:
            if spec.name == name and spec.ok:
                anchor = spec
                break
        if anchor:
            break
    if anchor is None:
        return False, ("no anchor encoder available (%s); the method cannot be "
                       "checked against a known answer"
                       % ", ".join(ANCHOR_ENCODERS))

    from .matrix import bitrate_for

    def anchor_case(preset):
        return runner.TestCase(
            encoder=anchor, res_key=clip.res_key, width=clip.width,
            height=clip.height, fps=fps, complexity=clip.complexity, frames=0,
            bitrate=bitrate_for(clip.res_key, "target"), preset=preset,
            rate_mode="bitrate", kind="latency-selfcheck")

    zero = measure(ff, anchor_case("veryfast"), clip, mode="lowlat",
                   mode_args=ANCHOR_ZERO_ARGS[anchor.name],
                   paced_seconds=paced_seconds)
    if not zero.ok:
        return False, "zero-delay anchor did not run (%s)" % (zero.error or "?")
    if zero.delay_frames > ANCHOR_ZERO_MAX_FRAMES:
        return False, ("%s with no lookahead and no B-frames measured %.1f frames "
                       "of delay instead of ~0" % (anchor.name, zero.delay_frames))

    deep = measure(ff, anchor_case("medium"), clip, mode="default",
                   paced_seconds=paced_seconds)
    if not deep.ok:
        return False, "buffered anchor did not run (%s)" % (deep.error or "?")
    if deep.delay_frames < ANCHOR_DEEP_MIN_FRAMES:
        return False, ("%s at its default lookahead measured only %.1f frames of "
                       "delay; the pacing is not being observed"
                       % (anchor.name, deep.delay_frames))

    return True, ("%s reads %.1f frames buffered and %.1f frames zero-latency"
                  % (anchor.name, deep.delay_frames, zero.delay_frames))


# --------------------------------------------------------------------------
# startup cost, derived from measurements already taken
# --------------------------------------------------------------------------

# The calibration run must be enough shorter than the timed run for their
# difference to be dominated by encoding rather than by noise.
STARTUP_FRAME_RATIO = 4


def startup_costs(calib_runs, results):
    """Fixed per-invocation overhead, at no extra measurement cost.

    Every configuration is already run twice at different lengths: a short
    calibration pass and the timed run. Two points on `rtime = startup + n/rate`
    give both the slope and the intercept, so the fixed cost of an invocation --
    process start, hardware device init, filter graph setup, teardown -- falls
    out of numbers already collected.

    Two points cannot separate init from teardown, so this is the whole
    per-invocation overhead: the figure that matters when something spawns
    ffmpeg once per file. It is deliberately not used to derive delay, which
    cancels the term instead of estimating it.
    """
    out = []
    for result in results:
        case = result.case
        if case.kind != "throughput" or not result.rtime or not result.frames:
            continue
        pair = calib_runs.get((case.encoder.name, case.res_key, case.preset))
        if not pair:
            continue
        n1, t1 = pair
        n2, t2 = result.frames, result.rtime
        if n1 <= 0 or n2 < n1 * STARTUP_FRAME_RATIO or t2 <= t1:
            continue
        per_frame = (t2 - t1) / float(n2 - n1)
        startup = t1 - n1 * per_frame
        # A fixed cost cannot be negative, and cannot exceed the short run it
        # was derived from. Outside that range the two runs were not measuring
        # the same thing -- another process, a thermal step, a cold cache --
        # and the intercept is meaningless. Drop it rather than publish it.
        if startup < 0 or startup > t1:
            continue
        out.append({
            "encoder": case.encoder.name,
            "hardware": case.encoder.hardware,
            "resolution": case.res_key,
            "preset": case.preset,
            "startup_seconds": round(startup, 4),
            "per_frame_seconds": round(per_frame, 6),
            "calibration_frames": n1,
            "timed_frames": n2,
        })
    return out


def startup_by_encoder(costs, base_res=None):
    """One representative figure per encoder: the median of its points."""
    grouped = {}
    for entry in costs:
        grouped.setdefault(entry["encoder"], []).append(entry)
    out = {}
    for name, entries in grouped.items():
        preferred = [e for e in entries if e["resolution"] == base_res] or entries
        values = sorted(e["startup_seconds"] for e in preferred)
        out[name] = {
            "startup_seconds": values[len(values) // 2],
            "hardware": preferred[0]["hardware"],
            "points": len(entries),
            "resolution": preferred[0]["resolution"],
        }
    return out
