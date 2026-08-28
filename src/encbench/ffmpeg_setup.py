"""Locate a usable ffmpeg, or download a static build into scratch space.

Nothing here trusts a binary because it exists on disk -- every candidate is
executed and its version parsed before it is accepted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time

from . import util
from .util import run, detail, info, step, warn

# Static build sources. BtbN first: those builds enable the hardware encoders
# (NVENC / VAAPI / QSV / AMF). johnvansickle covers architectures BtbN does not
# publish, but may expose software encoders only -- probe.py re-derives the real
# capability set from whichever binary lands, so nothing here is assumed.
BTBN_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            "ffmpeg-master-latest-%s-gpl.tar.xz")
JVS_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-%s-static.tar.xz"

ARCH_SOURCES = {
    "x86_64":  [("btbn", "linux64"), ("jvs", "amd64")],
    "amd64":   [("btbn", "linux64"), ("jvs", "amd64")],
    "aarch64": [("btbn", "linuxarm64"), ("jvs", "arm64")],
    "arm64":   [("btbn", "linuxarm64"), ("jvs", "arm64")],
    "armv7l":  [("jvs", "armhf")],
    "armv6l":  [("jvs", "armhf")],
    "armhf":   [("jvs", "armhf")],
    "i686":    [("jvs", "i686")],
    "i386":    [("jvs", "i686")],
}

# -progress and -benchmark both predate 4.0; below that we are guessing.
MIN_VERSION = (4, 0)


class FFmpeg(object):
    """A validated ffmpeg installation."""

    def __init__(self, path, ffprobe, version_str, version, configuration, origin):
        self.path = path
        self.ffprobe = ffprobe
        self.version_str = version_str
        self.version = version          # tuple, (999,) for git master builds
        self.configuration = configuration
        self.origin = origin            # 'user' | 'system' | 'downloaded'
        self._filters = None

    # -- capability shims -------------------------------------------------

    def fps_mode_args(self, mode="passthrough"):
        """-fps_mode landed in 5.0; older releases need -vsync."""
        if self.version >= (5, 0):
            return ["-fps_mode", mode]
        return ["-vsync", "0" if mode == "passthrough" else "cfr"]

    def has_filter(self, name):
        if self._filters is None:
            p = run([self.path, "-hide_banner", "-filters"], timeout=30)
            self._filters = p.out
        return re.search(r"^\s*\S+\s+%s\s" % re.escape(name), self._filters, re.M) is not None

    def config_has(self, token):
        return token in self.configuration

    def as_dict(self):
        return {
            "path": self.path,
            "ffprobe": self.ffprobe,
            "version": self.version_str,
            "origin": self.origin,
            "configuration": self.configuration,
        }

    def __repr__(self):
        return "FFmpeg(%s, %s, %s)" % (self.path, self.version_str, self.origin)


# --------------------------------------------------------------------------
# scratch space
# --------------------------------------------------------------------------

def _mount_fstype(path):
    """Filesystem type of the mount point containing path."""
    try:
        with open("/proc/mounts") as fh:
            mounts = []
            for line in fh:
                parts = line.split()
                if len(parts) >= 3:
                    mounts.append((parts[1], parts[2]))
    except OSError:
        return "unknown"
    path = os.path.abspath(path)
    best, best_type = "", "unknown"
    for point, fstype in mounts:
        if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) > len(best):
            best, best_type = point, fstype
    return best_type


def total_ram():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def choose_scratch(override=None, needed_bytes=0):
    """Pick a scratch directory with room, avoiding filling a small tmpfs.

    Writing many GB of lossless video into a tmpfs consumes RAM, which on a
    memory-constrained box is exactly how a benchmark takes the machine down.
    """
    if override:
        d = os.path.abspath(os.path.expanduser(override))
        _mkdir(d)
        return d, _scratch_note(d, needed_bytes)

    candidates = []
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidates.append(os.path.join(tmpdir, "encbench"))
    candidates.append("/tmp/encbench")
    candidates.append("/var/tmp/encbench")
    candidates.append(os.path.expanduser("~/.cache/encbench"))

    ram = total_ram()
    fallback = None
    for cand in candidates:
        parent = os.path.dirname(cand)
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            continue
        if fallback is None:
            fallback = cand
        try:
            free = shutil.disk_usage(parent).free
        except OSError:
            continue
        if needed_bytes and free < needed_bytes:
            detail("scratch candidate %s: only %s free, need %s"
                   % (cand, util.human_bytes(free), util.human_bytes(needed_bytes)))
            continue
        fstype = _mount_fstype(parent)
        if fstype in ("tmpfs", "ramfs") and ram and needed_bytes > ram * 0.25:
            detail("scratch candidate %s is %s; %s would exceed 25%% of RAM"
                   % (cand, fstype, util.human_bytes(needed_bytes)))
            continue
        _mkdir(cand)
        return cand, _scratch_note(cand, needed_bytes)

    target = fallback or "/tmp/encbench"
    _mkdir(target)
    warn("no scratch directory has comfortable room for %s; using %s anyway"
         % (util.human_bytes(needed_bytes), target))
    return target, _scratch_note(target, needed_bytes)


def _scratch_note(path, needed_bytes):
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        free = 0
    return {
        "path": path,
        "fstype": _mount_fstype(path),
        "free_bytes": free,
        "estimated_need_bytes": needed_bytes,
    }


def check_space(path, needed_bytes):
    """Raise a clear error rather than filling somebody's filesystem."""
    try:
        free = shutil.disk_usage(path).free
    except OSError as e:
        raise RuntimeError("cannot stat scratch dir %s: %s" % (path, e))
    if free < needed_bytes:
        raise RuntimeError(
            "not enough space in %s: need ~%s, have %s free.\n"
            "  Use --scratch-dir DIR to point somewhere roomier, or reduce the\n"
            "  workload with --frames / --resolutions / --profile quick."
            % (path, util.human_bytes(needed_bytes), util.human_bytes(free)))


def _mkdir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise RuntimeError("cannot create scratch dir %s: %s" % (path, e))


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def parse_version(text):
    """('ffmpeg version 7.1.1 Copyright...') -> ('7.1.1', (7,1,1))."""
    m = re.search(r"ffmpeg version (\S+)", text)
    if not m:
        return None, None
    raw = m.group(1)
    # Git/master builds look like 'N-118234-gabc123' or '2026-06-21-git-...'
    nums = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", raw)
    if nums:
        parts = [int(nums.group(1)), int(nums.group(2))]
        if nums.group(3):
            parts.append(int(nums.group(3)))
        return raw, tuple(parts)
    return raw, (999,)


def validate(path, origin, quiet=False):
    """Execute a candidate binary; return FFmpeg or None."""
    if not path:
        return None
    p = run([path, "-hide_banner", "-version"], timeout=30)
    if not p.ok:
        if not quiet:
            detail("rejected %s: %s" % (path, (p.err or p.out).strip().splitlines()[:1]))
        return None
    text = p.out or p.err
    version_str, version = parse_version(text)
    if version is None:
        detail("rejected %s: unrecognised -version output" % path)
        return None
    if version < MIN_VERSION and version != (999,):
        warn("%s is ffmpeg %s; encbench needs >= %s"
             % (path, version_str, ".".join(str(x) for x in MIN_VERSION)))
        return None
    config = ""
    m = re.search(r"configuration:(.*)", text)
    if m:
        config = m.group(1).strip()
    ffprobe = _sibling(path, "ffprobe")
    return FFmpeg(path, ffprobe, version_str, version, config, origin)


def _sibling(ffmpeg_path, name):
    cand = os.path.join(os.path.dirname(os.path.abspath(ffmpeg_path)), name)
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return util.which(name)


# --------------------------------------------------------------------------
# acquisition
# --------------------------------------------------------------------------

def acquire(args, scratch):
    """Resolution order: --ffmpeg, $ENCBENCH_FFMPEG, cache, PATH, download."""
    cache_dir = os.path.join(scratch, "ffmpeg-static")

    if args.ffmpeg:
        ff = validate(os.path.expanduser(args.ffmpeg), "user")
        if not ff:
            raise RuntimeError("--ffmpeg %s is not a working ffmpeg binary" % args.ffmpeg)
        return ff

    env_path = os.environ.get("ENCBENCH_FFMPEG")
    if env_path:
        ff = validate(os.path.expanduser(env_path), "user")
        if ff:
            return ff
        warn("$ENCBENCH_FFMPEG=%s is not usable; ignoring" % env_path)

    if not args.download:
        cached = os.path.join(cache_dir, "ffmpeg")
        ff = validate(cached, "downloaded", quiet=True)
        if ff:
            detail("reusing cached static build at %s" % cached)
            return ff

        ff = validate(util.which("ffmpeg"), "system", quiet=True)
        if ff:
            detail("using system ffmpeg %s at %s" % (ff.version_str, ff.path))
            return ff

    if args.no_download:
        raise RuntimeError(
            "no usable ffmpeg found and --no-download was given.\n"
            "  Install one with your package manager, e.g.:\n"
            "    apt install ffmpeg   |   dnf install ffmpeg   |   pacman -S ffmpeg\n"
            "  or point at a binary with --ffmpeg /path/to/ffmpeg")

    ff = download_static(cache_dir)
    if not ff:
        raise RuntimeError(
            "could not obtain an ffmpeg binary.\n"
            "  Install ffmpeg via your package manager, or pass --ffmpeg /path/to/ffmpeg.")
    return ff


