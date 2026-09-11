"""Orchestration: calibrate, size, schedule, execute, record.

Two things here keep a benchmark honest and finite:

* **Adaptive sizing.** Every measurement is calibrated with a few frames first,
  then sized so the timed run lasts roughly the same wall time regardless of
  whether the encoder does 20 fps or 2000. Fixed frame counts would make fast
  encoders unmeasurably brief and slow ones unbounded.

* **Explicit skips.** When an encoder is too slow to measure at a resolution
  inside the per-test cap, it is skipped with a recorded reason and higher
  resolutions are dropped for that encoder. Nothing silently disappears.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time

from . import matrix, probe, runner, sources, util
from .util import detail, human_time, info, step, warn

# Frames used to estimate an encoder's speed before the real run.
CALIB_FRAMES = {"360p": 24, "480p": 24, "720p": 24,
                "1080p": 16, "1440p": 12, "2160p": 8}
CALIB_TIMEOUT = 90.0

# Bounds on the timed run.
MAX_FRAMES = {"360p": 900, "480p": 900, "720p": 900,
              "1080p": 600, "1440p": 400, "2160p": 250}

# Per-test wall-clock cap by profile; anything slower is skipped, not endured.
TEST_CAP = {"quick": 20.0, "standard": 30.0, "deep": 180.0}

# Share of the time budget reserved for later phases. Only consulted when a
# budget exists at all -- the standard profile has none, so these apply to
# `quick` and to an explicit --time-budget.
CONCURRENCY_SHARE = 0.25
LATENCY_SHARE = 0.15
QUALITY_SHARE = 0.20

# -- cost model --------------------------------------------------------------
#
# With no wall on the default profile, a projection is the only thing standing
# between the user and an open-ended run, so it has to describe the run that
# will actually happen.

# Wall cost of one timed run beyond ffmpeg's own rtime: spawn, teardown,
# progress drain. Measured across 99 results on an Arc/Meteor Lake box, median
# 0.21s, mean 0.21s, max 0.47s.
PER_RUN_OVERHEAD = 0.25

# Throughput falls roughly as pixels ** -PIXEL_EXPONENT, used to carry one
# calibration estimate to a resolution that has not been calibrated yet. Fitted
# over 64 points (6 encoders x 6 resolutions, one box): median exponent 0.76,
# p10 0.48, p90 1.01. Wide enough that it is only ever good for sizing an ETA --
# never for a yes/no decision, the same split invariant 3 draws for calibration.
PIXEL_EXPONENT = 0.75

# Below this fraction of realtime at the baseline operating point, an encoder is
# useful for batch work and nothing else, and gets light testing: the full
# resolution ladder at the baseline preset, and none of the sweeps. Those all
# ask "does this knob move throughput" -- a question with no audience for an
# encoder running at 1.1 fps, and six capped tests apiece to answer. Resolution
# scaling stays, because how it scales is the interesting thing about it.
LIGHT_FRACTION = 0.25
LIGHT_TIERS = (matrix.TIER_PRESET, matrix.TIER_BITRATE,
               matrix.TIER_FPS, matrix.TIER_COMPLEXITY)
# The same scope in the latency pass, or the two disagree: light testing drops
# the throughput preset sweep, so a light encoder has no measurement *and no
# calibration* for any preset but the baseline -- and the latency preset sweep
# then cannot be priced and runs until it times out.
LIGHT_LATENCY_TIERS = (matrix.LATENCY_TIER_PRESET, matrix.LATENCY_TIER_FPS)

# Low-latency arguments change throughput, sometimes by a lot (-deadline
# realtime, -usage realtime), so a default-mode measurement understates what the
# low-latency run would do. Credit it this margin before refusing on its behalf.
LATENCY_LOWLAT_MARGIN = 4.0

_UNSET = object()


class Skip(object):
    def __init__(self, encoder, res_key, reason):
        self.encoder = encoder
        self.res_key = res_key
        self.reason = reason

    def as_dict(self):
        return {"encoder": self.encoder, "resolution": self.res_key,
                "reason": self.reason}


class Orchestrator(object):
    def __init__(self, ff, library, specs, profile, args, run_id, results_dir,
                 anchor_specs=None):
        self.ff = ff
        self.lib = library
        self.specs = specs
        # Every usable encoder on the box, not just the selected ones: the
        # latency self-check needs a known-answer anchor encoder even when the
        # user asked for --encoders hevc_vaapi.
        self.anchor_specs = anchor_specs or specs
        self.profile = profile
        self.args = args
        self.run_id = run_id
        self.results_dir = results_dir

        self.results = []
        self.ramps = {}
        self.quality_results = []
        self.latency_results = []
        self.skips = []

        # Coverage bookkeeping: what the plan asked for against what ran, so an
        # incomplete table can say why instead of quietly disappearing.
        self.axis_planned = {}    # axis name -> planned case count
        self.axis_ran = {}        # axis name -> completed case count
        self.latency_planned = 0
        self.latency_ran = 0
        # Why the run stopped short. `stopped_early` is the first phase to run
        # out -- everything after it was squeezed by the same shortfall, so a
        # later phase overwriting it would blame the wrong thing.
        self.stopped_early = None
        self.stopped_matrix = None   # the matrix phase's own reason, if any
        self.light = []           # batch-only encoders, lightly tested, with why
        self.latency_gated = []   # latency points not attempted, with the rate

        self._calib = {}          # (encoder, res) -> fps estimate or None
        # (encoder, res, preset) -> (frames, rtime) of the calibration run.
        # Kept because it is the short-run end of the two points that give
        # startup cost for free; see latency.startup_costs.
        self._calib_runs = {}
        self._max_res = {}        # encoder -> highest viable resolution index
        self._light = {}          # encoder -> bool, decided once per encoder
        self._done_keys = set()
        self._durations = []
        self._pending = []        # matrix cases not yet run, for the live ETA
        self._projected = None    # one-shot projection, printed after tier 0
        self._start = None
        self._budget = None
        self._jsonl = os.path.join(results_dir, "%s.jsonl" % run_id)
        self._jsonl_fh = None
        self._total = 0
        self._completed = 0
        self._last_line = ""

    # -- budget ------------------------------------------------------------

    def _stop(self, reason):
        """Record why a phase stopped. The first reason wins."""
        if self.stopped_early is None:
            self.stopped_early = reason
        return reason

    def _elapsed(self):
        return time.monotonic() - self._start

    def _budget_left(self, reserve=0.0):
        if self._budget is None:
            return float("inf")
        return self._budget * (1.0 - reserve) - self._elapsed()

    def _out_of_time(self, reserve=0.0):
        return self._budget_left(reserve) <= 0

    # -- resume ------------------------------------------------------------

    def load_resume(self, path):
        """Reload a previous run's results so the report is complete.

        Skipping already-finished work is only half of resume: the earlier
        measurements have to come back into the report too, otherwise resuming a
        finished run produces an empty one.
        """
        count = 0
        by_name = {spec.name: spec for spec in self.specs}
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    kind = rec.get("record")
                    data = rec.get("data") or {}
                    if kind == "result":
                        self._done_keys.add(_key_of(data))
                        restored = _restore_result(data, by_name)
                        if restored is not None:
                            self.results.append(restored)
                        count += 1
                    elif kind == "quality":
                        self.quality_results.append(data)
                    elif kind == "latency":
                        self.latency_results.append(data)
                    elif kind == "calibration":
                        self._calib_runs[(data.get("encoder"),
                                          data.get("resolution"),
                                          data.get("preset"))] = (
                            data.get("frames"), data.get("rtime"))
                    elif kind == "ramp":
                        spec = by_name.get(data.get("encoder"))
                        if spec is not None:
                            self.ramps[data["encoder"]] = {
                                "case": _restore_case(data.get("case", {}), spec),
                                "steps": [_restore_step(x) for x in data.get("steps", [])],
                                "max_level": data.get("max_level"),
                            }
                    elif kind == "skip":
                        self.skips.append(Skip(data.get("encoder"),
                                               data.get("resolution"),
                                               data.get("reason", "")))
        except OSError as e:
            raise RuntimeError("cannot read resume file %s: %s" % (path, e))
        self._jsonl = path
        return count

    def _record(self, kind, data):
        if self._jsonl_fh is None:
            self._jsonl_fh = open(self._jsonl, "a")
        self._jsonl_fh.write(json.dumps({"record": kind, "data": data},
                                        default=str) + "\n")
        self._jsonl_fh.flush()

    def close(self):
        if self._jsonl_fh:
            try:
                self._jsonl_fh.close()
            except OSError:
                pass
            self._jsonl_fh = None

    # -- progress ----------------------------------------------------------

    def _eta(self):
        """Projected time for the cases still to run.

        Deliberately not a mean of recent durations: the plan is executed tier
        by tier and per-test cost changes by an order of magnitude across a tier
        boundary, so an average over the last N tests is worst exactly where a
        user is looking at it.
        """
        if self._completed >= self._total:
            return None
        charged = set(self._calib)
        remaining = sum(self._projected_seconds(pc.case, charged)
                        for pc in self._pending if not self._is_light(pc))
        return remaining or None

    def _is_light(self, pc):
        """Already decided that this case will not run. Reads the cache only --
        deciding is _light_mode's job, and it needs a measurement first."""
        return (pc.tier in LIGHT_TIERS
                and self._light.get(pc.case.encoder.name, False))

    def _progress(self, label, suffix=""):
        if util.VERBOSITY < 1:
            return
        eta = self._eta()
        head = "[%d/%d]" % (self._completed, self._total)
        line = "  %s %s" % (util.grey(head), label)
        tail = []
        if eta:
            tail.append("eta %s" % human_time(eta))
        if suffix:
            tail.append(suffix)
        if tail:
            line += "  " + util.grey("(" + ", ".join(tail) + ")")
        if sys.stderr.isatty():
            pad = max(0, util.visible_len(self._last_line) - util.visible_len(line))
            sys.stderr.write("\r" + line + " " * pad)
            sys.stderr.flush()
            self._last_line = line
        else:
            sys.stderr.write(line + "\n")

    def _progress_done(self, text):
        if util.VERBOSITY < 1:
            return
        if sys.stderr.isatty():
            pad = max(0, util.visible_len(self._last_line) - util.visible_len(text) - 2)
            sys.stderr.write("\r  " + text + " " * pad + "\n")
            self._last_line = ""
        else:
            sys.stderr.write("  " + text + "\n")
        sys.stderr.flush()

    # -- calibration and sizing -------------------------------------------

    def _res_index(self, res_key):
        return sources.RES_ORDER.index(res_key)

    def _viable(self, spec, res_key):
        limit = self._max_res.get(spec.name)
        return limit is None or self._res_index(res_key) <= limit

    def _skip(self, encoder_name, res_key, reason):
        """Record a skip so it survives --resume as well as the live report."""
        entry = Skip(encoder_name, res_key, reason)
        self.skips.append(entry)
        self._record("skip", entry.as_dict())
        return entry

    def _mark_unviable(self, spec, res_key, reason):
        idx = self._res_index(res_key)
        current = self._max_res.get(spec.name)
        new_limit = idx - 1
        if current is None or new_limit < current:
            self._max_res[spec.name] = new_limit
        self._skip(spec.name, res_key, reason)

    def _calibrate(self, spec, res_key, fps, complexity, preset):
        """Rough fps estimate so the timed run can be sized sensibly.

        Keyed on the preset as well as the encoder and resolution: x264
        ultrafast is roughly eighty times faster than veryslow, so one estimate
        per encoder would size fast runs far too short to be meaningful.
        """
        key = (spec.name, res_key, preset)
        if key in self._calib:
            return self._calib[key]

        clip = self.lib.get(res_key, complexity)
        frames = CALIB_FRAMES.get(res_key, 16)
        case = runner.TestCase(
            encoder=spec, res_key=res_key, width=clip.width, height=clip.height,
            fps=fps, complexity=complexity, frames=frames,
            bitrate=matrix.bitrate_for(res_key, "target"),
            preset=preset,
            rate_mode="bitrate", kind="calibration",
        )
        result = runner.run_single(self.ff, case, clip,
                                   timeout=CALIB_TIMEOUT, sample=False)
        estimate = result.encode_fps if result.ok else None
        self._calib[key] = estimate
        # The short end of the pair that yields startup cost. Recorded as well
        # as remembered, or a --resume that skips the matrix loses it.
        if result.ok and result.rtime and result.frames:
            self._calib_runs[key] = (result.frames, result.rtime)
            self._record("calibration", {
                "encoder": spec.name, "resolution": res_key, "preset": preset,
                "frames": result.frames, "rtime": round(result.rtime, 4)})
        if estimate is None:
            reason = result.error or "calibration failed"
            self._mark_unviable(spec, res_key, reason)
        else:
            detail("calibrated %s @%s (%s): ~%.0f fps"
                   % (spec.name, res_key, preset, estimate))
        return estimate

    def _measured_fps(self, spec, res_key, fps, complexity, preset=_UNSET):
        """Best real measurement we already have for this operating point.

        Calibration runs only a handful of frames, so fixed startup cost
        (hardware device init, filter graph setup) drags its estimate well below
        sustained throughput -- measured on a Gemini Lake iGPU, 16 frames reads
        75 fps where 300 frames sustains 154. That bias is tolerable for sizing a
        run, which has floors, but not for a yes/no decision like "is this
        encoder even worth ramping". Prefer a real measurement wherever we have one.

        `preset` narrows to one setting of the speed knob. Left out, the best
        result over every preset wins, which is what the concurrency ramp wants
        ("can this encoder do realtime at all"). A question about the *baseline*
        operating point has to pass the baseline preset, or the preset sweep's
        fastest row answers it -- x264 ultrafast is ~80x veryslow.
        """
        best = None
        for result in self.results:
            case = result.case
            if (case.encoder.name == spec.name and case.res_key == res_key
                    and case.fps == fps and case.complexity == complexity
                    and (preset is _UNSET or case.preset == preset)
                    and result.encode_fps):
                best = result.encode_fps if best is None else max(best, result.encode_fps)
        return best

    def _size_frames(self, case, estimate):
        """Choose a frame count giving a consistent measurement duration."""
        cap = TEST_CAP.get(self.profile.name, 90.0)
        target = self.profile.measure_seconds
        ceiling = MAX_FRAMES.get(case.res_key, 600)

        if self.args.frames:
            return max(1, self.args.frames), None

        # GOP length is pinned at 2*fps, so two full GOPs is the shortest run
        # where rate control has actually converged and the opening I-frame is
        # not a dominant share of the bytes.
        gop = max(1, int(case.fps) * 2)
        preferred_floor = gop * 2
        hard_floor = max(24, int(case.fps))

        frames = int(estimate * target)
        frames = max(preferred_floor, min(frames, ceiling))

        # Do not exceed the per-test wall-clock cap.
        if frames / estimate > cap:
            frames = int(estimate * cap)
            if frames < hard_floor:
                return None, ("too slow to measure here (~%.1f fps, would need %s)"
                              % (estimate, human_time(hard_floor / estimate)))
        return max(frames, 1), None

    # -- cost projection ---------------------------------------------------

    def _estimate_fps(self, case):
        """Best guess at this encoder's rate here, for sizing only.

        The same number run_matrix will feed to _size_frames: a real measurement
        of this operating point where one exists, otherwise this exact
        calibration point, otherwise a calibration at another resolution carried
        across by pixel count. Never gate a decision on the result (invariant 3)
        -- the calibration term is biased low and the pixel term is a fit, so
        the two compose into a number good for an ETA and nothing else.
        """
        spec, res_key, preset = case.encoder, case.res_key, case.preset
        exact = self._calib.get((spec.name, res_key, preset))
        measured = self._measured_fps(spec, res_key, case.fps, case.complexity,
                                      preset=preset)
        if exact and measured:
            return max(exact, measured)
        if exact or measured:
            return exact or measured
        pixels = _pixels(res_key)
        # Same preset first: the knob moves throughput far more than resolution
        # does, so a same-preset point at another resolution beats an exact-
        # resolution point at another preset.
        for want_preset in (True, False):
            best = None
            for (name, other_res, other_preset), value in self._calib.items():
                if name != spec.name or not value:
                    continue
                if want_preset and other_preset != preset:
                    continue
                other_pixels = _pixels(other_res)
                scaled = value * (other_pixels / float(pixels)) ** PIXEL_EXPONENT
                # Closest in pixel count is the least extrapolated.
                distance = abs(_res_gap(other_res, res_key))
                if best is None or distance < best[0]:
                    best = (distance, scaled)
            if best:
                return best[1]
        return None

    def _projected_seconds(self, case, charged=None):
        """Wall cost of one planned case, in seconds.

        Runs the estimate through the same _size_frames the real run will use.
        A projection built from its own sizing rule describes a run that will
        not happen.

        `charged` is the set of calibration keys already paid for. Every new
        (encoder, resolution, preset) costs a calibration run before its timed
        run, and the resolution and preset sweeps are nothing but new keys;
        pass the set to have that counted once each rather than not at all.
        """
        estimate = self._estimate_fps(case)
        if not estimate:
            return 0.0
        frames, _reason = self._size_frames(case, estimate)
        if frames is None:
            return 0.0               # will be skipped, so it costs nothing
        repeats = max(1, self.args.repeats or self.profile.repeats)
        total = (frames / estimate + PER_RUN_OVERHEAD) * repeats
        if charged is not None:
            key = (case.encoder.name, case.res_key, case.preset)
            if key not in charged:
                charged.add(key)
                total += (CALIB_FRAMES.get(case.res_key, 16) / estimate
                          + PER_RUN_OVERHEAD)
        return total

    def _projected_concurrency(self, specs, resolutions, fps_list, complexities):
        """Cost of the ramps still to run."""
        total = 0.0
        for spec in specs:
            if spec.name in self.ramps:
                continue
            case = matrix.concurrency_case(spec, self.profile, resolutions,
                                           fps_list, complexities)
            if not self._viable(spec, case.res_key):
                continue
            estimate = self._estimate_fps(case)
            if not estimate or estimate < case.fps:
                continue             # ramp is skipped, or stops after level 1
            frames, _reason = self._size_frames(case, estimate)
            if frames is None:
                continue
            frames = min(frames, 300)
            for level in runner.CONCURRENCY_LEVELS:
                per_stream = estimate / float(level)
                total += frames / max(float(case.fps), per_stream) + PER_RUN_OVERHEAD
                if per_stream < case.fps:
                    break            # the ramp runs the level that fails, then stops
        return total

    def _projected_latency(self, planned):
        """Cost of the latency cases that survive the realtime gate.

        Gated here on the sizing estimate rather than on _measured_fps, because
        at projection time only the baseline point has been measured and every
        other resolution would look ungated and be costed at full price. This is
        a projection, not the gate -- _latency_gated still decides for real, on
        evidence, when the pass runs.
        """
        from . import latency
        total = 0.0
        for lc in planned:
            case = lc.case
            if not self._viable(case.encoder, case.res_key):
                continue
            if (lc.tier in LIGHT_LATENCY_TIERS
                    and self._light.get(case.encoder.name, False)):
                continue
            estimate = self._estimate_fps(case)
            if not estimate:
                continue
            rate = estimate * (LATENCY_LOWLAT_MARGIN if lc.mode == "lowlat" else 1.0)
            pace = latency.pace_for(rate, case.fps)
            frames = max(90, int(round(case.fps * self.profile.latency_seconds)))
            if frames / pace > latency.MAX_PACED_SECONDS:
                continue
            total += self._latency_pair_seconds(case, estimate)
            if lc.mode == "lowlat":
                # A short validation encode, the first time this encoder's
                # low-latency arguments are tried.
                total += 2.0
        if total <= 0:
            return total
        # The self-check is two more paired measurements, run before any row is
        # produced (invariant 34). It always costs, and leaving it out made the
        # projection read a third short on a run whose latency pass dominated.
        anchor = None
        for name in latency.ANCHOR_ENCODERS:
            anchor = next((s for s in self.anchor_specs
                           if s.name == name and s.ok), None)
            if anchor:
                break
        if anchor is not None:
            probe = planned[0].case
            for preset in ("veryfast", "medium"):
                case = runner.TestCase(
                    encoder=anchor, res_key=probe.res_key, width=probe.width,
                    height=probe.height, fps=probe.fps,
                    complexity=probe.complexity, frames=0,
                    bitrate=probe.bitrate, preset=preset, rate_mode="bitrate",
                    kind="latency-selfcheck")
                estimate = self._estimate_fps(case)
                if estimate:
                    total += self._latency_pair_seconds(case, estimate)
        return total

    def _latency_pair_seconds(self, case, estimate):
        """One paced/unpaced measurement pair: the unit the latency pass buys."""
        from . import latency
        frames = max(90, int(round(case.fps * self.profile.latency_seconds)))
        # The paced run costs frames / the rate it is paced at, which for a slow
        # encoder is below the content rate and so costs more than realtime.
        pace = latency.pace_for(estimate, case.fps)
        return (frames / estimate + frames / pace + 2 * PER_RUN_OVERHEAD)

    def _announce_projection(self, specs, resolutions, fps_list, complexities,
                             latency_planned, base_res, base_fps, base_cx):
        """One projection, made once every encoder has a real measurement.

        Called at the tier 0 -> 1 boundary, which is the first point where every
        encoder has been measured rather than merely calibrated. Demotion is
        settled here too, so the figure describes the run that will happen and
        the skips are on record before the work they replace would have started.
        """
        if self._projected is not None:
            return
        for spec in specs:
            self._light_mode(spec, base_res, base_fps, base_cx)
        will_run = [pc for pc in self._pending if not self._is_light(pc)]
        charged = set(self._calib)
        parts = [("tests", sum(self._projected_seconds(pc.case, charged)
                               for pc in will_run))]
        if not self.args.no_concurrency:
            parts.append(("ramps", self._projected_concurrency(
                specs, resolutions, fps_list, complexities)))
        if not self.args.no_latency:
            parts.append(("latency", self._projected_latency(
                latency_planned or [])))
        total = sum(value for _name, value in parts)
        self._projected = total
        if total <= 0 or util.VERBOSITY < 1:
            return
        # Broken down by phase, because a phase can still decline to run after
        # this point -- the latency pass is gated on a self-check that happens
        # later -- and a single number then reads long with nothing to say why.
        detail_parts = ", ".join("%s %s" % (name, human_time(value))
                                 for name, value in parts if value > 0)
        step("Projected %s for %d remaining tests across %d encoders%s"
             % (human_time(total), len(will_run), len(specs),
                "" if self._budget else
                " - Ctrl-C stops and still reports what finished"))
        if len(parts) > 1:
            # Shown at normal verbosity: with no wall on the default profile
            # this is the number a user plans around.
            info("  %s" % util.grey("of which %s" % detail_parts))

    # -- scope gates -------------------------------------------------------

    def _light_mode(self, spec, base_res, base_fps, base_cx):
        """Is this encoder batch-only, and so due light testing?

        Decided once per encoder, on a real measurement of the *baseline*
        operating point and never on calibration (invariant 3), and only on
        evidence: with no measurement the encoder keeps every axis, the same
        rule run_concurrency states about its own ramp.

        The measurement must be at the baseline preset. A light encoder keeps
        the resolution ladder at that preset and nothing else, and
        report._baseline_preset filters the resolution table on it (invariants
        17/18) -- measured at any other preset it would fall out of the one
        table it is still in.
        """
        if spec.name in self._light:
            return self._light[spec.name]
        base_preset = matrix.balanced_preset(spec, self.profile.preset_count)
        measured = self._measured_fps(spec, base_res, base_fps, base_cx,
                                      preset=base_preset)
        light = (measured is not None
                 and measured < LIGHT_FRACTION * base_fps)
        self._light[spec.name] = light
        if light:
            reason = ("below %.2gx realtime at %s%d (%.1f fps) - light testing: "
                      "resolution scaling only, no preset, bitrate, framerate "
                      "or complexity sweep"
                      % (LIGHT_FRACTION, base_res, base_fps, measured))
            self.light.append({"encoder": spec.name, "measured_fps": measured,
                               "reason": reason})
            # --resume restores the earlier skip and re-derives the same
            # decision from the restored results; recording it again would list
            # the encoder twice.
            already = any(sk.encoder == spec.name and sk.reason == reason
                          for sk in self.skips)
            if not already:
                self._skip(spec.name, base_res, reason)
        return light

    def _slowest_measured(self, case):
        """Lowest real rate seen for this encoder at this operating point.

        A lower bound is what a cost estimate wants: it errs towards skipping a
        measurement rather than towards paying for one that will be refused.
        """
        rates = [r.encode_fps for r in self.results
                 if r.case.encoder.name == case.encoder.name
                 and r.case.res_key == case.res_key
                 and r.case.fps == case.fps
                 and r.case.complexity == case.complexity
                 and r.encode_fps]
        return min(rates) if rates else None

    def _latency_gated(self, lc):
        """(reason, rate) when this case is not worth measuring, else None.

        The pacer adapts to the encoder (`latency.pace_for`), so the question is
        no longer "can this be measured" but "what does measuring it cost". A
        paced run costs its wall time, and pacing below the content rate costs
        proportionally more; past `MAX_PACED_SECONDS` it is not worth it.
        latency.measure applies the same cap, but only after paying for a full
        free-running encode -- ~110s per case for libaom-av1 on one run.

        The rate is returned rather than looked up again by the caller: the
        decision is made on the measurement for *this preset*, and the best rate
        over all presets is a different and much larger number. Recording that
        one produced the line "libx264 below 2x realtime (827.6 fps)".

        Gated on a real measurement, never calibration (invariant 3), and only
        on evidence: with nothing measured, run it and let latency.measure judge.
        """
        from . import latency
        case = lc.case
        # This exact preset if it has been measured -- the latency preset sweep
        # spans the encoder's whole knob, and x265 veryslow is nowhere near
        # x265 ultrafast.
        measured = self._measured_fps(case.encoder, case.res_key, case.fps,
                                      case.complexity, preset=case.preset)
        if measured is None:
            # Otherwise this preset's calibration, then the slowest preset
            # measured here -- in that order, because both are lower bounds and
            # a lower bound is what a *cost* estimate wants. Invariant 3 forbids
            # gating on calibration because it is biased low and would wrongly
            # call an encoder incapable; here the bias errs towards skipping an
            # expensive measurement rather than towards paying for one that will
            # be refused anyway, and the question is budget, not capability.
            # libx265 veryslow at 1080p on four cores timed out proving it was
            # too slow to pace, gated in on its own ultrafast figure.
            measured = self._calib.get(
                (case.encoder.name, case.res_key, case.preset))
        if measured is None:
            measured = self._slowest_measured(case)
        if measured is None:
            return None
        rate = measured
        if lc.mode == "lowlat":
            # Low-latency arguments can make an encoder several times faster, so
            # the default-mode figure is not what the paced run will see.
            rate *= LATENCY_LOWLAT_MARGIN
        frames = max(90, int(round(case.fps * self.profile.latency_seconds)))
        if frames / latency.pace_for(rate, case.fps) <= latency.MAX_PACED_SECONDS:
            return None

        # Deliberately the same string for every encoder gated the same way:
        # report.render_warnings groups skips by reason, so a message carrying
        # this encoder's own fps would split one note into a dozen and crowd
        # the block out. The rates live in coverage()["latency_gated"].
        return ("too slow to pace: measuring it would need more than %s of "
                "realtime-paced input per point"
                % human_time(latency.MAX_PACED_SECONDS)), measured

    # -- single-stream phase ----------------------------------------------

    def run_matrix(self, planned, specs=None, resolutions=None, fps_list=None,
                   complexities=None, latency_planned=None):
        self._total = len(planned)
        self._completed = 0
        self._pending = list(planned)
        for pc in planned:
            self.axis_planned[pc.axis] = self.axis_planned.get(pc.axis, 0) + 1

        resolutions = resolutions or self.profile.resolutions
        fps_list = fps_list or self.profile.fps_list
        complexities = complexities or self.profile.complexities
        base_res = matrix.baseline_resolution(resolutions)
        base_fps = matrix.baseline_fps(fps_list)
        base_cx = matrix.baseline_complexity(complexities)
        last_tier = None

        for pc in self._in_plan_order(planned):
            if runner.aborted():
                self.stopped_matrix = self._stop("interrupted")
                break
            if self._out_of_time(reserve=self._reserve()):
                remaining = self._total - self._completed
                if remaining > 0:
                    self.stopped_matrix = self._stop(
                        "time budget reached with %d of %d tests still to run"
                        % (remaining, self._total))
                    warn("time budget reached; %d planned tests not run" % remaining)
                    self._record("budget_stop", {"remaining": remaining})
                break

            case = pc.case
            spec = case.encoder

            # Tier 0 calibrates and measures every encoder, so the boundary out
            # of it is the first moment the rest of the run can be costed.
            if (last_tier == matrix.TIER_BASELINE
                    and pc.tier != matrix.TIER_BASELINE):
                self._announce_projection(specs or self.specs, resolutions,
                                          fps_list, complexities,
                                          latency_planned, base_res, base_fps,
                                          base_cx)
            last_tier = pc.tier

            self._pending.remove(pc)

            if pc.tier in LIGHT_TIERS and self._light_mode(
                    spec, base_res, base_fps, base_cx):
                self._completed += 1
                continue
            if not self._viable(spec, case.res_key):
                self._completed += 1
                continue
            if _key_of(case.as_dict()) in self._done_keys:
                self.axis_ran[pc.axis] = self.axis_ran.get(pc.axis, 0) + 1
                self._completed += 1
                continue

            estimate = self._calibrate(spec, case.res_key, case.fps,
                                       case.complexity, case.preset)
            if estimate is None:
                self._completed += 1
                continue
            # Same preset, not the encoder's best. This line exists to correct
            # calibration's low bias with a real measurement, and a measurement
            # at another preset is not a measurement of this operating point:
            # the preset sweep runs at the baseline resolution, so the fastest
            # preset's row was sizing the slowest preset's run. Seen on
            # libaom-av1, whose cpu-used 8 result (4 fps) sized a cpu-used 2 run
            # of 120 frames that then ran at 0.8 fps -- 150s against a 30s cap.
            measured = self._measured_fps(spec, case.res_key, case.fps,
                                          case.complexity, preset=case.preset)
            if measured and measured > estimate:
                estimate = measured

            frames, reason = self._size_frames(case, estimate)
            if frames is None:
                self._mark_unviable(spec, case.res_key, reason)
                self._completed += 1
                continue
            case.frames = frames

            clip = self.lib.get(case.res_key, case.complexity)
            self._progress(case.label())

            started = time.monotonic()
            result = self._run_repeats(case, clip)
            self._durations.append(time.monotonic() - started)
            self._completed += 1

            if result.ok:
                self.results.append(result)
                self._record("result", result.as_dict())
                self.axis_ran[pc.axis] = self.axis_ran.get(pc.axis, 0) + 1
                self._progress(case.label(), "%.0f fps" % result.encode_fps)
            else:
                self._record("failure", result.as_dict())
                if "timed out" in (result.error or ""):
                    self._mark_unviable(spec, case.res_key, "timed out")
                else:
                    self._skip(spec.name, case.res_key,
                               result.error or "failed")

            if self.args.cooldown:
                time.sleep(self.args.cooldown)

        if sys.stderr.isatty() and self._last_line:
            sys.stderr.write("\r" + " " * util.visible_len(self._last_line) + "\r")
            sys.stderr.flush()
            self._last_line = ""

    def _in_plan_order(self, planned):
        """Yield tier by tier, in the order the plan already carries.

        matrix.build_plan sorts on (tier, encoder rank, resolution) and the
        encoder rank comes from the spec list, which probe.popularity has
        already ordered. Tiers above 0 used to be re-sorted here by measured
        speed, to get the most measurements out of an expiring budget; no
        profile has a budget now, so the question is what an *interrupted* run
        holds, and that is answered by popularity rather than by speed. Two
        ordering rules that disagreed became one.
        """
        tiers = {}
        for pc in planned:
            tiers.setdefault(pc.tier, []).append(pc)
        for tier in sorted(tiers):
            for pc in tiers[tier]:
                yield pc

    def _reserve(self):
        reserve = 0.0
        if not self.args.no_concurrency:
            reserve += CONCURRENCY_SHARE
        if not self.args.no_latency:
            reserve += LATENCY_SHARE
        if self.args.quality:
            reserve += QUALITY_SHARE
        return min(reserve, 0.7)

    def _latency_reserve(self):
        """What the phases after the concurrency ramp still need."""
        reserve = 0.0
        if not self.args.no_latency:
            reserve += LATENCY_SHARE
        if self.args.quality:
            reserve += QUALITY_SHARE
        return min(reserve, 0.5)

    def _run_repeats(self, case, clip):
        """Median of N runs; a single timing is noise, not a measurement."""
        repeats = self.args.repeats or self.profile.repeats
        attempts = []
        for i in range(max(1, repeats)):
            if runner.aborted():
                break
            result = runner.run_single(self.ff, case, clip)
            if not result.ok:
                return result
            attempts.append(result)
        if not attempts:
            failed = runner.TestResult(case)
            failed.error = "aborted"
            return failed
        if len(attempts) == 1:
            return attempts[0]
        attempts.sort(key=lambda r: r.encode_fps)
        chosen = attempts[len(attempts) // 2]
        values = [r.encode_fps for r in attempts]
        try:
            chosen.stdev_fps = statistics.stdev(values)
        except statistics.StatisticsError:
            chosen.stdev_fps = 0.0
        return chosen

    # -- concurrency phase -------------------------------------------------

    def run_concurrency(self, specs, resolutions, fps_list, complexities):
        cores = os.cpu_count() or 4
        for spec in specs:
            if runner.aborted():
                self._stop("interrupted")
                break
            if self._out_of_time(reserve=self._latency_reserve()):
                self._stop("time budget reached during the concurrency ramps")
                break
            case = matrix.concurrency_case(spec, self.profile, resolutions,
                                           fps_list, complexities)
            if not self._viable(spec, case.res_key):
                continue
            measured = self._measured_fps(spec, case.res_key, case.fps,
                                          case.complexity)
            # Skip only on evidence. A calibration estimate is biased low by
            # startup cost (invariant 3), so it must not decide this; when there
            # is no real measurement, run the ramp and let its own level-1 step
            # settle it -- run_concurrency_ramp stops after level 1 anyway if
            # that single stream cannot hold realtime.
            if measured is not None and measured < case.fps:
                self._skip(spec.name, case.res_key,
                           "single stream below realtime (%.0f fps < %d) - "
                           "ramp skipped" % (measured, case.fps))
                continue
            estimate = measured
            if estimate is None:
                estimate = self._calibrate(spec, case.res_key, case.fps,
                                           case.complexity, case.preset)
            if estimate is None:
                continue

            frames, reason = self._size_frames(case, estimate)
            if frames is None:
                continue
            case.frames = min(frames, 300)

            clip = self.lib.get(case.res_key, case.complexity)
            max_level = self.args.concurrency_max or self.profile.concurrency_max
            if not max_level:
                max_level = cores * 4 if not spec.hardware else 64

            label = "concurrency ramp: %s %s@%d" % (spec.name, case.res_key, case.fps)
            self._progress(label)

            def on_step(step_result, _label=label):
                self._progress(_label, "%d streams" % step_result.level)

            steps = runner.run_concurrency_ramp(self.ff, case, clip,
                                                max_level, on_step=on_step)
            self.ramps[spec.name] = {"case": case, "steps": steps,
                                     "max_level": max_level}
            self._record("ramp", {"encoder": spec.name, "case": case.as_dict(),
                                  "max_level": max_level,
                                  "steps": [s.as_dict() for s in steps]})
            best = runner.max_realtime_streams(steps)
            self._progress_done("%s  %s" % (
                util.grey(label),
                util.green("%d realtime streams" % best) if best
                else util.yellow("below realtime")))

    def _quality_reserve(self):
        return QUALITY_SHARE if self.args.quality else 0.0

    # -- latency phase -----------------------------------------------------

    def run_latency(self, planned):
        from . import latency
        if not planned:
            return
        # Restored records must not be measured again, or every encoder is
        # listed twice with two different numbers (invariant 27).
        done = {(r.get("encoder"), r.get("resolution"), r.get("fps"),
                 r.get("preset"), r.get("mode"))
                for r in self.latency_results}
        # Recorded before the filtering and the self-check gates below, so a
        # pass that never runs still reports what it was going to measure.
        self.latency_planned = len(planned)
        self.latency_ran = len(done)
        planned = [lc for lc in planned if lc.key not in done]
        if not planned:
            detail("latency results already complete; nothing to re-measure")
            return

        seconds = self.profile.latency_seconds
        first = planned[0].case
        # Three options the pass cannot work without, each of which fails
        # quietly rather than loudly if it is missing: -stats_period (output
        # timing would only be sampled every 0.5s, fifteen frames at 30 fps),
        # -readrate (the input could not be paced at the target rate) and
        # -readrate_initial_burst (the pacer would hand over the first half
        # second free and hide the lookahead it is there to measure).
        missing = [name for name in ("stats_period", "readrate",
                                     "readrate_initial_burst")
                   if not self.ff.has_option(name)]
        if missing:
            warn("this ffmpeg has no %s, which the latency measurement depends "
                 "on; skipping the pass rather than reporting numbers it cannot "
                 "support" % ", ".join("-" + m for m in missing))
            return

        clip = self.lib.get(first.res_key, first.complexity)
        ok, note = latency.self_check(self.ff, self.anchor_specs, clip,
                                      first.fps, seconds)
        if not ok:
            warn("latency self-check failed (%s); skipping the latency pass "
                 "rather than reporting wrong numbers" % note)
            return
        detail("latency self-check passed: %s" % note)

        modes = {}                    # encoder -> (args, partial) or (None, False)
        gated_seen = set()            # (encoder, resolution, mode) already recorded
        total = len(planned)
        index = 0
        for lc in self._in_plan_order(planned):
            index += 1
            if runner.aborted():
                self._stop("interrupted")
                break
            if self._out_of_time(reserve=self._quality_reserve()):
                self._stop("time budget reached with %d of %d latency points "
                           "still to measure" % (total - index + 1, total))
                warn("time budget reached; latency pass truncated")
                break
            case = lc.case
            spec = case.encoder
            if not self._viable(spec, case.res_key):
                continue

            # A light encoder is light here too (LIGHT_LATENCY_TIERS).
            if (lc.tier in LIGHT_LATENCY_TIERS
                    and self._light.get(spec.name, False)):
                continue

            # Do not pay for a measurement the method will refuse to report.
            gated = self._latency_gated(lc)
            if gated is not None:
                reason, rate = gated
                self.latency_gated.append({
                    "encoder": spec.name, "resolution": case.res_key,
                    "fps": case.fps, "preset": case.preset, "mode": lc.mode,
                    "measured_fps": rate, "reason": reason})
                # One skip per encoder, resolution and mode; the preset sweep
                # would otherwise record the same finding several times over.
                if (spec.name, case.res_key, lc.mode) not in gated_seen:
                    gated_seen.add((spec.name, case.res_key, lc.mode))
                    self._skip(spec.name, case.res_key, reason)
                continue

            clip = self.lib.get(case.res_key, case.complexity)
            mode_args, partial = [], False
            if lc.mode == "lowlat":
                if spec.name not in modes:
                    modes[spec.name] = latency.validate_low_latency(
                        self.ff, spec, case, clip)
                mode_args, partial = modes[spec.name]
                if mode_args is None:
                    self._skip(spec.name, case.res_key,
                               "no low-latency configuration this encoder accepts")
                    continue

            self._progress("latency %s (%s) [%d/%d]"
                           % (case.label(), lc.mode, index, total))
            record = latency.measure(self.ff, case, clip, mode=lc.mode,
                                     mode_args=mode_args, paced_seconds=seconds,
                                     axis=lc.axis, partial_mode=partial)
            self.latency_results.append(record.as_dict())
            self._record("latency", record.as_dict())
            self.latency_ran += 1
            if record.ok:
                self._progress("latency %s (%s) [%d/%d]"
                               % (case.label(), lc.mode, index, total),
                               "%.0f frames" % record.delay_frames)
            if self.args.cooldown:
                time.sleep(self.args.cooldown)

        if sys.stderr.isatty() and self._last_line:
            sys.stderr.write("\r" + " " * util.visible_len(self._last_line) + "\r")
            sys.stderr.flush()
            self._last_line = ""

    def startup_costs(self):
        from . import latency
        return latency.startup_costs(self._calib_runs, self.results)

    # -- quality phase -----------------------------------------------------

    def run_quality(self, specs, resolutions, fps_list, metrics):
        from . import quality
        cases = matrix.quality_plan(specs, self.profile, resolutions, fps_list)
        # Records restored by --resume must not be measured a second time, or
        # every encoder appears twice in the table with different numbers.
        done = {(r.get("encoder"), r.get("resolution"), r.get("fps"),
                 r.get("bitrate"), r.get("preset"))
                for r in self.quality_results}
        cases = [c for c in cases
                 if (c.encoder.name, c.res_key, c.fps, c.bitrate, c.preset) not in done]
        # Most-used encoder first (invariant 30). quality_plan yields encoders in
        # codec order, which puts libaom-av1 -- often the slowest thing on the
        # box -- first; it then consumes the whole quality pass and the encoders
        # a user actually cares about get no numbers at all. sort is stable, so
        # each encoder's low/target/high stay together.
        cases.sort(key=lambda c: probe.popularity(c.encoder))
        total = len(cases)
        if not total:
            detail("quality results already complete; nothing to re-measure")
            return
        if cases:
            probe_case = cases[0]
            probe_case.frames = min(120, self.lib.frames_for(probe_case.res_key))
            clip = self.lib.get(probe_case.res_key, probe_case.complexity)
            ok, note = quality.self_check(self.ff, probe_case, clip,
                                          self.lib.lossless_encoder)
            if not ok:
                warn("quality measurement self-check failed (%s); skipping the "
                     "quality pass rather than reporting wrong numbers" % note)
                return
            detail("quality self-check passed: %s" % note)
        for i, case in enumerate(cases, 1):
            if runner.aborted() or self._out_of_time():
                self._stop("time budget reached with %d of %d quality "
                           "measurements still to make" % (total - i + 1, total))
                warn("time budget reached; quality pass truncated")
                break
            if not self._viable(case.encoder, case.res_key):
                continue
            estimate = self._calibrate(case.encoder, case.res_key, case.fps,
                                       case.complexity, case.preset)
            if estimate is None:
                continue
            frames, _ = self._size_frames(case, estimate)
            if frames is None:
                continue
            case.frames = min(frames or 120, 240)
            clip = self.lib.get(case.res_key, case.complexity)
            self._progress("quality %s [%d/%d]" % (case.label(), i, total))
            record = quality.measure(self.ff, case, clip, metrics)
            if record:
                self.quality_results.append(record)
                self._record("quality", record)
        if sys.stderr.isatty() and self._last_line:
            sys.stderr.write("\r" + " " * util.visible_len(self._last_line) + "\r")
            self._last_line = ""

    # -- coverage ----------------------------------------------------------

    def coverage(self):
        """What the plan asked for against what ran.

        A table with nothing in it must be able to say why. Without this the
        bitrate, framerate and complexity sweeps simply vanished from the report
        when they did not run -- exactly the silent disappearance invariant 26
        exists to prevent.
        """
        axes = {}
        for axis, planned in self.axis_planned.items():
            axes[axis] = {"planned": planned,
                          "ran": self.axis_ran.get(axis, 0)}
        return {
            "planned": self._total,
            "completed": self._completed,
            "axes": axes,
            "latency": {"planned": self.latency_planned,
                        "ran": self.latency_ran},
            "light": list(self.light),
            "latency_gated": list(self.latency_gated),
            "stopped_early": self.stopped_early,
        }

    def axis_incomplete(self, axis):
        """A note for an axis that did not fully run, or None."""
        entry = self.axis_planned.get(axis)
        if not entry:
            return None
        ran = self.axis_ran.get(axis, 0)
        if ran >= entry:
            return None
        if self.stopped_matrix:
            return "not run - %s" % self.stopped_matrix
        if self.light:
            return ("%d of %d measurements did not run - see Notes"
                    % (entry - ran, entry))
        return "%d of %d measurements did not run" % (entry - ran, entry)

    # -- driver ------------------------------------------------------------

    def execute(self, planned, specs, resolutions, fps_list, complexities,
                budget_minutes, metrics=None, latency_planned=None):
        self._start = time.monotonic()
        self._budget = budget_minutes * 60.0 if budget_minutes else None

        step("Benchmarking %d configurations across %d encoders%s"
             % (len(planned), len(specs),
                " (budget %s)" % human_time(self._budget) if self._budget else
                " - no time budget; Ctrl-C stops and still reports"))
        self.run_matrix(planned, specs=specs, resolutions=resolutions,
                        fps_list=fps_list, complexities=complexities,
                        latency_planned=latency_planned)

        if not self.args.no_concurrency and not runner.aborted():
            step("Measuring concurrent stream limits")
            self.run_concurrency(specs, resolutions, fps_list, complexities)

        if not self.args.no_latency and not runner.aborted():
            step("Measuring encode latency (realtime-paced input)")
            self.run_latency(latency_planned or [])

        if self.args.quality and not runner.aborted():
            step("Measuring encode quality (PSNR/SSIM%s)"
                 % ("/VMAF" if metrics and metrics.get("vmaf") else ""))
            self.run_quality(specs, resolutions, fps_list, metrics or {})

        self.close()
        return self._elapsed()


def _pixels(res_key):
    width, height = sources.RES_BY_KEY.get(res_key, (1920, 1080))
    return width * height


def _res_gap(a, b):
    """Distance between two resolutions on the ladder, for picking the least
    extrapolated calibration point to carry across."""
    order = sources.RES_ORDER
    try:
        return order.index(a) - order.index(b)
    except ValueError:
        return len(order)


def _restore_case(data, spec):
    case = runner.TestCase(
        encoder=spec, res_key=data.get("resolution"), width=data.get("width", 0),
        height=data.get("height", 0), fps=data.get("fps", 30),
        complexity=data.get("complexity", "high"), frames=data.get("frames", 0),
        bitrate=data.get("bitrate"), preset=data.get("preset"),
        threads=data.get("threads"), rate_mode=data.get("rate_mode", "bitrate"),
        quality=data.get("quality"), kind=data.get("kind", "throughput"))
    return case


def _restore_result(data, by_name):
    spec = by_name.get(data.get("encoder"))
    if spec is None:
        return None                      # encoder no longer available; drop it
    result = runner.TestResult(_restore_case(data, spec))
    result.ok = bool(data.get("ok"))
    if not result.ok:
        return None
    result.frames = data.get("frames_encoded") or 0
    result.encode_fps = data.get("encode_fps")
    result.realtime_x = data.get("realtime_x")
    result.wall_seconds = data.get("wall_seconds")
    result.rtime = data.get("rtime")
    result.cpu_seconds = data.get("cpu_seconds")
    result.cpu_cores = data.get("cpu_cores")
    result.maxrss_bytes = data.get("maxrss_bytes")
    result.achieved_bitrate = data.get("achieved_bitrate")
    result.bitrate_error_pct = data.get("bitrate_error_pct")
    result.decode_fps = data.get("decode_fps")
    result.decode_bound = bool(data.get("decode_bound"))
    result.temp_c = data.get("temp_c")
    return result


def _restore_step(data):
    step_obj = runner.RampStep(data.get("streams", 0))
    step_obj.ok_count = data.get("ok_count", 0)
    step_obj.min_fps = data.get("min_fps")
    step_obj.mean_fps = data.get("mean_fps")
    step_obj.aggregate_fps = data.get("aggregate_fps")
    step_obj.realtime = bool(data.get("all_realtime"))
    step_obj.session_limited = bool(data.get("session_limited"))
    step_obj.error = data.get("error", "")
    return step_obj


def _key_of(record):
    return (record.get("encoder"), record.get("resolution"), record.get("fps"),
            record.get("complexity"), record.get("bitrate"), record.get("preset"),
            record.get("threads"), record.get("rate_mode"), record.get("quality"),
            record.get("kind"))
