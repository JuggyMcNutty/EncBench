"""Encode quality measurement: PSNR, SSIM and VMAF against the source clip.

Throughput alone cannot tell you whether an encoder is any good -- a hardware
encoder that is ten times faster may also be visibly worse at the same bitrate.
This pass encodes to a real file, then scores it against the very clip that fed
the encoder, so the reference is exact.
"""

from __future__ import annotations

import os
import re
import time

from . import runner, util
from .util import detail, run, warn

PSNR_RE = re.compile(r"PSNR.*?average:([\d.]+)", re.I)
INF_RE = re.compile(r"average:inf", re.I)
SSIM_RE = re.compile(r"SSIM.*?All:([\d.]+)", re.I)
VMAF_RE = re.compile(r"VMAF score:\s*([\d.]+)", re.I)


def detect(ff):
    """Which metrics this ffmpeg build can compute."""
    metrics = {
        "psnr": ff.has_filter("psnr"),
        "ssim": ff.has_filter("ssim"),
        "vmaf": ff.has_filter("libvmaf"),
    }
    return metrics


def describe(metrics):
    have = [k.upper() for k in ("psnr", "ssim", "vmaf") if metrics.get(k)]
    return "+".join(have) if have else "none"


def measure(ff, case, clip, metrics, scratch=None):
    """Encode to a file, then score it. Returns a record dict or None."""
    scratch = scratch or os.path.join(os.path.dirname(clip.path), "..", "quality")
    scratch = os.path.abspath(scratch)
    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as e:
        warn("cannot create quality scratch dir: %s" % e)
        return None

    out_path = os.path.join(scratch, "q_%s_%s_%d.mkv" % (
        re.sub(r"[^A-Za-z0-9]+", "_", case.encoder.name),
        case.res_key, int((case.bitrate or 0) / 1000)))

    # Compare encoders at the bitrate that was actually asked for. Without VBV
    # constraints one-pass rate control overshoots badly on a short clip -- and
    # comparing quality at wildly different real bitrates is not a comparison.
    # Not every encoder accepts them (SVT-AV1 rejects max bitrate outside CRF
    # mode), so fall back automatically rather than losing the measurement.
    vbv = []
    if case.bitrate:
        vbv = ["-maxrate", str(int(case.bitrate)),
               "-bufsize", str(int(case.bitrate * 2))]

    rate_capped = bool(vbv)
    attempt_args = [vbv, []] if vbv else [[]]
    rc, err, timed_out, proc_seconds = 1, "", False, 0.0
    prog = runner.progress_file(clip)
    progress_text = ""
    for index, extra in enumerate(attempt_args):
        cmd = runner.build_command(ff, case, clip, output=out_path,
                                   output_format="matroska", extra_args=extra,
                                   progress_path=prog)
        started = time.monotonic()
        proc = runner.launch(cmd)
        rc, _out, err, timed_out = runner.collect(
            proc, runner.estimate_timeout(case, clip))
        proc_seconds = time.monotonic() - started
        progress_text = runner.drain_progress(prog)
        if rc == 0 and not timed_out:
            rate_capped = bool(extra)
            break
        if index == 0 and vbv:
            detail("%s rejected VBV constraints; retrying unconstrained"
                   % case.encoder.name)
    if rc != 0 or timed_out:
        _unlink(out_path)
        detail("quality encode failed for %s: %s"
               % (case.label(), runner.first_error_line(err)))
        return None

    progress = runner.parse_progress(progress_text)
    bench = runner.parse_benchmark(err)
    frames = int(progress.get("frame", 0) or 0)
    elapsed = bench.get("rtime") or proc_seconds
    encode_fps = (frames / elapsed) if (frames and elapsed) else None

    achieved = None
    try:
        size = int(progress.get("total_size", 0))
        seconds = int(progress.get("out_time_us", 0)) / 1e6
        if size and seconds > 0:
            achieved = size * 8 / seconds
    except (ValueError, ZeroDivisionError):
        pass

    scores = _score(ff, case, clip, out_path, metrics)
    _unlink(out_path)

    if not scores:
        return None

    record = case.as_dict()
    record.update(scores)
    record["encode_fps"] = round(encode_fps, 2) if encode_fps else None
    record["achieved_bitrate"] = round(achieved) if achieved else None
    record["rate_capped"] = rate_capped
    if achieved and case.bitrate:
        record["bitrate_error_pct"] = round(
            100.0 * (achieved - case.bitrate) / case.bitrate, 1)
    return record


def _reference_input(case, clip):
    """The exact frames the encoder saw: same loop, rate and duration."""
    duration = case.frames / float(case.fps)
    return ["-stream_loop", "-1", "-r", str(case.fps),
            "-t", "%.6f" % duration, "-i", clip.path]


def _align(fps, filter_name):
    """Pair the two streams by frame index, not by timestamp.

    The encoded file and the reference carry different timebases, and the
    default framesync pairing then silently compares frame N against a
    neighbour. That does not fail loudly -- it just returns plausible-looking
    numbers that are wrong (a bit-exact lossless encode scored 36 dB instead of
    infinity). Normalising the timebase and restamping by frame index fixes it.
    """
    tb = "settb=1/%d,setpts=N" % int(fps)
    return ("[0:v]%s[dist];[1:v]%s[ref];[dist][ref]%s=shortest=1"
            % (tb, tb, filter_name))


def self_check(ff, case, clip, lossless_encoder="libx264"):
    """Verify the comparison pipeline before trusting any of its numbers.

    Encodes losslessly and compares: anything short of a near-perfect score
    means the two streams are not aligned, and the metrics must not be reported.
    """
    from . import runner
    tmp = os.path.join(os.path.dirname(clip.path), "..", "quality")
    tmp = os.path.abspath(tmp)
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        return False, "cannot create scratch dir"
    path = os.path.join(tmp, "selfcheck.mkv")

    frames = min(case.frames, clip.frames, 48)
    from .sources import lossless_args
    cmd = [ff.path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-stream_loop", "-1", "-r", str(case.fps), "-i", clip.path,
           "-an", "-sn", "-frames:v", str(frames)]
    cmd += lossless_args(lossless_encoder) + ["-f", "matroska", path]
    proc = runner.launch(cmd)
    rc, _, err, _ = runner.collect(proc, 300)
    if rc != 0:
        _unlink(path)
        return False, "lossless reference encode failed (%s)" % lossless_encoder

    probe_case = _CheckCase(case.fps, frames)
    cmd = [ff.path, "-nostdin", "-hide_banner", "-i", path]
    cmd += _reference_input(probe_case, clip)
    cmd += ["-lavfi", _align(case.fps, "psnr"), "-f", "null", "-"]
    proc = runner.launch(cmd)
    rc, out, err, _ = runner.collect(proc, 300)
    _unlink(path)
    text = (err or "") + (out or "")
    if rc != 0:
        return False, "comparison pass failed"
    if INF_RE.search(text):
        return True, "lossless reference scored perfect"
    m = PSNR_RE.search(text)
    if m and float(m.group(1)) >= 60.0:
        return True, "lossless reference scored %.1f dB" % float(m.group(1))
    return False, ("lossless reference scored %s instead of perfect - the "
                   "distorted and reference streams are not aligned"
                   % (m.group(1) + " dB" if m else "nothing"))


class _CheckCase(object):
    def __init__(self, fps, frames):
        self.fps = fps
        self.frames = frames


def _combined_align(fps):
    """psnr and ssim in one graph.

    psnr emits its main (distorted) input unchanged, so it can be chained
    straight into ssim with a second copy of the reference. That halves the work
    of the slowest phase: each separate pass otherwise re-decodes both the
    encoded file and the looped raw reference from scratch.
    """
    tb = "settb=1/%d,setpts=N" % int(fps)
    return ("[0:v]%s[dist];[1:v]%s,split=2[ref1][ref2];"
            "[dist][ref1]psnr=shortest=1[p];[p][ref2]ssim=shortest=1" % (tb, tb))


def _run_graph(ff, case, clip, encoded, graph, label, timeout=900):
    cmd = [ff.path, "-nostdin", "-hide_banner", "-i", encoded]
    cmd += _reference_input(case, clip)
    cmd += ["-lavfi", graph, "-f", "null", "-"]
    proc = runner.launch(cmd)
    rc, out, err, timed_out = runner.collect(proc, timeout)
    text = (err or "") + (out or "")
    if rc != 0 or timed_out:
        detail("%s pass failed for %s: %s"
               % (label, case.label(), runner.first_error_line(text)))
        return None
    return text


def _extract(text, scores, pattern, field):
    m = pattern.search(text or "")
    if not m:
        return
    try:
        scores[field] = round(float(m.group(1)), 3)
    except ValueError:
        pass


def _score(ff, case, clip, encoded, metrics):
    scores = {}
    want_psnr = bool(metrics.get("psnr"))
    want_ssim = bool(metrics.get("ssim"))

    if want_psnr and want_ssim:
        text = _run_graph(ff, case, clip, encoded,
                          _combined_align(case.fps), "psnr+ssim")
        _extract(text, scores, PSNR_RE, "psnr_db")
        _extract(text, scores, SSIM_RE, "ssim")
    elif want_psnr:
        text = _run_graph(ff, case, clip, encoded, _align(case.fps, "psnr"), "psnr")
        _extract(text, scores, PSNR_RE, "psnr_db")
    elif want_ssim:
        text = _run_graph(ff, case, clip, encoded, _align(case.fps, "ssim"), "ssim")
        _extract(text, scores, SSIM_RE, "ssim")

    if metrics.get("vmaf"):
        text = _run_graph(ff, case, clip, encoded,
                          _align(case.fps, "libvmaf"), "vmaf")
        _extract(text, scores, VMAF_RE, "vmaf")
    return scores


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass
