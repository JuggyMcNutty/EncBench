"""System inventory and encoder discovery.

The important idea here: `ffmpeg -encoders` is a list of what the *binary* was
compiled with, not what the *machine* can do. h264_nvenc, h264_qsv, h264_amf and
h264_v4l2m2m all appear on hardware that has none of those engines and fail only
at runtime. So every candidate gets a real 2-frame encode before it is believed,
and the exact invocation that worked is recorded and reused by the runner.
"""

from __future__ import annotations

import glob
import os
import re
from concurrent.futures import ThreadPoolExecutor

from . import util
from .util import detail, run, warn

# Codec families worth benchmarking by default. --all-encoders lifts this.
DEFAULT_CODECS = {"h264", "hevc", "av1", "vp9", "vp8"}
EXTENDED_CODECS = DEFAULT_CODECS | {"mpeg2video", "mpeg4", "vvc", "theora", "prores"}

# Encoders that are not really video compressors for our purposes.
ALWAYS_SKIP = {"wrapped_avframe", "rawvideo", "bitpacked", "vnull"}

# Valid encoders whose numbers are not comparable in a YUV benchmark.
SKIP_UNLESS_ALL = {"libx264rgb", "libopenjpeg"}

HW_SUFFIXES = ("_vaapi", "_nvenc", "_qsv", "_amf", "_v4l2m2m", "_rkmpp",
               "_mediacodec", "_videotoolbox", "_omx", "_vulkan")

VENDOR_NAMES = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA",
                "0x1af4": "virtio", "0x15ad": "VMware", "0x1234": "QEMU"}

# Fastest -> slowest. Used where ffmpeg reports a free-form string option.
X26X_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow"]

KNOWN_SPEED_KNOB = {
    "libx264":    ("preset", X26X_PRESETS),
    "libx265":    ("preset", X26X_PRESETS),
    "libx262":    ("preset", X26X_PRESETS),
    "libsvtav1":  ("preset", ["12", "10", "8", "6", "4"]),
    "libvpx-vp9": ("cpu-used", ["8", "6", "4", "2", "0"]),
    "libvpx":     ("cpu-used", ["8", "6", "4", "2", "0"]),
    "libaom-av1": ("cpu-used", ["8", "6", "4", "2"]),
    "librav1e":   ("speed", ["10", "8", "6", "4"]),
    "libtheora":  (None, []),
}

# Hardware families whose speed knob is not a documented fast-to-slow ladder.
# VAAPI's -quality is defined by the driver: on AMD/VCN levels 1-3 and 8 run at
# one speed and 4-7 at roughly half, so the values are worth sweeping but must
# not be treated as an ordered preset ladder, and the driver default is what a
# user actually gets. NVENC's p1..p7 is a real ladder and is left alone.
HW_SPEED_KNOB = {
    "vaapi": ("quality", ["1", "4", "7"]),
}


class EncoderSpec(object):
    """A video encoder plus the exact invocation proven to work on this box."""

    def __init__(self, name, codec, flags, description):
        self.name = name
        self.codec = codec
        self.flags = flags
        self.description = description
        self.family = _family(name)
        self.hardware = self.family != "software"
        self.device = None
        self.pre_input = []        # global args placed before -i
        self.filters = []          # filter chain prepended to every encode
        self.extra_args = []       # e.g. -strict experimental
        self.ok = False
        self.error = ""
        self.speed_knob = None     # e.g. 'preset'
        self.speed_values = []     # fastest -> slowest
        self.pix_fmts = []
        # True when speed_values form a documented fastest-to-slowest ladder.
        # When False the values are still worth measuring, but the baseline
        # stays on the driver default rather than the middle of the list.
        self.knob_is_ladder = True
        self.rc_options = set()
        self.probe_seconds = 0.0

    @property
    def label(self):
        return self.name

    def speed_triple(self):
        """A fast / balanced / slow sample of this encoder's speed knob."""
        vals = self.speed_values
        if not vals:
            return [None]
        if len(vals) == 1:
            return [vals[0]]
        if len(vals) == 2:
            return [vals[0], vals[-1]]
        return [vals[0], vals[len(vals) // 2], vals[-1]]

    def as_dict(self):
        return {
            "name": self.name, "codec": self.codec, "family": self.family,
            "hardware": self.hardware, "device": self.device,
            "description": self.description, "ok": self.ok, "error": self.error,
            "speed_knob": self.speed_knob, "speed_values": self.speed_values,
            "knob_is_ladder": self.knob_is_ladder,
            "pix_fmts": self.pix_fmts[:12],
            "rc_options": sorted(self.rc_options),
            "pre_input": self.pre_input, "filters": self.filters,
            "extra_args": self.extra_args,
        }

    def __repr__(self):
        return "EncoderSpec(%s, %s, ok=%s)" % (self.name, self.family, self.ok)


def _family(name):
    for suffix in HW_SUFFIXES:
        if name.endswith(suffix):
            return suffix.lstrip("_")
    return "software"


# --------------------------------------------------------------------------
# system inventory
# --------------------------------------------------------------------------

def system_info():
    info = {
        "hostname": _hostname(),
        "kernel": os.uname().release,
        "arch": os.uname().machine,
        "distro": _distro(),
        "cpu": _cpu_info(),
        "memory_bytes": _meminfo("MemTotal"),
        "memory_available_bytes": _meminfo("MemAvailable"),
        "governor": _governor(),
        "container": _container(),
        "loadavg": _loadavg(),
        "gpus": gpu_info(),
    }
    return info


def _hostname():
    try:
        return os.uname().nodename
    except Exception:
        return "unknown"


def _distro():
    try:
        data = {}
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    data[k] = v.strip().strip('"')
        return data.get("PRETTY_NAME") or data.get("NAME") or "unknown"
    except OSError:
        return "unknown"


def _cpu_info():
    model, sockets, cores_per_socket = "unknown", 1, 0
    physical_ids, core_ids = set(), set()
    logical = 0
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if ":" not in line:
                    continue
                key, val = [x.strip() for x in line.split(":", 1)]
                if key == "model name" and model == "unknown":
                    model = val
                elif key == "Hardware" and model == "unknown":
                    model = val          # ARM boards
                elif key == "processor":
                    logical += 1
                elif key == "physical id":
                    physical_ids.add(val)
                elif key == "core id":
                    core_ids.add(val)
    except OSError:
        pass
    if not logical:
        logical = os.cpu_count() or 1
    sockets = max(1, len(physical_ids))
    cores_per_socket = len(core_ids) or 0
    physical = cores_per_socket * sockets if cores_per_socket else logical
    return {
        "model": model,
        "logical": logical,
        "physical": physical,
        "sockets": sockets,
        "smt": logical > physical,
        "max_mhz": _max_mhz(),
        "affinity": _affinity_count(),
        "quota": _cgroup_quota(),
    }


def _affinity_count():
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _cgroup_quota():
    """CPU limit imposed by a container, in cores. None if unlimited."""
    try:
        with open("/sys/fs/cgroup/cpu.max") as fh:              # cgroup v2
            parts = fh.read().split()
            if parts and parts[0] != "max":
                return round(int(parts[0]) / int(parts[1]), 2)
            return None
    except OSError:
        pass
    try:
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as fh:  # cgroup v1
            quota = int(fh.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as fh:
            period = int(fh.read().strip())
        if quota > 0 and period > 0:
            return round(quota / period, 2)
    except (OSError, ValueError):
        pass
    return None


def _max_mhz():
    best = 0
    for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq"):
        try:
            with open(path) as fh:
                best = max(best, int(fh.read().strip()) // 1000)
        except (OSError, ValueError):
            continue
    return best or None


def _governor():
    govs = set()
    for path in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
        try:
            with open(path) as fh:
                govs.add(fh.read().strip())
        except OSError:
            continue
    return "+".join(sorted(govs)) if govs else None


def _meminfo(key):
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _loadavg():
    try:
        return list(os.getloadavg())
    except OSError:
        return [0.0, 0.0, 0.0]


def _container():
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.path.exists("/run/.containerenv"):
        return "podman/toolbox"
    try:
        with open("/proc/1/cgroup") as fh:
            text = fh.read()
        for marker in ("docker", "lxc", "kubepods", "containerd"):
            if marker in text:
                return marker
    except OSError:
        pass
    if os.environ.get("container"):
        return os.environ["container"]
    return None


def gpu_info():
    """Enumerate DRM render nodes; each is a potentially independent encoder."""
    gpus = []
    pci_names = _lspci_names()
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        entry = {"render_node": node, "vendor": None, "vendor_id": None,
                 "device_id": None, "name": None, "driver": None,
                 "writable": os.access(node, os.W_OK)}
        sysfs = "/sys/class/drm/%s/device" % os.path.basename(node)
        entry["vendor_id"] = _read(os.path.join(sysfs, "vendor"))
        entry["device_id"] = _read(os.path.join(sysfs, "device"))
        entry["vendor"] = VENDOR_NAMES.get(entry["vendor_id"] or "", entry["vendor_id"])
        try:
            entry["driver"] = os.path.basename(os.readlink(os.path.join(sysfs, "driver")))
        except OSError:
            pass
        slot = None
        try:
            slot = os.path.basename(os.readlink(sysfs))
        except OSError:
            pass
        if slot:
            entry["pci_slot"] = slot
            short = slot.split(":", 1)[1] if ":" in slot else slot
            entry["name"] = pci_names.get(short) or pci_names.get(slot)
        gpus.append(entry)

    for line in _nvidia_smi():
        gpus.append({"render_node": None, "vendor": "NVIDIA", "name": line,
                     "driver": "nvidia", "writable": True})
    return gpus


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _lspci_names():
    names = {}
    if not util.which("lspci"):
        return names
    p = run(["lspci"], timeout=15)
    if not p.ok:
        return names
    for line in p.out.splitlines():
        m = re.match(r"^(\S+)\s+(?:VGA compatible controller|3D controller|Display controller):\s*(.*)$", line)
        if m:
            names[m.group(1)] = m.group(2).strip()
    return names


def _nvidia_smi():
    if not util.which("nvidia-smi"):
        return []
    p = run(["nvidia-smi", "-L"], timeout=20)
    if not p.ok:
        return []
    return [l.strip() for l in p.out.splitlines() if l.strip().startswith("GPU ")]


def render_nodes(sysinfo):
    nodes = [g["render_node"] for g in sysinfo.get("gpus", [])
             if g.get("render_node") and g.get("writable")]
    return nodes


# --------------------------------------------------------------------------
# encoder discovery
# --------------------------------------------------------------------------

def list_encoders(ff):
    """Parse `ffmpeg -encoders` into EncoderSpec objects (unvalidated)."""
    p = run([ff.path, "-hide_banner", "-encoders"], timeout=60)
    if not p.ok:
        raise RuntimeError("ffmpeg -encoders failed: %s" % (p.err or p.out)[:400])
    specs = []
    started = False
    for line in p.out.splitlines():
        if not started:
            if line.strip().startswith("------"):
                started = True
            continue
        m = re.match(r"^\s*([VASD.][.FSXBD]{5})\s+(\S+)\s*(.*)$", line)
        if not m:
            continue
        flags, name, desc = m.group(1), m.group(2), m.group(3).strip()
        if flags[0] != "V":
            continue
        if name in ALWAYS_SKIP:
            continue
        codec_match = re.search(r"\(codec ([^)]+)\)", desc)
        codec = codec_match.group(1).strip() if codec_match else name
        specs.append(EncoderSpec(name, codec, flags, desc))
    return specs


def probe_encoders(ff, sysinfo, all_encoders=False, only=None, exclude=None,
                   extended=False):
    """Discover, filter, then prove each encoder by running it."""
    specs = list_encoders(ff)

    allowed = EXTENDED_CODECS if extended else DEFAULT_CODECS
    if only:
        wanted = set(only)
        specs = [s for s in specs if s.name in wanted]
    elif not all_encoders:
        specs = [s for s in specs
                 if s.codec in allowed and s.name not in SKIP_UNLESS_ALL]
    if exclude:
        skip = set(exclude)
        specs = [s for s in specs if s.name not in skip]

    nodes = render_nodes(sysinfo)
    for spec in specs:
        _load_options(ff, spec)

    sw = [s for s in specs if not s.hardware]
    hw = [s for s in specs if s.hardware]

    # Software probes are independent; hardware probes go one at a time so a
    # shared encode engine is never the reason a probe fails.
    if sw:
        workers = min(4, max(1, (os.cpu_count() or 2) // 2))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda s: _validate(ff, s, nodes), sw))
    for spec in hw:
        _validate(ff, spec, nodes)

    specs.sort(key=lambda s: (not s.ok, s.codec, s.hardware, s.name))
    return specs


def _load_options(ff, spec):
    """Read `-h encoder=NAME` for the speed knob and pixel formats."""
    p = run([ff.path, "-hide_banner", "-h", "encoder=" + spec.name], timeout=30)
    text = p.out or p.err
    if not text:
        return

    m = re.search(r"Supported pixel formats:\s*(.*)", text)
    if m:
        spec.pix_fmts = m.group(1).split()

    knob, values = _speed_knob(text, spec.name)
    if not knob and spec.family in HW_SPEED_KNOB:
        candidate, candidate_values = HW_SPEED_KNOB[spec.family]
        if _has_option(text, candidate):
            knob, values = candidate, list(candidate_values)
            spec.knob_is_ladder = False
    spec.speed_knob = knob
    spec.speed_values = values

    # Which rate-control knobs this encoder actually exposes, so the quality
    # mode picks a real option instead of guessing per codec.
    for opt in ("crf", "qp", "cq", "global_quality", "rc", "rc_mode",
                "quality", "maxrate", "qmin"):
        if _has_option(text, opt):
            spec.rc_options.add(opt)


def _speed_knob(text, name):
    """Find the option that trades speed for compression, and its values."""
    if name in KNOWN_SPEED_KNOB:
        knob, values = KNOWN_SPEED_KNOB[name]
        if knob is None:
            return None, []
        if _has_option(text, knob):
            return knob, list(values)

    for candidate in ("preset", "speed", "cpu-used", "quality",
                      "compression_level", "deadline", "usage"):
        block = _option_block(text, candidate)
        if block is None:
            continue
        enumerated = _enumerated_values(block)
        if enumerated:
            return candidate, enumerated
        rng = re.search(r"\(from (-?\d+) to (-?\d+)\)", block[0])
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            return candidate, _sample_range(lo, hi, candidate)
    return None, []


def _has_option(text, name):
    return re.search(r"^\s+-%s\s" % re.escape(name), text, re.M) is not None


def _option_block(text, name):
    """Return (header_line, [child value lines]) for an AVOption, or None."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s+-%s\s+<" % re.escape(name), line):
            children = []
            for child in lines[i + 1:]:
                if re.match(r"^\s{5,}\S+\s+-?\d+\s+E", child):
                    children.append(child)
                elif child.strip() == "":
                    continue
                else:
                    break
            return (line, children)
    return None


def _enumerated_values(block):
    header, children = block
    values = []
    for child in children:
        m = re.match(r"^\s+(\S+)\s+(-?\d+)\s+E", child)
        if m:
            values.append(m.group(1))
    # Drop meta-values that just alias something else.
    values = [v for v in values if v not in ("default", "auto", "unknown")]
    if not values:
        return []
    # NVENC exposes both legacy names (slow/medium/fast) and p1..p7; prefer p*.
    p_values = [v for v in values if re.match(r"^p\d$", v)]
    if p_values:
        return sorted(p_values)          # p1 fastest -> p7 slowest
    return values


def _sample_range(lo, hi, knob):
    """Pick a fastest->slowest sample across a numeric option range."""
    lo = max(lo, -16)
    hi = min(hi, 16)
    if hi <= lo:
        return [str(lo)]
    span = list(range(lo, hi + 1))
    # For cpu-used / speed style knobs, higher = faster. For preset-style
    # integers (SVT-AV1), higher is also faster. compression_level is inverted.
    if knob in ("compression_level",):
        ordered = span
    else:
        ordered = list(reversed(span))
    if len(ordered) <= 5:
        return [str(v) for v in ordered]
    idx = [0, len(ordered) // 4, len(ordered) // 2, (3 * len(ordered)) // 4, len(ordered) - 1]
    seen, out = set(), []
    for i in idx:
        v = str(ordered[i])
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# --------------------------------------------------------------------------
# validation: actually run the encoder
# --------------------------------------------------------------------------

def _variants(spec, nodes):
    """Candidate invocations to try, in order of preference."""
    fam = spec.family
    if fam == "vaapi":
        out = []
        for node in nodes or ["/dev/dri/renderD128"]:
            out.append({
                "device": node,
                "pre_input": ["-init_hw_device", "vaapi=va:" + node,
                              "-filter_hw_device", "va"],
                "filters": ["format=nv12", "hwupload"],
            })
        return out
    if fam == "qsv":
        return [
            {"device": None, "pre_input": [], "filters": []},
            {"device": "qsv", "pre_input": ["-init_hw_device", "qsv=hw",
                                            "-filter_hw_device", "hw"],
             "filters": ["format=nv12", "hwupload=extra_hw_frames=64"]},
        ]
    if fam == "vulkan":
        out = []
        for index in range(max(1, len(nodes))):
            out.append({
                "device": "vulkan:%d" % index,
                "pre_input": ["-init_hw_device", "vulkan=vk:%d" % index,
                              "-filter_hw_device", "vk"],
                "filters": ["format=nv12", "hwupload"],
            })
        return out
    if fam == "v4l2m2m":
        if not glob.glob("/dev/video*"):
            return []
        return [{"device": None, "pre_input": [], "filters": []}]
    return [{"device": None, "pre_input": [], "filters": []}]


def _validate(ff, spec, nodes):
    """Run a 2-frame encode. Only an exit code of 0 counts as support."""
    variants = _variants(spec, nodes)
    if not variants:
        spec.ok = False
        spec.error = "no suitable device present"
        return spec

    extra = []
    if spec.flags[3] == "X":
        extra = ["-strict", "experimental"]

    last_err = ""
    for variant in variants:
        cmd = [ff.path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        cmd += variant["pre_input"]
        cmd += ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-frames:v", "2"]
        if variant["filters"]:
            cmd += ["-vf", ",".join(variant["filters"])]
        cmd += ["-c:v", spec.name] + extra + ["-f", "null", "-"]

        p = run(cmd, timeout=45)
        spec.probe_seconds += p.seconds
        if p.ok:
            spec.ok = True
            spec.device = variant["device"]
            spec.pre_input = list(variant["pre_input"])
            spec.filters = list(variant["filters"])
            spec.extra_args = extra
            detail("probe ok: %-18s %s" % (spec.name, spec.device or ""))
            return spec
        last_err = _first_error(p.err or p.out) or ("exit %d" % p.rc)
        if p.timed_out:
            last_err = "timed out during probe"

    spec.ok = False
    spec.error = last_err
    detail("probe failed: %-15s %s" % (spec.name, last_err))
    return spec


def _looks_like_message(line):
    """Reject banner rules and decoration; keep lines with actual words."""
    stripped = line.strip()
    if len(stripped) < 3:
        return False
    letters = sum(1 for ch in stripped if ch.isalpha())
    return letters >= 3


def _first_error(text):
    best = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or not _looks_like_message(line):
            continue
        if line.startswith("[") and "]" in line:
            line = line.split("]", 1)[1].strip()
        if not _looks_like_message(line):
            continue
        lowered = line.lower()
        # Prefer a line that actually names a failure over a version banner.
        if any(word in lowered for word in
               ("error", "failed", "cannot", "unable", "not supported",
                "no such", "invalid", "unsupported", "denied")):
            return line[:160]
        if not best:
            best = line[:160]
    return best


# --------------------------------------------------------------------------
# diagnostics: turn probe failures into something a user can act on
# --------------------------------------------------------------------------

PKG_HINTS = {
    "vaapi": {
        "arch":   "sudo pacman -S libva libva-mesa-driver   # Intel: intel-media-driver",
        "debian": "sudo apt install libva2 libva-drm2 va-driver-all",
        "fedora": "sudo dnf install libva mesa-va-drivers    # Intel: intel-media-driver",
        "suse":   "sudo zypper install libva2 libva-vdpau-driver",
        "alpine": "sudo apk add libva libva-utils",
        None:     "install your distribution's libva runtime package",
    },
    "nvenc": {
        "arch":   "sudo pacman -S nvidia-utils",
        "debian": "sudo apt install nvidia-driver libnvidia-encode1",
        "fedora": "sudo dnf install akmod-nvidia xorg-x11-drv-nvidia-cuda",
        "suse":   "install the NVIDIA proprietary driver",
        "alpine": "install the NVIDIA proprietary driver",
        None:     "install the NVIDIA proprietary driver (provides libcuda / libnvidia-encode)",
    },
    "qsv": {
        "arch":   "sudo pacman -S intel-media-driver libva vpl-gpu-rt",
        "debian": "sudo apt install intel-media-va-driver-non-free libmfx-gen1.2",
        "fedora": "sudo dnf install intel-media-driver libvpl",
        "suse":   "sudo zypper install intel-media-driver",
        "alpine": "sudo apk add intel-media-driver",
        None:     "install the Intel media driver and oneVPL runtime",
    },
}


def _distro_key():
    try:
        data = {}
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    data[k] = v.strip().strip('"')
    except OSError:
        return None
    ident = (data.get("ID") or "").lower()
    like = (data.get("ID_LIKE") or "").lower()
    blob = ident + " " + like
    for key, markers in (
        ("arch", ("arch", "manjaro", "endeavouros")),
        ("debian", ("debian", "ubuntu", "mint", "pop")),
        ("fedora", ("fedora", "rhel", "centos", "rocky", "alma")),
        ("suse", ("suse", "opensuse")),
        ("alpine", ("alpine",)),
    ):
        if any(m in blob for m in markers):
            return key
    return None


def _pkg_hint(kind):
    table = PKG_HINTS.get(kind, {})
    return table.get(_distro_key()) or table.get(None) or ""


def _gpu_vendors(sysinfo):
    return {(g.get("vendor") or "").upper() for g in sysinfo.get("gpus", [])}


def diagnose(specs, sysinfo):
    """Explain why hardware encoders were rejected, and what would fix it.

    Distinguishes 'this machine has no such hardware' (normal, informational)
    from 'the hardware is right there but its runtime is missing' (actionable).
    """
    failed = [s for s in specs if not s.ok]
    if not failed:
        return []

    vendors = _gpu_vendors(sysinfo)
    has_amd = "AMD" in vendors
    has_intel = "INTEL" in vendors
    has_nvidia = "NVIDIA" in vendors or bool(util.which("nvidia-smi"))
    hints = []

    def names(pred):
        return sorted(s.name for s in failed if pred(s))

    # Families where at least one encoder works: a failure there means the
    # engine lacks that codec, not that the backend is missing.
    working = {s.family for s in specs if s.ok}

    def partial_hint(family, encoders):
        codecs = sorted({e.split("_")[0] for e in encoders})
        return {
            "severity": "info",
            "title": "%s: %s not supported by this hardware" % (
                family.upper(), "/".join(codecs)),
            "lines": ["The %s backend works on this system, but its encode engine "
                      "has no %s support." % (family.upper(), "/".join(codecs))],
            "encoders": encoders,
        }

    # --- VAAPI: the common case where a working GPU is one package away ----
    va_partial = names(lambda s: s.family == "vaapi" and "vaapi" in working)
    if va_partial:
        hints.append(partial_hint("vaapi", va_partial))
    # Two distinct fixable failures: the libva loader is absent (a .so message),
    # or libva is present but no VA driver answers ("Failed to initialise VAAPI
    # connection"). Matching only the loader message left the more common case
    # falling through to a raw error line with no guidance.
    va_broken = re.compile(
        r"libva[^\s]*\.so|failed to initialise vaapi|failed to initialize vaapi"
        r"|unknown libva error|no va driver|vaInitialize failed", re.I)
    va_missing = names(lambda s: s.family in ("vaapi", "qsv")
                       and s.name not in va_partial
                       and va_broken.search(s.error or ""))
    if va_missing and (has_amd or has_intel):
        driver_present = _va_driver_present()
        detail_lines = [
            "An %s GPU is present at %s, but ffmpeg cannot load the VA-API runtime"
            % ("/".join(sorted(vendors & {"AMD", "INTEL"})) or "GPU",
               ", ".join(render_nodes(sysinfo)) or "a render node"),
            "(libva) or no VA driver answered. %s"
            % ("A VA driver is installed, so the libva runtime is the missing piece."
               if driver_present
               else "No VA driver was found either, so both are needed."),
            "Fix:  %s" % _pkg_hint("vaapi"),
            "Then re-run; verify independently with: vainfo",
        ]
        hints.append({
            "severity": "actionable",
            "title": "Hardware encoding is available but not usable yet (VA-API)",
            "lines": detail_lines,
            "encoders": va_missing,
        })
    elif va_missing:
        hints.append({
            "severity": "info",
            "title": "VA-API encoders unavailable",
            "lines": ["No Intel or AMD GPU render node was detected on this system."],
            "encoders": va_missing,
        })

    # --- NVENC ------------------------------------------------------------
    nv = names(lambda s: s.family == "nvenc")
    if nv and "nvenc" in working:
        hints.append(partial_hint("nvenc", nv))
    elif nv:
        if has_nvidia:
            hints.append({
                "severity": "actionable",
                "title": "NVIDIA GPU detected but NVENC could not initialise",
                "lines": ["Fix:  %s" % _pkg_hint("nvenc"),
                          "Inside a container, the driver must also be passed through "
                          "(e.g. --gpus all)."],
                "encoders": nv,
            })
        else:
            hints.append({
                "severity": "info",
                "title": "NVENC unavailable",
                "lines": ["No NVIDIA GPU in this system - expected."],
                "encoders": nv,
            })

    # --- QSV --------------------------------------------------------------
    qsv = names(lambda s: s.family == "qsv" and s.name not in va_missing
                and s.name not in va_partial)
    if qsv and "qsv" in working:
        hints.append(partial_hint("qsv", qsv))
    elif qsv:
        hints.append({
            "severity": "actionable" if has_intel else "info",
            "title": ("Intel GPU detected but Quick Sync could not initialise"
                      if has_intel else "Intel Quick Sync unavailable"),
            "lines": (["Fix:  %s" % _pkg_hint("qsv")] if has_intel
                      else ["No Intel GPU in this system - expected."]),
            "encoders": qsv,
        })

    # --- AMF --------------------------------------------------------------
    amf = names(lambda s: s.family == "amf")
    if amf:
        lines = ["AMF is AMD's proprietary encode runtime; on Linux it ships with "
                 "the amdgpu-pro stack and is rarely installed."]
        if has_amd:
            lines.append("VA-API is the normal path for AMD hardware encoding on Linux.")
        hints.append({"severity": "info", "title": "AMD AMF unavailable",
                      "lines": lines, "encoders": amf})

    # --- V4L2 M2M ---------------------------------------------------------
    v4l = names(lambda s: s.family == "v4l2m2m")
    if v4l:
        hints.append({
            "severity": "info",
            "title": "V4L2 memory-to-memory encoders unavailable",
            "lines": ["No /dev/video* M2M encoder device. These exist mainly on ARM "
                      "SoCs (Raspberry Pi, Rockchip, i.MX)."],
            "encoders": v4l,
        })

    # --- Vulkan -----------------------------------------------------------
    vk = names(lambda s: s.family == "vulkan")
    if vk:
        if "vulkan" in working:
            hints.append(partial_hint("vulkan", vk))
        else:
            hints.append({
                "severity": "info",
                "title": "Vulkan video encode unavailable",
                "lines": ["Requires a driver and GPU with Vulkan video encode extensions."],
                "encoders": vk,
            })

    # --- anything else ----------------------------------------------------
    covered = set()
    for h in hints:
        covered.update(h["encoders"])
    other = [s for s in failed if s.name not in covered]
    if other:
        hints.append({
            "severity": "info",
            "title": "Other encoders rejected",
            "lines": ["%s: %s" % (s.name, s.error or "failed") for s in other[:8]],
            "encoders": [s.name for s in other],
        })

    return hints


def _va_driver_present():
    for pattern in ("/usr/lib/dri/*_drv_video.so",
                    "/usr/lib64/dri/*_drv_video.so",
                    "/usr/lib/*/dri/*_drv_video.so"):
        if glob.glob(pattern):
            return True
    return False
