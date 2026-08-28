"""Shared helpers: subprocess wrapper, ANSI color, formatting."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time

DEFAULT_TIMEOUT = 120


class Proc:
    """Result of a completed subprocess."""

    __slots__ = ("rc", "out", "err", "seconds", "timed_out")

    def __init__(self, rc, out="", err="", seconds=0.0, timed_out=False):
        self.rc = rc
        self.out = out
        self.err = err
        self.seconds = seconds
        self.timed_out = timed_out

    @property
    def ok(self):
        return self.rc == 0

    def __repr__(self):
        return "Proc(rc=%r, %.2fs, out=%dB, err=%dB)" % (
            self.rc, self.seconds, len(self.out), len(self.err))


# Every ffmpeg this process starts is tracked here so a signal handler can tear
# down the whole tree. Source generation and encoder probes are long-running
# (up to 30 minutes for a 2160p clip) and used to survive SIGTERM because they
# were started outside the runner's registry.
_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_ABORTED = threading.Event()


def register(proc):
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)
    return proc


def unregister(proc):
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)


def kill_group(proc):
    """Kill a child and everything it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except Exception:
            pass


def terminate_all():
    """Tear down every tracked child. Safe to call more than once."""
    _ABORTED.set()
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
        _ACTIVE.clear()
    for proc in procs:
        kill_group(proc)
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def aborted():
    return _ABORTED.is_set()


def reset_abort():
    _ABORTED.clear()


def spawn(cmd, env=None, cwd=None):
    """Start a tracked child in its own process group."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    return register(proc)


def run(cmd, timeout=DEFAULT_TIMEOUT, env=None, cwd=None):
    """Run a command to completion, capturing output. Never raises."""
    start = time.monotonic()
    try:
        proc = spawn(cmd, env=env, cwd=cwd)
    except FileNotFoundError:
        return Proc(127, "", "command not found: %s" % (cmd[0],), time.monotonic() - start)
    except PermissionError:
        return Proc(126, "", "not executable: %s" % (cmd[0],), time.monotonic() - start)
    except OSError as e:
        return Proc(1, "", str(e), time.monotonic() - start)

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = b"", b""
        unregister(proc)
        return Proc(-9, _dec(out), _dec(err) or "timed out after %ss" % timeout,
                    time.monotonic() - start, timed_out=True)
    except Exception as e:
        kill_group(proc)
        unregister(proc)
        return Proc(1, "", str(e), time.monotonic() - start)
    unregister(proc)
    return Proc(proc.returncode, _dec(out), _dec(err), time.monotonic() - start)


def _dec(b):
    if not b:
        return ""
    if isinstance(b, str):
        return b
    return b.decode("utf-8", "replace")


def which(name):
    return shutil.which(name)


# --------------------------------------------------------------------------
# color
# --------------------------------------------------------------------------

_COLOR = True


def init_color(force=None):
    """Enable/disable ANSI color. force=None means auto-detect."""
    global _COLOR
    if force is not None:
        _COLOR = bool(force)
        return _COLOR
    if os.environ.get("NO_COLOR"):
        _COLOR = False
    elif os.environ.get("TERM", "") == "dumb":
        _COLOR = False
    else:
        _COLOR = sys.stdout.isatty()
    return _COLOR


def color_enabled():
    return _COLOR


def _wrap(code):
    def fn(text):
        if not _COLOR:
            return str(text)
        return "\033[%sm%s\033[0m" % (code, text)
    return fn


bold = _wrap("1")
dim = _wrap("2")
red = _wrap("31")
green = _wrap("32")
yellow = _wrap("33")
blue = _wrap("34")
magenta = _wrap("35")
cyan = _wrap("36")
grey = _wrap("90")


def visible_len(s):
    """Length of a string ignoring ANSI escape sequences."""
    out = 0
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\033":
            while i < n and s[i] != "m":
                i += 1
            i += 1
        else:
            out += 1
            i += 1
    return out


def pad(s, width, align="left"):
    """Pad to a visible width, ANSI-aware."""
    gap = width - visible_len(s)
    if gap <= 0:
        return s
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def term_width(default=100):
    try:
        w = shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default
    return max(60, min(w, 200))


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

VERBOSITY = 1  # 0=quiet 1=normal 2=verbose


def set_verbosity(level):
    global VERBOSITY
    VERBOSITY = level


def info(msg):
    if VERBOSITY >= 1:
        sys.stderr.write("%s\n" % msg)
        sys.stderr.flush()


def step(msg):
    if VERBOSITY >= 1:
        sys.stderr.write("%s %s\n" % (cyan("::"), bold(msg)))
        sys.stderr.flush()


def detail(msg):
    if VERBOSITY >= 2:
        sys.stderr.write("%s %s\n" % (grey("  ."), grey(msg)))
        sys.stderr.flush()


def warn(msg):
    if VERBOSITY >= 1:
        sys.stderr.write("%s %s\n" % (yellow("warning:"), msg))
        sys.stderr.flush()


def error(msg):
    sys.stderr.write("%s %s\n" % (red("error:"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def human_bytes(n):
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            if unit == "B":
                return "%d B" % int(n)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TiB" % n


def human_time(seconds):
    if seconds is None:
        return "-"
    seconds = float(seconds)
    if seconds < 1:
        return "%dms" % int(seconds * 1000)
    if seconds < 60:
        return "%.1fs" % seconds
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm" % (h, m)


def human_rate(bps):
    """Bits per second -> human string."""
    if bps is None:
        return "-"
    if bps >= 1e6:
        return "%.1f Mbps" % (bps / 1e6)
    if bps >= 1e3:
        return "%.0f kbps" % (bps / 1e3)
    return "%.0f bps" % bps


def fmt_num(v, digits=1):
    if v is None:
        return "-"
    return "%.*f" % (digits, v)
