"""Execution and measurement.

Every number reported by encbench comes from here. Two ffmpeg facilities do the
work: `-progress pipe:1` for machine-readable frame/size counters (never scrape
stderr), and `-benchmark` for the process's own utime/stime/rtime/maxrss.

Output goes to a real muxer pointed at /dev/null: measurably the same speed as
the null muxer, but it yields the achieved output size so requested-vs-actual
bitrate can be checked.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time

from . import util
from .util import detail, warn

# Errors that mean "the hardware refused another session", not "the test failed".
SESSION_LIMIT_PATTERNS = [
    re.compile(r"OpenEncodeSessionEx failed", re.I),
    re.compile(r"out of memory", re.I),
    re.compile(r"No capable devices found", re.I),
    re.compile(r"Failed to create (VAAPI|Vulkan) (context|instance)", re.I),
    re.compile(r"resource allocation failed", re.I),
    re.compile(r"Cannot allocate memory", re.I),
    re.compile(r"device is busy", re.I),
]

# Process tracking lives in util so probes and source generation share it.
kill_group = util.kill_group
terminate_all = util.terminate_all
aborted = util.aborted
reset_abort = util.reset_abort


# --------------------------------------------------------------------------
# test description
# --------------------------------------------------------------------------

class TestCase(object):
    def __init__(self, encoder, res_key, width, height, fps, complexity,
                 frames, bitrate=None, preset=None, threads=None,
                 rate_mode="bitrate", quality=None, kind="throughput"):
        self.encoder = encoder
        self.res_key = res_key
        self.width = width
        self.height = height
        self.fps = fps
        self.complexity = complexity
        self.frames = frames
        self.bitrate = bitrate
        self.preset = preset
        self.threads = threads
        self.rate_mode = rate_mode
        self.quality = quality
        self.kind = kind

    @property
    def key(self):
        return (self.encoder.name, self.res_key, self.fps, self.complexity,
                self.bitrate, self.preset, self.threads, self.rate_mode,
                self.quality, self.kind)

    def label(self):
        bits = ["%s %s@%d" % (self.encoder.name, self.res_key, self.fps)]
        if self.preset is not None:
            bits.append(str(self.preset))
        if self.rate_mode == "bitrate" and self.bitrate:
            bits.append("%.1fM" % (self.bitrate / 1e6))
        elif self.quality is not None:
            bits.append("q%s" % self.quality)
        bits.append(self.complexity)
        return " ".join(bits)

    def as_dict(self):
        return {
            "encoder": self.encoder.name, "codec": self.encoder.codec,
            "hardware": self.encoder.hardware, "backend": self.encoder.family,
            "resolution": self.res_key, "width": self.width, "height": self.height,
            "fps": self.fps, "complexity": self.complexity, "frames": self.frames,
            "bitrate": self.bitrate, "preset": self.preset, "threads": self.threads,
            "rate_mode": self.rate_mode, "quality": self.quality, "kind": self.kind,
        }


class TestResult(object):
    def __init__(self, case):
        self.case = case
        self.ok = False
        self.error = ""
        self.session_limited = False
        self.frames = 0
        self.encode_fps = None
        self.realtime_x = None
        self.wall_seconds = None
        self.rtime = None
        self.cpu_seconds = None
        self.cpu_cores = None
        self.maxrss_bytes = None
        self.achieved_bitrate = None
        self.bitrate_error_pct = None
        self.decode_fps = None
        self.decode_bound = False
        self.temp_c = None
        self.throttled = False
        self.stdev_fps = None
        self.wall_fallback = False
        self.min_mhz = None
        self.command = []

    def as_dict(self):
        d = self.case.as_dict()
        d.update({
            "ok": self.ok, "error": self.error,
            "session_limited": self.session_limited,
            "frames_encoded": self.frames,
            "encode_fps": _round(self.encode_fps, 2),
            "realtime_x": _round(self.realtime_x, 3),
            "wall_seconds": _round(self.wall_seconds, 3),
            "rtime": _round(self.rtime, 3),
            "cpu_seconds": _round(self.cpu_seconds, 3),
            "cpu_cores": _round(self.cpu_cores, 2),
            "maxrss_bytes": self.maxrss_bytes,
            "achieved_bitrate": _round(self.achieved_bitrate, 0),
            "bitrate_error_pct": _round(self.bitrate_error_pct, 1),
            "decode_fps": _round(self.decode_fps, 1),
            "decode_bound": self.decode_bound,
            "temp_c": self.temp_c, "throttled": self.throttled,
            "min_mhz": _round(self.min_mhz, 0),
            "stdev_fps": _round(self.stdev_fps, 2),
            "wall_fallback": self.wall_fallback,
        })
        return d


def _round(v, n):
    return None if v is None else round(v, n)


# --------------------------------------------------------------------------
# command construction
# --------------------------------------------------------------------------

def build_command(ff, case, clip, output="/dev/null", output_format="matroska",
                  extra_args=None, progress_path=None, paced=False,
                  stats_period=None, pace_fps=None):
    spec = case.encoder
    # -benchmark writes at AV_LOG_INFO, so "-loglevel error" silently discards
    # utime/stime/rtime/maxrss and every throughput figure falls back to wall
    # clock -- which folds process startup, hardware init and filter-graph setup
    # into the measurement (understating a short ultrafast run by ~36%).
    # -nostats suppresses only the periodic progress line, which -progress replaces.
    #
    # -progress goes to a private file, never "pipe:1": Fedora's libx265 is linked
    # against libvmaf and, on failing to find its model, prints a line to *stdout*
    # once per frame. On the shared stdout stream that interleaves mid-line with
    # the progress counters ("problem loading model file: frame=364"), parse_progress
    # mis-keys it, the frame count is lost and a healthy encode reads back as
    # "no usable progress output". A dedicated sink cannot be corrupted that way.
    progress_target = ("file:" + progress_path) if progress_path else "pipe:1"
    cmd = [ff.path, "-nostdin", "-hide_banner", "-loglevel", "info", "-nostats",
           "-benchmark", "-progress", progress_target, "-y"]
    # How often -progress emits a block. Left alone (0.5s default) for
    # throughput, where only the final counters matter; the latency pass needs
    # a quantum far smaller than one frame and passes its own.
    if stats_period:
        cmd += ["-stats_period", "%.4f" % stats_period]

    # Hardware device initialisation must precede the input.
    cmd += spec.pre_input

    # Pace the input reader at the target framerate. Latency pass only: it turns
    # the encoder's structural delay from a few milliseconds of free-running
    # time into something an order of magnitude larger than process startup
    # noise. It would destroy a throughput measurement, which is why it is off
    # by default rather than a property of the case.
    #
    # -readrate rather than -re, and expressed against the clip's *stored* rate
    # rather than the target one: the pacer runs at the demuxer, below the "-r"
    # below, so it sees the clip's own timestamps and -re would pace a 60 fps
    # clip at 60 fps no matter what the encode was configured for. Measured: a
    # 600-frame run of a 60 fps clip with "-re -r 30" takes 9.5s, not the 20s
    # that pacing at 30 would give.
    #
    # The initial burst must also be disabled, or the pacer hands over the
    # first ~0.5s of input for free -- fifteen frames at 30 fps, more than
    # enough to fill a lookahead and make a deep pipeline read as a shallow
    # one. A zero there means "use the default", so it takes a small positive
    # value instead.
    if paced:
        rate = float(getattr(clip, "rate", None) or case.fps)
        # The pacing rate is not necessarily the content rate. Delay is solved
        # from (1/pace - 1/E), which is ill-conditioned as E approaches the
        # pacing rate and undefined at or below it -- so pacing an encoder that
        # cannot reach realtime at its own content rate measures nothing.
        # Pacing slower keeps the arithmetic well-conditioned for any encoder;
        # delay is still a frame count, and converts to ms at the content rate.
        target = float(pace_fps or case.fps)
        cmd += ["-readrate", "%.10g" % (target / rate),
                "-readrate_initial_burst", "0.001"]

    # Loop the short cached clip up to the requested measurement length, and
    # reinterpret its nominal rate as the target fps so rate control and GOP
    # length behave as they would for real content at that framerate.
    cmd += ["-stream_loop", "-1", "-r", str(case.fps), "-i", clip.path]
    cmd += ["-an", "-sn", "-frames:v", str(case.frames)]

    if spec.filters:
        cmd += ["-vf", ",".join(spec.filters)]

    cmd += ["-c:v", spec.name]
    cmd += rate_args(case)
    cmd += preset_args(case)
    if extra_args:
        cmd += list(extra_args)

    # Pin GOP length so encoders with wildly different defaults (x264 250,
    # libvpx 9999) are compared on the same keyframe cadence.
    cmd += ["-g", str(int(case.fps * 2))]

    if case.threads:
        cmd += ["-threads", str(case.threads)]
    cmd += spec.extra_args
    cmd += ff.fps_mode_args("passthrough")
    cmd += ["-f", output_format, output]
    return cmd


def rate_args(case):
    if case.rate_mode == "bitrate" and case.bitrate:
        return ["-b:v", str(int(case.bitrate))]
    if case.rate_mode == "quality" and case.quality is not None:
        return quality_args(case.encoder, case.quality)
    return []


def quality_args(spec, level):
    """Constant-quality knob, chosen from options the encoder actually has."""
    opts = getattr(spec, "rc_options", set()) or set()
    fam = spec.family
    if fam == "vaapi":
        return ["-rc_mode", "CQP", "-qp", str(level)]
    if fam == "nvenc":
        return ["-rc", "constqp", "-qp", str(level)]
    if fam == "qsv":
        return ["-global_quality", str(level)]
    if spec.name in ("libvpx", "libvpx-vp9"):
        return ["-crf", str(level), "-b:v", "0"]
    if "crf" in opts:
        return ["-crf", str(level)]
    if "qp" in opts:
        return ["-qp", str(level)]
    if "global_quality" in opts:
        return ["-global_quality", str(level)]
    return ["-q:v", str(level)]


def preset_args(case):
    spec = case.encoder
    if case.preset is None or not spec.speed_knob:
        return []
    return ["-" + spec.speed_knob, str(case.preset)]


# --------------------------------------------------------------------------
# output parsing
# --------------------------------------------------------------------------

def progress_file(clip):
    """Create a private file for one encode's -progress output.

    Lives next to the source clip, i.e. in the scratch dir that was chosen to
    have room. Tiny (a few KB); drain_progress removes it.
    """
    directory = os.path.dirname(clip.path) or "."
    # No leading dot: a straggler (drain skipped by a hard crash) must still be
    # swept by SourceLibrary.cleanup, whose glob("*") ignores hidden files.
    fd, path = tempfile.mkstemp(prefix="progress_", suffix=".txt", dir=directory)
    os.close(fd)
    return path


def drain_progress(path):
    """Read a -progress file and delete it. Missing/partial is fine."""
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class ProgressTailer(threading.Thread):
    """Follows a -progress file live, stamping each block with a wall time.

    drain_progress reads the file once the process has exited, which is all a
    throughput measurement needs. Latency needs to know *when* a frame came out,
    so this follows the file while the encode runs and records one
    (seconds-since-launch, frames-out) sample per block.

    ffmpeg flushes the progress AVIO after writing each block, so a block's
    arrival time really is an observation of when the encoder had produced that
    many frames -- but only to within -stats_period, which the caller must set
    small enough (see latency.stats_period_for).
    """

    def __init__(self, path, start_time, interval=0.004):
        threading.Thread.__init__(self, daemon=True)
        self.path = path
        self.start_time = start_time
        self.interval = interval
        self.samples = []              # (seconds since start_time, frames out)
        self._stop_event = threading.Event()
        self._buf = ""
        self._frame = 0

    def run(self):
        fh = None
        try:
            while not self._stop_event.is_set():
                if fh is None:
                    try:
                        fh = open(self.path, "r", encoding="utf-8",
                                  errors="replace")
                    except OSError:
                        self._stop_event.wait(self.interval)
                        continue
                if not self._pump(fh):
                    self._stop_event.wait(self.interval)
        finally:
            if fh is not None:
                # One last read: the final blocks are usually written between
                # the process exiting and this thread being told to stop.
                try:
                    self._pump(fh)
                finally:
                    try:
                        fh.close()
                    except OSError:
                        pass

    def _pump(self, fh):
        try:
            chunk = fh.read()
        except OSError:
            return False
        if not chunk:
            return False
        now = time.monotonic() - self.start_time
        self._buf += chunk
        lines = self._buf.split("\n")
        self._buf = lines.pop()
        for line in lines:
            line = line.strip()
            if line.startswith("frame="):
                try:
                    self._frame = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("progress="):
                # End of a block: everything above it belongs to this instant.
                self.samples.append((now, self._frame))
        return True

    def stop(self):
        self._stop_event.set()
        self.join(timeout=5)

    def first_output_at(self):
        """When the first encoded frame was muxed, seconds since launch."""
        for seconds, frames in self.samples:
            if frames >= 1:
                return seconds
        return None


# -progress emits only these keys; anything else on a line is foreign noise.
_PROGRESS_KEY = re.compile(
    r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|"
    r"out_time|dup_frames|drop_frames|speed|progress)=(.*)$")


def parse_progress(text):
    """Last value of each -progress key.

    Only well-formed "key=value" lines whose key is one -progress actually emits
    are accepted; a dedicated sink should be clean, but a strict parse means a
    stray write from an encoder library can never masquerade as a counter.
    """
    values = {}
    for line in (text or "").splitlines():
        m = _PROGRESS_KEY.match(line.strip())
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


BENCH_RE = re.compile(r"bench: utime=([\d.]+)s stime=([\d.]+)s rtime=([\d.]+)s")
MAXRSS_RE = re.compile(r"bench: maxrss=(\d+)(?:KiB|kB)")


def parse_benchmark(text):
    out = {}
    for m in BENCH_RE.finditer(text or ""):
        out["utime"] = float(m.group(1))
        out["stime"] = float(m.group(2))
        out["rtime"] = float(m.group(3))
    m = MAXRSS_RE.search(text or "")
    if m:
        out["maxrss"] = int(m.group(1)) * 1024
    return out


def is_session_limit(text):
    return any(p.search(text or "") for p in SESSION_LIMIT_PATTERNS)


# Noise an encoder library emits regardless of whether the encode itself failed.
# Fedora's libx265 spams "problem loading model file" / "libvmaf ERROR ..." once
# per frame; without this it becomes the user-visible reason for an unrelated skip.
_ERROR_NOISE = ("problem loading model file", "libvmaf error",
                "could not read model from path")


def first_error_line(text):
    best = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("bench:"):
            continue
        # Skip banner rules and decoration: an encoder's first stderr line is
        # often its version banner, not the reason it failed.
        if sum(1 for ch in line if ch.isalpha()) < 3:
            continue
        if line.startswith("[") and "]" in line:
            line = line.split("]", 1)[1].strip()
        lowered = line.lower()
        if any(n in lowered for n in _ERROR_NOISE):
            continue
        if any(word in lowered for word in
               ("error", "failed", "cannot", "unable", "not supported",
                "no such", "invalid", "unsupported", "denied")):
            return line[:200]
        if not best:
            best = line[:200]
    return best


# --------------------------------------------------------------------------
# process control
# --------------------------------------------------------------------------

def launch(cmd):
    return util.spawn(cmd)


def collect(proc, timeout):
    """Wait for a process, killing its whole group if it overruns."""
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = b"", b""
    finally:
        util.unregister(proc)
    return (proc.returncode, _dec(out), _dec(err), timed_out)


def _dec(b):
    if not b:
        return ""
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else b




# --------------------------------------------------------------------------
# thermal / frequency sampling
# --------------------------------------------------------------------------

class Sampler(threading.Thread):
    """Polls temperature and CPU clock during a run to catch throttling."""

    WARMUP_SAMPLES = 2      # discard the ramp from idle to load

    def __init__(self, interval=0.5):
        threading.Thread.__init__(self, daemon=True)
        self.interval = interval
        self._stop_event = threading.Event()
        self.max_temp = None
        self.min_mhz = None
        self.max_mhz = None
        self.samples = 0
        self.mhz_series = []

    def run(self):
        while not self._stop_event.is_set():
            temp = read_temp_c()
            if temp is not None:
                self.max_temp = temp if self.max_temp is None else max(self.max_temp, temp)
            mhz = read_cpu_mhz()
            if mhz is not None:
                self.mhz_series.append(mhz)
                if self.samples >= self.WARMUP_SAMPLES:
                    self.min_mhz = mhz if self.min_mhz is None else min(self.min_mhz, mhz)
                    self.max_mhz = mhz if self.max_mhz is None else max(self.max_mhz, mhz)
            self.samples += 1
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()
        self.join(timeout=2)

    def sustained_drop(self):
        """How far the clock fell from the run's first half to its last half.

        Throttling is a *sustained* downward trend -- the clock fell and stayed
        down -- not instantaneous spread. On a many-core part running a bursty
        encode, per-core scaling_cur_freq averaged across the package swings ~3x
        between work units with the governor perfectly healthy, so the old
        min < 0.65*max test fired on every long software encode on a powersave
        box (min 1.5 GHz, max 4.2 GHz, 81 C, no actual throttle). Comparing an
        early window's mean against a late one ignores that jitter and only
        catches a real decline. Returns a fraction (0.3 == the late window ran
        30% slower) or None when there are too few samples to judge.
        """
        series = self.mhz_series[self.WARMUP_SAMPLES:]
        if len(series) < 8:
            return None
        half = len(series) // 2
        early = sum(series[:half]) / half
        late = sum(series[half:]) / (len(series) - half)
        if early <= 0:
            return None
        return 1.0 - late / early


_THERMAL_PATHS = None
_FREQ_PATHS = None
_MAX_MHZ = None


def _thermal_paths():
    global _THERMAL_PATHS
    if _THERMAL_PATHS is None:
        import glob as _glob
        paths = []
        for pattern in ("/sys/class/thermal/thermal_zone*/temp",
                        "/sys/class/hwmon/hwmon*/temp1_input"):
            paths.extend(_glob.glob(pattern))
        _THERMAL_PATHS = paths
    return _THERMAL_PATHS


def read_temp_c():
    best = None
    for path in _thermal_paths():
        try:
            with open(path) as fh:
                raw = int(fh.read().strip())
        except (OSError, ValueError):
            continue
        celsius = raw / 1000.0 if raw > 1000 else float(raw)
        if 0 < celsius < 150:
            best = celsius if best is None else max(best, celsius)
    return best


def _freq_paths():
    """Resolve the sysfs clock files once; this is polled during every run."""
    global _FREQ_PATHS
    if _FREQ_PATHS is None:
        import glob as _glob
        _FREQ_PATHS = _glob.glob(
            "/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")
    return _FREQ_PATHS


def max_cpu_mhz():
    global _MAX_MHZ
    if _MAX_MHZ is None:
        import glob as _glob
        best = 0
        for path in _glob.glob(
                "/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq"):
            try:
                with open(path) as fh:
                    best = max(best, int(fh.read().strip()) / 1000.0)
            except (OSError, ValueError):
                continue
        _MAX_MHZ = best or 0
    return _MAX_MHZ or None


def read_cpu_mhz():
    total, count = 0, 0
    for path in _freq_paths():
        try:
            with open(path) as fh:
                total += int(fh.read().strip()) / 1000.0
                count += 1
        except (OSError, ValueError):
            continue
    if count:
        return total / count
    try:
        with open("/proc/cpuinfo") as fh:
            vals = [float(l.split(":")[1]) for l in fh if l.startswith("cpu MHz")]
        if vals:
            return sum(vals) / len(vals)
    except (OSError, ValueError, IndexError):
        pass
    return None


# --------------------------------------------------------------------------
# running a single test
# --------------------------------------------------------------------------

def estimate_timeout(case, clip):
    """Generous per-test ceiling; slow encoders at 4K are legitimately slow."""
    pixels = case.width * case.height
    base = 60.0 + (pixels / (1920.0 * 1080.0)) * case.frames * 0.6
    if case.encoder.name in ("libaom-av1", "librav1e", "libvpx-vp9", "libvpx"):
        base *= 4
    if case.encoder.hardware:
        base = min(base, 240.0)
    return max(45.0, min(base, 1800.0))


def run_single(ff, case, clip, timeout=None, sample=True):
    """Run one encode and turn it into a TestResult."""
    result = TestResult(case)
    result.decode_fps = clip.decode_fps
    prog = progress_file(clip)
    cmd = build_command(ff, case, clip, progress_path=prog)
    result.command = cmd
    timeout = timeout or estimate_timeout(case, clip)

    sampler = None
    if sample:
        sampler = Sampler()
        sampler.start()

    start = time.monotonic()
    try:
        proc = launch(cmd)
        rc, _out, err, timed_out = collect(proc, timeout)
    finally:
        progress_text = drain_progress(prog)
    wall = time.monotonic() - start

    if sampler:
        sampler.stop()
        result.temp_c = sampler.max_temp
        result.min_mhz = sampler.min_mhz
        # Throttling means the clock FELL and stayed down during the run, or the
        # part got hot. It does not mean "this CPU did not sustain its single-core
        # boost on all cores" -- true of essentially every modern processor -- nor
        # "the averaged per-core clock jittered", which it always does under a
        # bursty encode. Only a sustained decline (see Sampler.sustained_drop)
        # or a real temperature counts.
        drop = sampler.sustained_drop()
        if drop is not None and drop >= 0.35:
            result.throttled = True
        if sampler.max_temp and sampler.max_temp >= 95:
            result.throttled = True

    result.wall_seconds = wall
    _fill_result(result, case, rc, progress_text, err, timed_out, wall)
    return result


def _fill_result(result, case, rc, out, err, timed_out, wall):
    if timed_out:
        result.error = "timed out"
        return
    if rc != 0:
        result.session_limited = is_session_limit(err)
        result.error = first_error_line(err) or ("exit %s" % rc)
        return

    progress = parse_progress(out)
    bench = parse_benchmark(err)

    try:
        result.frames = int(progress.get("frame", 0))
    except ValueError:
        result.frames = 0

    result.rtime = bench.get("rtime")
    result.wall_fallback = result.rtime is None
    elapsed = result.rtime or wall
    if not result.frames or not elapsed:
        result.error = "no usable progress output"
        return

    result.encode_fps = result.frames / elapsed
    result.realtime_x = result.encode_fps / float(case.fps)

    if "utime" in bench:
        result.cpu_seconds = bench["utime"] + bench["stime"]
        if elapsed > 0:
            result.cpu_cores = result.cpu_seconds / elapsed
    result.maxrss_bytes = bench.get("maxrss")

    size = progress.get("total_size")
    out_time_us = progress.get("out_time_us")
    if size and size != "N/A" and out_time_us and out_time_us != "N/A":
        try:
            seconds = int(out_time_us) / 1e6
            if seconds > 0:
                result.achieved_bitrate = (int(size) * 8) / seconds
                if case.bitrate:
                    result.bitrate_error_pct = (
                        100.0 * (result.achieved_bitrate - case.bitrate) / case.bitrate)
        except (ValueError, ZeroDivisionError):
            pass

    # If the encoder ran close to the source's decode ceiling we measured the
    # pipeline, not the encoder. Say so rather than publishing a bogus number.
    if result.decode_fps and result.encode_fps >= result.decode_fps * 0.85:
        result.decode_bound = True

    result.ok = True


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------

def run_parallel(ff, case, clip, count, timeout=None):
    """Launch `count` identical encodes at once; measure each independently."""
    timeout = timeout or estimate_timeout(case, clip) * 2
    # Each stream needs its own -progress sink, or they overwrite each other's
    # counters and every stream reads back the same (wrong) fps.
    progs = [progress_file(clip) for _ in range(count)]
    cmds = [build_command(ff, case, clip, progress_path=p) for p in progs]

    sampler = Sampler()
    sampler.start()

    procs = []
    start = time.monotonic()
    try:
        for i in range(count):
            procs.append(launch(cmds[i]))

        collected = [None] * count
        threads = []

        def worker(index, proc):
            collected[index] = collect(proc, timeout)

        for i, proc in enumerate(procs):
            t = threading.Thread(target=worker, args=(i, proc), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    finally:
        wall = time.monotonic() - start
        sampler.stop()
        progress_texts = [drain_progress(p) for p in progs]

    results = []
    for i in range(count):
        res = TestResult(case)
        res.decode_fps = clip.decode_fps
        res.command = cmds[i]
        res.temp_c = sampler.max_temp
        res.wall_seconds = wall
        if collected[i] is None:
            res.error = "no result"
            results.append(res)
            continue
        rc, _out, err, timed_out = collected[i]
        _fill_result(res, case, rc, progress_texts[i], err, timed_out, wall)
        results.append(res)
    return results


CONCURRENCY_LEVELS = [1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64]


class RampStep(object):
    def __init__(self, level):
        self.level = level
        self.ok_count = 0
        self.min_fps = None
        self.mean_fps = None
        self.aggregate_fps = None
        self.realtime = False
        self.session_limited = False
        self.at_ceiling = False
        self.failed_streams = 0
        self.error = ""

    def as_dict(self):
        return {"streams": self.level, "ok_count": self.ok_count,
                "min_fps": _round(self.min_fps, 2),
                "mean_fps": _round(self.mean_fps, 2),
                "aggregate_fps": _round(self.aggregate_fps, 2),
                "all_realtime": self.realtime,
                "session_limited": self.session_limited,
                "at_ceiling": self.at_ceiling,
                "failed_streams": self.failed_streams,
                "error": self.error}


def run_concurrency_ramp(ff, case, clip, max_level, on_step=None):
    """Raise the parallel stream count until the box stops keeping up.

    'Keeping up' means every stream individually held the target framerate --
    an aggregate average would hide streams that fell behind.
    """
    steps = []
    for index, level in enumerate(CONCURRENCY_LEVELS):
        if level > max_level or aborted():
            break
        results = run_parallel(ff, case, clip, level)
        step_result = RampStep(level)
        ok = [r for r in results if r.ok and r.encode_fps]
        step_result.ok_count = len(ok)

        if not ok:
            failed = next((r for r in results if r.error), None)
            step_result.error = failed.error if failed else "all streams failed"
            step_result.session_limited = any(r.session_limited for r in results)
            steps.append(step_result)
            if on_step:
                on_step(step_result)
            break

        fps_values = [r.encode_fps for r in ok]
        step_result.min_fps = min(fps_values)
        step_result.mean_fps = sum(fps_values) / len(fps_values)
        step_result.aggregate_fps = sum(fps_values)
        step_result.realtime = (len(ok) == level
                                and step_result.min_fps >= case.fps)
        step_result.session_limited = any(r.session_limited for r in results)
        step_result.failed_streams = level - len(ok)

        # Streams that crashed are not the same as streams that fell behind.
        # Reporting a driver hiccup or an OOM kill as "saturated at N" asserts a
        # hardware capacity limit that was never measured.
        if step_result.failed_streams and not step_result.session_limited:
            failed = next((r for r in results if r.error), None)
            step_result.error = "%d of %d streams failed: %s" % (
                step_result.failed_streams, level,
                failed.error if failed else "unknown")

        steps.append(step_result)
        if on_step:
            on_step(step_result)

        if step_result.session_limited or step_result.failed_streams:
            break
        if not step_result.realtime:
            break

        # Distinguish "the machine kept up and we ran out of configured levels"
        # from "the machine saturated". max_level rarely equals a ramp level, so
        # comparing the last level against it is not enough.
        remaining = [l for l in CONCURRENCY_LEVELS[index + 1:] if l <= max_level]
        if not remaining:
            step_result.at_ceiling = True
            break
    return steps


def max_realtime_streams(steps):
    best = 0
    for s in steps:
        if s.realtime:
            best = max(best, s.level)
    return best


def peak_aggregate(steps):
    best = None
    for s in steps:
        if s.aggregate_fps is not None:
            best = s.aggregate_fps if best is None else max(best, s.aggregate_fps)
    return best