def download_static(cache_dir):
    """Fetch, extract and validate a static build. Returns FFmpeg or None."""
    arch = os.uname().machine
    sources = ARCH_SOURCES.get(arch)
    if not sources:
        warn("no known static build for architecture %r" % arch)
        return None

    _mkdir(cache_dir)
    step("No usable ffmpeg found - fetching a static build for %s" % arch)

    for kind, tag in sources:
        url = (BTBN_URL % tag) if kind == "btbn" else (JVS_URL % tag)
        archive = os.path.join(cache_dir, "download.tar.xz")
        info("   %s %s" % (util.grey("from"), url))
        if not _fetch(url, archive):
            warn("download failed from %s; trying next source" % kind)
            continue
        try:
            extracted = _extract_binaries(archive, cache_dir)
        except Exception as e:
            warn("could not extract archive from %s: %s" % (kind, e))
            _rm(archive)
            continue
        _rm(archive)
        if "ffmpeg" not in extracted:
            warn("archive from %s contained no ffmpeg binary" % kind)
            continue
        ff = validate(extracted["ffmpeg"], "downloaded")
        if ff:
            _write_build_info(cache_dir, kind, tag, url, ff)
            info("   %s ffmpeg %s (%s build)"
                 % (util.green("ok"), ff.version_str, kind))
            return ff
        warn("binary from %s did not run on this system; trying next source" % kind)

    return None


def _fetch(url, dest):
    """urllib first, then curl, then wget -- proxies and old CA stores vary."""
    if _fetch_urllib(url, dest):
        return True
    for cmd in (["curl", "-fL", "--retry", "2", "-o", dest, url],
                ["wget", "-q", "-O", dest, url]):
        if not util.which(cmd[0]):
            continue
        detail("retrying download with %s" % cmd[0])
        p = run(cmd, timeout=1800)
        if p.ok and os.path.exists(dest) and os.path.getsize(dest) > 1024 * 1024:
            return True
    return False


def _fetch_urllib(url, dest):
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "encbench/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last = 0.0
            with open(dest, "wb") as out:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last > 0.2:
                        last = now
                        _progress(done, total)
            _progress(done, total, final=True)
        return os.path.getsize(dest) > 1024 * 1024
    except Exception as e:
        detail("urllib download failed: %s" % e)
        _rm(dest)
        return False


def _progress(done, total, final=False):
    if util.VERBOSITY < 1 or not sys.stderr.isatty():
        return
    if total:
        pct = 100.0 * done / total
        bar_w = 28
        filled = int(bar_w * done / total)
        bar = "#" * filled + "-" * (bar_w - filled)
        msg = "   [%s] %5.1f%%  %s" % (bar, pct, util.human_bytes(done))
    else:
        msg = "   downloaded %s" % util.human_bytes(done)
    sys.stderr.write("\r" + msg + " " * 6)
    if final:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _extract_binaries(archive, dest):
    """Pull just ffmpeg/ffprobe out; layouts differ between build providers."""
    found = {}
    with tarfile.open(archive, "r:*") as tf:
        for member in tf:
            if not member.isfile():
                continue
            base = os.path.basename(member.name)
            if base not in ("ffmpeg", "ffprobe") or base in found:
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            out_path = os.path.join(dest, base)
            with open(out_path, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(out_path, os.stat(out_path).st_mode | stat.S_IXUSR | stat.S_IXGRP)
            found[base] = out_path
            if len(found) == 2:
                break
    return found


def _write_build_info(cache_dir, kind, tag, url, ff):
    try:
        with open(os.path.join(cache_dir, "build-info.json"), "w") as fh:
            json.dump({
                "provider": kind, "tag": tag, "url": url,
                "version": ff.version_str, "fetched": time.time(),
            }, fh, indent=2)
    except OSError:
        pass


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass
