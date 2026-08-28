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

from . import matrix, runner, sources, util
from .util import detail, human_time, step, warn

# Frames used to estimate an encoder's speed before the real run.
CALIB_FRAMES = {"360p": 24, "480p": 24, "720p": 24,
                "1080p": 16, "1440p": 12, "2160p": 8}
CALIB_TIMEOUT = 90.0

# Bounds on the timed run.
MAX_FRAMES = {"360p": 900, "480p": 900, "720p": 900,
              "1080p": 600, "1440p": 400, "2160p": 250}

# Per-test wall-clock cap by profile; anything slower is skipped, not endured.
TEST_CAP = {"quick": 20.0, "standard": 30.0, "deep": 180.0}

# Share of the time budget reserved for later phases.
CONCURRENCY_SHARE = 0.25
QUALITY_SHARE = 0.20


class Skip(object):
    def __init__(self, encoder, res_key, reason):
        self.encoder = encoder
        self.res_key = res_key
        self.reason = reason

    def as_dict(self):
        return {"encoder": self.encoder, "resolution": self.res_key,
                "reason": self.reason}


class Orchestrator(object):
    def __init__(self, ff, library, specs, profile, args, run_id, results_dir):
        self.ff = ff
        self.lib = library
        self.specs = specs
        self.profile = profile
        self.args = args
        self.run_id = run_id
        self.results_dir = results_dir

        self.results = []
        self.ramps = {}
        self.quality_results = []
        self.skips = []

        self._calib = {}          # (encoder, res) -> fps estimate or None
        self._max_res = {}        # encoder -> highest viable resolution index
        self._done_keys = set()
        self._durations = []
        self._start = None
        self._budget = None
        self._jsonl = os.path.join(results_dir, "%s.jsonl" % run_id)
        self._jsonl_fh = None
        self._total = 0
        self._completed = 0
        self._last_line = ""

    # -- budget ------------------------------------------------------------

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
        if not self._durations or self._completed >= self._total:
            return None
        mean = sum(self._durations[-25:]) / len(self._durations[-25:])
        return mean * (self._total - self._completed)

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
        if estimate is None:
            reason = result.error or "calibration failed"
            self._mark_unviable(spec, res_key, reason)
        else:
            detail("calibrated %s @%s (%s): ~%.0f fps"
                   % (spec.name, res_key, preset, estimate))
        return estimate

    def _measured_fps(self, spec, res_key, fps, complexity):
        """Best real measurement we already have for this operating point.

        Calibration runs only a handful of frames, so fixed startup cost
        (hardware device init, filter graph setup) drags its estimate well below
        sustained throughput -- measured on a Gemini Lake iGPU, 16 frames reads
        75 fps where 300 frames sustains 154. That bias is tolerable for sizing a
        run, which has floors, but not for a yes/no decision like "is this
        encoder even worth ramping". Prefer a real measurement wherever we have one.
        """
        best = None
        for result in self.results:
            case = result.case
            if (case.encoder.name == spec.name and case.res_key == res_key
                    and case.fps == fps and case.complexity == complexity
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

    # -- single-stream phase ----------------------------------------------

    def run_matrix(self, planned):
        self._total = len(planned)
        self._completed = 0

        for pc in self._by_speed(planned):
            if runner.aborted():
                break
            if self._out_of_time(reserve=self._reserve()):
                remaining = self._total - self._completed
                if remaining > 0:
                    warn("time budget reached; %d planned tests not run" % remaining)
                    self._record("budget_stop", {"remaining": remaining})
                break

            case = pc.case
            spec = case.encoder

            if not self._viable(spec, case.res_key):
                self._completed += 1
                continue
            if _key_of(case.as_dict()) in self._done_keys:
                self._completed += 1
                continue

            estimate = self._calibrate(spec, case.res_key, case.fps,
                                       case.complexity, case.preset)
            if estimate is None:
                self._completed += 1
                continue
            measured = self._measured_fps(spec, case.res_key, case.fps,
                                          case.complexity)
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

    def _by_speed(self, planned):
        """Yield tier by tier, fastest known encoder first inside each tier.

        Tier 0 calibrates every encoder, so from tier 1 on we know roughly how
        expensive each one is. Spending the remaining budget on the cheap
        measurements first means a truncated run still covers far more ground.
        """
        tiers = {}
        for pc in planned:
            tiers.setdefault(pc.tier, []).append(pc)

        for tier in sorted(tiers):
            group = tiers[tier]
            if tier > 0:
                group = sorted(group, key=lambda pc: -self._known_speed(pc.case.encoder))
            for pc in group:
                yield pc

    def _known_speed(self, spec):
        estimates = [v for (name, _res, _preset), v in self._calib.items()
                     if name == spec.name and v]
        return max(estimates) if estimates else 0.0

    def _reserve(self):
        reserve = 0.0
        if not self.args.no_concurrency:
            reserve += CONCURRENCY_SHARE
        if self.args.quality:
            reserve += QUALITY_SHARE
        return min(reserve, 0.6)

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
            if runner.aborted() or self._out_of_time(reserve=self._quality_reserve()):
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
        # Fastest encoder first (invariant 30). quality_plan yields encoders in
        # codec order, which puts libaom-av1 -- often the slowest thing on the
        # box -- first; on a tight budget it then consumes the whole quality
        # pass and the encoders a user actually cares about get no numbers at
        # all. sort is stable, so each encoder's low/target/high stay together.
        cases.sort(key=lambda c: -self._known_speed(c.encoder))
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

    # -- driver ------------------------------------------------------------

    def execute(self, planned, specs, resolutions, fps_list, complexities,
                budget_minutes, metrics=None):
        self._start = time.monotonic()
        self._budget = budget_minutes * 60.0 if budget_minutes else None

        step("Benchmarking %d configurations across %d encoders%s"
             % (len(planned), len(specs),
                "" if not self._budget else
                " (budget %s)" % human_time(self._budget)))
        self.run_matrix(planned)

        if not self.args.no_concurrency and not runner.aborted():
            step("Measuring concurrent stream limits")
            self.run_concurrency(specs, resolutions, fps_list, complexities)

        if self.args.quality and not runner.aborted():
            step("Measuring encode quality (PSNR/SSIM%s)"
                 % ("/VMAF" if metrics and metrics.get("vmaf") else ""))
            self.run_quality(specs, resolutions, fps_list, metrics or {})

        self.close()
        return self._elapsed()


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
