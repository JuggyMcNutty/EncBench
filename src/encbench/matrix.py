"""Test plan generation.

A full cross product of encoder x resolution x fps x bitrate x preset is
thousands of runs and hours of machine time, and most of those runs tell you
nothing new. Instead this builds **axis sweeps around a baseline operating
point**: hold everything at the baseline, vary one dimension at a time. Each
result then isolates the effect of a single variable, and the plan stays small
enough to finish.

Cases carry a priority tier. Tiers are executed lowest-first and interleaved
across encoders, so a run that hits its time budget still has comparable
coverage for every encoder rather than complete data for the first few.
"""

from __future__ import annotations

from . import sources
from .runner import TestCase

# Target bitrates per resolution: (low, target, high), bits/sec.
BITRATE_LADDER = {
    "360p":  (0.5e6,  1.0e6,  2.0e6),
    "480p":  (1.0e6,  2.0e6,  4.0e6),
    "720p":  (2.0e6,  4.0e6,  8.0e6),
    "1080p": (3.0e6,  6.0e6, 12.0e6),
    "1440p": (6.0e6, 12.0e6, 24.0e6),
    "2160p": (15.0e6, 30.0e6, 60.0e6),
}
BITRATE_NAMES = ("low", "target", "high")

# Constant-quality levels per codec family, for --quality runs.
QUALITY_LEVELS = {
    "h264": (18, 23, 28), "hevc": (20, 26, 32),
    "av1": (25, 32, 40), "vp9": (26, 32, 40), "vp8": (26, 32, 40),
}
DEFAULT_QUALITY = (20, 26, 32)

# Priority tiers, executed in order.
TIER_BASELINE = 0
TIER_RESOLUTION = 1
TIER_PRESET = 2
TIER_BITRATE = 3
TIER_FPS = 4
TIER_COMPLEXITY = 5

TIER_NAMES = {
    TIER_BASELINE: "baseline", TIER_RESOLUTION: "resolution scaling",
    TIER_PRESET: "preset sweep", TIER_BITRATE: "bitrate sweep",
    TIER_FPS: "framerate sweep", TIER_COMPLEXITY: "content complexity",
}


class Profile(object):
    def __init__(self, name, resolutions, fps_list, bitrates, complexities,
                 preset_count, repeats, measure_seconds, budget_minutes,
                 concurrency_max, full_cross=False, quality=False):
        self.name = name
        self.resolutions = resolutions
        self.fps_list = fps_list
        self.bitrates = bitrates
        self.complexities = complexities
        self.preset_count = preset_count
        self.repeats = repeats
        self.measure_seconds = measure_seconds
        self.budget_minutes = budget_minutes
        self.concurrency_max = concurrency_max
        self.full_cross = full_cross
        self.quality = quality


def clone_profile(profile):
    """CLI overrides tune a profile; never mutate the shared definition."""
    import copy
    return copy.copy(profile)


PROFILES = {
    "quick": Profile(
        name="quick",
        resolutions=["720p", "1080p"],
        fps_list=[30],
        bitrates=["target"],
        complexities=["high"],
        preset_count=2,
        repeats=1,
        measure_seconds=1.5,
        budget_minutes=6,
        concurrency_max=4,
    ),
    "standard": Profile(
        name="standard",
        resolutions=["360p", "480p", "720p", "1080p", "1440p", "2160p"],
        fps_list=[30, 60],
        bitrates=["low", "target", "high"],
        complexities=["low", "high"],
        preset_count=3,
        repeats=1,
        measure_seconds=2.5,
        budget_minutes=25,
        concurrency_max=None,
    ),
    "deep": Profile(
        name="deep",
        resolutions=["360p", "480p", "720p", "1080p", "1440p", "2160p"],
        fps_list=[24, 30, 60, 120],
        bitrates=["low", "target", "high"],
        complexities=["low", "high"],
        preset_count=5,
        repeats=3,
        measure_seconds=4.0,
        budget_minutes=None,
        concurrency_max=None,
        full_cross=True,
        quality=True,
    ),
}


class PlannedCase(object):
    """A TestCase plus scheduling metadata."""

    def __init__(self, case, tier, axis):
        self.case = case
        self.tier = tier
        self.axis = axis

    def __repr__(self):
        return "PlannedCase(%s, tier=%d, axis=%s)" % (
            self.case.label(), self.tier, self.axis)


def baseline_resolution(resolutions):
    """Prefer 1080p as the anchor; it is what most people actually encode."""
    for preferred in ("1080p", "720p", "1440p", "480p", "2160p", "360p"):
        if preferred in resolutions:
            return preferred
    return resolutions[0]


def baseline_fps(fps_list):
    return 30 if 30 in fps_list else fps_list[0]


def baseline_complexity(complexities):
    return "high" if "high" in complexities else complexities[0]


def presets_for(spec, count):
    """Sample the encoder's speed knob from fastest to slowest."""
    values = spec.speed_values
    if not spec.speed_knob or not values:
        return [None]
    if count >= len(values):
        return list(values)
    if count <= 1:
        return [values[len(values) // 2]]
    if count == 2:
        return [values[0], values[-1]]
    stride = (len(values) - 1) / float(count - 1)
    picked, seen = [], set()
    for i in range(count):
        v = values[int(round(i * stride))]
        if v not in seen:
            seen.add(v)
            picked.append(v)
    return picked


def balanced_preset(spec, count=None):
    """The encoder's own middle setting -- e.g. x264 'medium'.

    Deliberately independent of how many presets are being sampled: with a
    2-preset sample the 'middle' of the sample is the slowest one, which would
    make the baseline (and every speed estimate derived from it) unrepresentative.
    """
    values = spec.speed_values
    if not spec.speed_knob or not values:
        return None
    if not getattr(spec, "knob_is_ladder", True):
        # Driver-defined knob: the default is what this machine actually gives
        # you, so anchor there and let the sweep show what tuning changes.
        return None
    return values[len(values) // 2]


def bitrate_for(res_key, name):
    ladder = BITRATE_LADDER.get(res_key, BITRATE_LADDER["1080p"])
    return ladder[BITRATE_NAMES.index(name)]


def quality_levels_for(codec):
    return QUALITY_LEVELS.get(codec, DEFAULT_QUALITY)


def build_plan(specs, profile, resolutions=None, fps_list=None,
               complexities=None, bitrates=None):
    """Axis sweeps around a baseline, deduplicated, tier-ordered."""
    resolutions = resolutions or profile.resolutions
    fps_list = fps_list or profile.fps_list
    complexities = complexities or profile.complexities
    bitrates = bitrates or profile.bitrates

    base_res = baseline_resolution(resolutions)
    base_fps = baseline_fps(fps_list)
    base_cx = baseline_complexity(complexities)
    base_rate_name = "target" if "target" in bitrates else bitrates[0]

    planned = []
    seen = set()

    def add(spec, res_key, fps, complexity, rate_name, preset, tier, axis):
        width, height = sources.RES_BY_KEY[res_key]
        case = TestCase(
            encoder=spec, res_key=res_key, width=width, height=height,
            fps=fps, complexity=complexity, frames=0,
            bitrate=bitrate_for(res_key, rate_name), preset=preset,
            rate_mode="bitrate",
        )
        if case.key in seen:
            return
        seen.add(case.key)
        planned.append(PlannedCase(case, tier, axis))

    for spec in specs:
        base_preset = balanced_preset(spec, profile.preset_count)
        preset_choices = presets_for(spec, profile.preset_count)

        # Tier 0: the anchor every other result is compared against.
        add(spec, base_res, base_fps, base_cx, base_rate_name, base_preset,
            TIER_BASELINE, "baseline")

        # Tier 1: how throughput scales with pixel count.
        for res_key in resolutions:
            add(spec, res_key, base_fps, base_cx, base_rate_name, base_preset,
                TIER_RESOLUTION, "resolution")

        # Tier 2: the encoder's own speed/efficiency tradeoff.
        for preset in preset_choices:
            add(spec, base_res, base_fps, base_cx, base_rate_name, preset,
                TIER_PRESET, "preset")

        # Tier 3: does bitrate move throughput at all?
        for rate_name in bitrates:
            add(spec, base_res, base_fps, base_cx, rate_name, base_preset,
                TIER_BITRATE, "bitrate")

        # Tier 4: framerate affects rate control and GOP structure.
        for fps in fps_list:
            add(spec, base_res, fps, base_cx, base_rate_name, base_preset,
                TIER_FPS, "fps")

        # Tier 5: easy vs hard content.
        for complexity in complexities:
            add(spec, base_res, base_fps, complexity, base_rate_name, base_preset,
                TIER_COMPLEXITY, "complexity")

        # deep: fill in the full resolution x preset grid as well.
        if profile.full_cross:
            for res_key in resolutions:
                for preset in preset_choices:
                    for fps in fps_list:
                        add(spec, res_key, fps, base_cx, base_rate_name, preset,
                            TIER_COMPLEXITY + 1, "cross")

    # Interleave across encoders inside each tier so a truncated run still
    # covers every encoder.
    order = {spec.name: i for i, spec in enumerate(specs)}
    planned.sort(key=lambda pc: (pc.tier, order.get(pc.case.encoder.name, 99),
                                 sources.RES_ORDER.index(pc.case.res_key)))
    return planned


def concurrency_case(spec, profile, resolutions, fps_list, complexities):
    """The fixed reference point used for the parallel-streams ramp."""
    res_key = baseline_resolution(resolutions)
    fps = baseline_fps(fps_list)
    complexity = baseline_complexity(complexities)
    width, height = sources.RES_BY_KEY[res_key]
    return TestCase(
        encoder=spec, res_key=res_key, width=width, height=height,
        fps=fps, complexity=complexity, frames=0,
        bitrate=bitrate_for(res_key, "target"),
        preset=balanced_preset(spec, profile.preset_count),
        rate_mode="bitrate", kind="concurrency",
    )


def quality_plan(specs, profile, resolutions, fps_list):
    """A reduced constant-quality set for PSNR/SSIM/VMAF comparison."""
    res_key = baseline_resolution(resolutions)
    fps = baseline_fps(fps_list)
    width, height = sources.RES_BY_KEY[res_key]
    cases = []
    for spec in specs:
        preset = balanced_preset(spec, profile.preset_count)
        for rate_name in ("low", "target", "high"):
            cases.append(TestCase(
                encoder=spec, res_key=res_key, width=width, height=height,
                fps=fps, complexity="high", frames=0,
                bitrate=bitrate_for(res_key, rate_name), preset=preset,
                rate_mode="bitrate", kind="quality",
            ))
    return cases
