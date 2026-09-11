"""Deterministic test footage, generated locally and cached.

Two design choices matter here:

* Sources are stored **raw** (yuv4mpeg) when there is room. A compressed source
  imposes a decode ceiling, and since ffmpeg pipelines decode and encode in one
  process, a fast hardware encoder can end up measuring the *decoder*. Raw input
  removes that failure mode; lossless H.264 is the space-constrained fallback and
  the decode baseline is always measured so the report can flag the risk.

* Stored clips are short and extended at encode time with -stream_loop. Storage
  stays small while each measurement runs long enough to be stable.
"""

from __future__ import annotations

import glob
import os
import re

from . import ffmpeg_setup, util
from .util import detail, run, step, warn

# Bump when a filter graph changes so stale caches regenerate.
GRAPH_VERSION = 2

RESOLUTIONS = [
    ("360p",  640,  360),
    ("480p",  854,  480),
    ("720p",  1280, 720),
    ("1080p", 1920, 1080),
    ("1440p", 2560, 1440),
    ("2160p", 3840, 2160),
]
RES_BY_KEY = {k: (w, h) for k, w, h in RESOLUTIONS}
RES_ORDER = [k for k, _, _ in RESOLUTIONS]

# Nominal rate of stored clips; the encode step reinterprets with -r.
BASE_RATE = 60

# Stored frame counts taper with resolution to bound the cache footprint.
FRAMES_BY_RES = {
    "360p": 120, "480p": 120, "720p": 120,
    "1080p": 120, "1440p": 72, "2160p": 48,
}

COMPLEXITIES = ("low", "high")

BYTES_PER_FRAME_RAW = {k: w * h * 1.5 for k, w, h in RESOLUTIONS}
# Lossless H.264 measured at roughly half of raw on this content.
LOSSLESS_RATIO = 0.6


class SourceClip(object):
    def __init__(self, path, res_key, complexity, width, height, frames, fmt):
        self.path = path
        self.res_key = res_key
        self.complexity = complexity
        self.width = width
        self.height = height
        self.frames = frames
        self.fmt = fmt               # 'raw' | 'lossless'
        self.decode_fps = None       # measured ceiling, frames/sec
        # Nominal rate the clip is stored at. Every encode reinterprets this as
        # its own target framerate, so it has to be known rather than assumed:
        # a --source clip carries whatever rate its own footage had.
        self.rate = float(BASE_RATE)

    def as_dict(self):
        return {"path": self.path, "resolution": self.res_key,
                "complexity": self.complexity, "width": self.width,
                "height": self.height, "frames": self.frames,
                "format": self.fmt, "decode_fps": self.decode_fps,
                "rate": self.rate}

    def __repr__(self):
        return "SourceClip(%s/%s, %dx%d, %d frames, %s)" % (
            self.complexity, self.res_key, self.width, self.height,
            self.frames, self.fmt)


def _graph(complexity, width, height):
    """Deterministic lavfi graph. Fixed seeds keep it identical everywhere."""
    if complexity == "low":
        # Talking-head / screencast analogue: smooth gradients, slow motion,
        # one moving element, light grain. Cheap to encode; rewards good RC.
        fg_w, fg_h = _even(width // 3), _even(height // 3)
        return (
            "gradients=size=%dx%d:rate=%d:speed=0.01:nb_colors=3[bg];"
            "testsrc2=size=%dx%d:rate=%d[fg];"
            "[bg][fg]overlay=x='(W-w)/2+sin(t)*W/8':y='(H-h)/2',"
            "noise=alls=4:allf=t:all_seed=12345,format=yuv420p"
            % (width, height, BASE_RATE, fg_w, fg_h, BASE_RATE)
        )
    # Sports / action analogue: dense detail, constant global motion, grain.
    # Saturates motion estimation and soaks up bitrate.
    return (
        "mandelbrot=size=%dx%d:rate=%d:maxiter=180,"
        "noise=alls=6:allf=t:all_seed=12345,format=yuv420p"
        % (width, height, BASE_RATE)
    )


def lossless_args(encoder):
    """Encoder arguments that produce a mathematically lossless file."""
    if encoder in ("libx264", "libx265"):
        return ["-c:v", encoder, "-preset", "ultrafast", "-qp", "0"]
    return ["-c:v", encoder]


def _even(v):
    return max(2, int(v) & ~1)


def estimate_bytes(res_keys, complexities, fmt, frames_override=None):
    total = 0
    for key in res_keys:
        frames = frames_override or FRAMES_BY_RES.get(key, 120)
        per = BYTES_PER_FRAME_RAW[key] * frames
        if fmt == "lossless":
            per *= LOSSLESS_RATIO
        total += per * len(complexities)
    return int(total)


def choose_format(scratch, res_keys, complexities, requested=None,
                  frames_override=None):
    """Raw removes the decode ceiling; fall back to lossless if space is tight."""
    if requested in ("raw", "lossless"):
        return requested
    need_raw = estimate_bytes(res_keys, complexities, "raw", frames_override)
    try:
        free = os.statvfs(scratch)
        avail = free.f_bavail * free.f_frsize
    except OSError:
        return "lossless"
    # Leave generous headroom; on tmpfs this is RAM being consumed.
    if avail > need_raw * 3:
        return "raw"
    detail("raw sources would need %s with only %s free; using lossless sources"
           % (util.human_bytes(need_raw), util.human_bytes(avail)))
    return "lossless"


class SourceLibrary(object):
    """Lazily generates and caches test clips, one per (resolution, complexity)."""

    def __init__(self, ff, scratch, fmt="raw", frames_override=None,
                 user_source=None, lossless_encoder=None):
        self.ff = ff
        self.dir = os.path.join(scratch, "sources")
        self.fmt = fmt
        self.frames_override = frames_override
        self.user_source = user_source
        self.lossless_encoder = lossless_encoder or self._pick_lossless_encoder()
        self._cache = {}
        os.makedirs(self.dir, exist_ok=True)
        # runner writes one -progress file here per encode and removes it when
        # read; a hard kill (SIGKILL, second Ctrl-C) can orphan one. Sweep any
        # left by an earlier run so they never accumulate.
        for stale in glob.glob(os.path.join(self.dir, "progress_*.txt")):
            _unlink(stale)

    def _pick_lossless_encoder(self):
        p = run([self.ff.path, "-hide_banner", "-encoders"], timeout=60)
        text = p.out or ""
        for name in ("libx264", "ffv1", "libx265"):
            if re.search(r"\s%s\s" % re.escape(name), text):
                return name
        return "ffv1"

    def frames_for(self, res_key):
        return self.frames_override or FRAMES_BY_RES.get(res_key, 120)

    def _source_tag(self):
        """Identify user footage by content, not by the word 'user'.

        Two different --source files produced the same cache path, so the second
        run silently benchmarked the first file's frames.
        """
        if not self.user_source:
            return "syn"
        try:
            st = os.stat(self.user_source)
            ident = "%s:%d:%d" % (os.path.abspath(self.user_source),
                                  st.st_size, int(st.st_mtime))
        except OSError:
            ident = os.path.abspath(self.user_source)
        import hashlib
        return "user" + hashlib.sha1(ident.encode("utf-8")).hexdigest()[:8]

    def path_for(self, res_key, complexity):
        frames = self.frames_for(res_key)
        ext = "y4m" if self.fmt == "raw" else "mkv"
        tag = self._source_tag()
        return os.path.join(
            self.dir, "%s_%s_%s_%df_v%d.%s"
            % (tag, complexity, res_key, frames, GRAPH_VERSION, ext))

    def get(self, res_key, complexity):
        key = (res_key, complexity)
        if key in self._cache:
            return self._cache[key]

        width, height = RES_BY_KEY[res_key]
        frames = self.frames_for(res_key)
        path = self.path_for(res_key, complexity)
        clip = SourceClip(path, res_key, complexity, width, height, frames, self.fmt)

        if not (os.path.exists(path) and os.path.getsize(path) > 1024):
            self._generate(clip)
        else:
            detail("reusing cached source %s" % os.path.basename(path))

        clip.decode_fps = self._measure_decode(clip)
        self._cache[key] = clip
        return clip

    def _generate(self, clip):
        expected = int(BYTES_PER_FRAME_RAW[clip.res_key] * clip.frames
                       * (1.0 if self.fmt == "raw" else LOSSLESS_RATIO))
        ffmpeg_setup.check_space(self.dir, int(expected * 1.4))

        step("Generating %s-complexity %s source (%d frames, %s)"
             % (clip.complexity, clip.res_key, clip.frames, self.fmt))

        cmd = [self.ff.path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        if self.user_source:
            cmd += ["-i", self.user_source,
                    "-vf", "scale=%d:%d:flags=lanczos,format=yuv420p"
                    % (clip.width, clip.height),
                    "-an", "-sn", "-frames:v", str(clip.frames)]
        else:
            cmd += ["-filter_complex", _graph(clip.complexity, clip.width, clip.height),
                    "-frames:v", str(clip.frames)]

        tmp = clip.path + ".partial"
        if self.fmt == "raw":
            cmd += ["-f", "yuv4mpegpipe", "-strict", "-1", tmp]
        else:
            # The temp name ends in .partial, so the muxer cannot be inferred
            # from the extension and must be given explicitly.
            cmd += lossless_args(self.lossless_encoder) + ["-f", "matroska", tmp]

        p = run(cmd, timeout=1800)
        if not p.ok or not os.path.exists(tmp):
            _unlink(tmp)
            raise RuntimeError("failed to generate test source %s:\n  %s"
                               % (os.path.basename(clip.path),
                                  (p.err or p.out).strip()[:400]))
        os.replace(tmp, clip.path)
        detail("wrote %s (%s)" % (os.path.basename(clip.path),
                                  util.human_bytes(os.path.getsize(clip.path))))

    def _measure_decode(self, clip):
        """Decode-only throughput: the ceiling every encode result sits under.

        Also the cheapest place to learn the clip's stored frame rate: this
        invocation already prints the stream line.
        """
        cmd = [self.ff.path, "-nostdin", "-hide_banner", "-benchmark",
               "-i", clip.path, "-f", "null", "-"]
        p = run(cmd, timeout=600)
        clip.rate = _detect_rate(clip.path, (p.err or "") + (p.out or ""))
        if not p.ok:
            return None
        m = re.search(r"bench: utime=\S+ stime=\S+ rtime=([\d.]+)s", p.err or p.out)
        if not m:
            return None
        rtime = float(m.group(1))
        if rtime <= 0:
            return None
        fps = clip.frames / rtime
        detail("decode baseline %s/%s: %.0f fps" % (clip.complexity, clip.res_key, fps))
        return fps

    def clips(self):
        """Every clip generated or reused during this run."""
        return list(self._cache.values())

    def cached_bytes(self):
        total = 0
        for path in glob.glob(os.path.join(self.dir, "*")):
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    def cleanup(self):
        for path in glob.glob(os.path.join(self.dir, "*")):
            _unlink(path)


# "F<num>:<den>" in a yuv4mpeg header is the frame rate, exactly.
_Y4M_RATE_RE = re.compile(rb"\bF(\d+):(\d+)\b")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s+fps\b")


def _detect_rate(path, ffmpeg_text=""):
    """Stored frame rate of a clip, preferring the container's own header.

    Needed because an encode retimes the clip to its target framerate, and the
    scale factor is (stored rate / target rate). Guessing BASE_RATE here would
    silently mis-time any --source footage.
    """
    try:
        with open(path, "rb") as fh:
            header = fh.readline(512)
        m = _Y4M_RATE_RE.search(header)
        if m and int(m.group(2)):
            return int(m.group(1)) / float(m.group(2))
    except OSError:
        pass
    m = _FPS_RE.search(ffmpeg_text or "")
    if m:
        try:
            value = float(m.group(1))
            if value > 0:
                return value
        except ValueError:
            pass
    detail("could not read the frame rate of %s; assuming %d fps"
           % (os.path.basename(path), BASE_RATE))
    return float(BASE_RATE)


def _unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass
