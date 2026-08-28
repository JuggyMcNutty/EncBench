"""encbench CLI entry point and top-level flow."""

from __future__ import annotations

import argparse
import atexit
import datetime
import os
import signal
import sys
import time

from . import (__version__, bench, ffmpeg_setup, matrix, probe, quality,
               report, runner, sources, util)
from .util import error, info, step, warn


def build_parser():
    p = argparse.ArgumentParser(
        prog="encbench",
        description="Benchmark a Linux system's video encoding capability with ffmpeg.",
        epilog="If no ffmpeg is installed, a static build is downloaded into scratch "
               "space automatically. Run with --list-encoders first to see what this "
               "machine can do.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="encbench " + __version__)

    g = p.add_argument_group("ffmpeg / scratch")
    g.add_argument("--ffmpeg", metavar="PATH", help="use this ffmpeg binary")
    g.add_argument("--download", action="store_true",
                   help="force downloading a static build even if ffmpeg is installed")
    g.add_argument("--no-download", action="store_true",
                   help="never touch the network; fail if no ffmpeg is present")
    g.add_argument("--scratch-dir", metavar="DIR",
                   help="where to cache the ffmpeg build and test clips")
    g.add_argument("--clean", action="store_true",
                   help="delete cached test clips when finished")

    g = p.add_argument_group("discovery")
    g.add_argument("--list-encoders", action="store_true",
                   help="probe the system, print the encoder inventory, and exit")
    g.add_argument("--encoders", metavar="A,B", help="only benchmark these encoders")
    g.add_argument("--exclude", metavar="A,B", help="skip these encoders")
    g.add_argument("--all-encoders", action="store_true",
                   help="include every video encoder, not just delivery codecs")
    g.add_argument("--extended-codecs", action="store_true",
                   help="also include mpeg2/mpeg4/vvc/theora/prores")
    g.add_argument("--hw-only", action="store_true", help="hardware encoders only")
    g.add_argument("--sw-only", action="store_true", help="software encoders only")

    g = p.add_argument_group("workload")
    g.add_argument("--profile", choices=sorted(matrix.PROFILES), default="standard",
                   help="benchmark depth (default: standard, roughly 15-25 min)")
    g.add_argument("--time-budget", type=float, metavar="MINUTES",
                   help="stop starting new tests after this long")
    g.add_argument("--resolutions", metavar="A,B",
                   help="e.g. 720p,1080p (default: from profile)")
    g.add_argument("--fps", metavar="A,B", help="e.g. 30,60")
    g.add_argument("--bitrates", metavar="A,B", help="any of low,target,high")
    g.add_argument("--complexity", metavar="A,B", help="any of low,high")
    g.add_argument("--presets", type=int, metavar="N",
                   help="how many speed-knob values to sample per encoder")
    g.add_argument("--repeats", type=int, metavar="N",
                   help="timed runs per configuration; the median is reported")
    g.add_argument("--frames", type=int, metavar="N",
                   help="fixed frame count per test (default: sized automatically)")
    g.add_argument("--cooldown", type=float, default=0.0, metavar="SECONDS",
                   help="pause between tests")

    g = p.add_argument_group("measurements")
    g.add_argument("--quality", action="store_true",
                   help="also measure PSNR/SSIM/VMAF (slower)")
    g.add_argument("--no-concurrency", action="store_true",
                   help="skip the concurrent-stream ramp")
    g.add_argument("--concurrency-max", type=int, metavar="N",
                   help="highest parallel stream count to try")
    g.add_argument("--source", metavar="FILE",
                   help="use your own footage instead of generated clips")
    g.add_argument("--source-format", choices=("raw", "lossless"),
                   help="raw removes the decode ceiling but needs more scratch space")

    g = p.add_argument_group("output")
    g.add_argument("--json", metavar="PATH", help="write full results here")
    g.add_argument("--html", metavar="PATH", help="write an HTML report here")
    g.add_argument("--no-report-files", action="store_true",
                   help="terminal output only")
    g.add_argument("--resume", metavar="FILE",
                   help="continue a previous run from its .jsonl file")
    g.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"),
                   help="compare two saved runs and exit")
    g.add_argument("-v", "--verbose", action="count", default=0)
    g.add_argument("-q", "--quiet", action="store_true")
    g.add_argument("--no-color", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    util.init_color(False if args.no_color else None)
    util.set_verbosity(0 if args.quiet else (2 if args.verbose else 1))

    if args.hw_only and args.sw_only:
        error("--hw-only and --sw-only are mutually exclusive")
        return 2

    _install_signal_handlers()
    atexit.register(runner.terminate_all)

    try:
        if args.compare:
            return compare_runs(args.compare[0], args.compare[1])
        return run(args)
    except KeyboardInterrupt:
        runner.terminate_all()
        sys.stderr.write("\n")
        error("interrupted")
        return 130
    except RuntimeError as e:
        runner.terminate_all()
        error(str(e))
        return 1


def _install_signal_handlers():
    def handler(signum, frame):
        if runner.aborted():
            os._exit(130)                     # second interrupt: leave now
        sys.stderr.write("\n")
        warn("interrupt received - killing ffmpeg processes and reporting "
             "what finished (press Ctrl-C again to exit immediately)")
        runner.terminate_all()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------------------

def run(args):
    profile = matrix.clone_profile(matrix.PROFILES[args.profile])
    resolutions = _resolutions(args, profile)
    fps_list = _fps(args, profile)
    complexities = _complexities(args, profile)
    bitrates = _bitrates(args, profile)
    if args.presets:
        profile.preset_count = max(1, args.presets)

    # Estimate the source cache up front so scratch selection can avoid
    # filling a small tmpfs.
    needed = sources.estimate_bytes(resolutions, complexities, "raw", args.frames)
    scratch, scratch_note = ffmpeg_setup.choose_scratch(
        args.scratch_dir, needed + 200 * 1024 * 1024)

    ff = ffmpeg_setup.acquire(args, scratch)

    step("Inspecting system")
    sysinfo = probe.system_info()

    step("Probing encoders (every candidate gets a real 2-frame encode)")
    specs = probe.probe_encoders(
        ff, sysinfo,
        all_encoders=args.all_encoders,
        only=_split(args.encoders),
        exclude=_split(args.exclude),
        extended=args.extended_codecs,
    )
    hints = probe.diagnose(specs, sysinfo)

    report.render_system(sysinfo, ff, scratch_note)
    report.render_encoders(specs, show_failed=args.verbose > 0)
    report.render_diagnostics(hints)

    usable = [s for s in specs if s.ok]
    if args.hw_only:
        usable = [s for s in usable if s.hardware]
    if args.sw_only:
        usable = [s for s in usable if not s.hardware]

    if args.list_encoders:
        print()
        return 0 if usable else 1
    if not usable:
        error("no usable video encoders on this system; nothing to benchmark")
        return 1

    fmt = sources.choose_format(scratch, resolutions, complexities,
                                args.source_format, args.frames)
    library = sources.SourceLibrary(ff, scratch, fmt=fmt,
                                    frames_override=args.frames,
                                    user_source=args.source)

    metrics = {}
    if args.quality:
        metrics = quality.detect(ff)
        if not any(metrics.values()):
            warn("this ffmpeg build has no psnr/ssim/libvmaf filters; "
                 "quality measurement disabled")
            args.quality = False
        else:
            info("  %s" % util.grey("quality metrics available: %s"
                                    % quality.describe(metrics)))

    planned = matrix.build_plan(usable, profile, resolutions, fps_list,
                                complexities, bitrates)

    run_id = "%s-%s" % (sysinfo["hostname"],
                        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    results_dir = _results_dir(args)

    orch = bench.Orchestrator(ff, library, usable, profile, args,
                              run_id, results_dir)
    if args.resume:
        n = orch.load_resume(args.resume)
        info("  %s" % util.grey("resuming: %d completed tests loaded" % n))

    budget = args.time_budget if args.time_budget is not None else profile.budget_minutes
    elapsed = orch.execute(planned, usable, resolutions, fps_list,
                           complexities, budget, metrics)

    base_res = matrix.baseline_resolution(resolutions)
    base_fps = matrix.baseline_fps(fps_list)
    base_cx = matrix.baseline_complexity(complexities)

    render_all(orch, base_res, base_fps, base_cx, sysinfo, elapsed)

    payload = build_payload(orch, run_id, profile, ff, sysinfo, scratch_note,
                            library, specs, hints, elapsed, base_res, base_fps,
                            base_cx, metrics)
    _write_outputs(args, payload, results_dir, run_id)

    if args.clean:
        library.cleanup()
        print("  %s" % util.grey("scratch clips removed"))

    return 0 if orch.results else 1


def render_all(orch, base_res, base_fps, base_cx, sysinfo, elapsed):
    results = orch.results
    if not results:
        warn("no successful measurements")
        return
    report.render_headline(results, orch.ramps, base_res, base_fps)
    report.render_resolution_scaling(results, base_fps, base_cx)
    report.render_preset_sweep(results, base_res, base_fps)

    report.render_axis(
        results, "Throughput by target bitrate",
        "does bitrate move throughput? at %s%d, %s complexity"
        % (base_res, base_fps, base_cx),
        "bitrate", base_res,
        {"fps": base_fps, "complexity": base_cx},
        lambda r: report.fps_cell(r, base_fps),
        headers_fmt=lambda v: util.human_rate(v))

    report.render_axis(
        results, "Throughput by framerate",
        "same pixels per frame, different realtime targets, at %s" % base_res,
        "fps", base_res, {"complexity": base_cx},
        lambda r: report.fps_cell(r, r.case.fps),
        headers_fmt=lambda v: "%d fps" % v)

    report.render_axis(
        results, "Throughput by content complexity",
        "low = smooth gradients and slow motion; high = dense detail, "
        "constant motion and grain",
        "complexity", base_res, {"fps": base_fps},
        lambda r: report.fps_cell(r, base_fps),
        headers_fmt=lambda v: str(v).upper())

    report.render_concurrency(orch.ramps)
    report.render_quality(orch.quality_results)
    report.render_warnings(results, orch.skips, sysinfo, elapsed)

    print()
    print("  %s" % util.grey("%d measurements in %s"
                             % (len(results), util.human_time(elapsed))))


def build_payload(orch, run_id, profile, ff, sysinfo, scratch_note, library,
                  specs, hints, elapsed, base_res, base_fps, base_cx, metrics):
    return {
        "encbench_version": __version__,
        "run_id": run_id,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration_seconds": round(elapsed, 1),
        "profile": profile.name,
        "baseline": {"resolution": base_res, "fps": base_fps,
                     "complexity": base_cx},
        # Which preset each encoder's resolution sweep was run at. Without this
        # a report cannot tell the sweep's row at the baseline resolution apart
        # from the extra rows the preset sweep adds there.
        "baseline_presets": {s.name: matrix.balanced_preset(s)
                             for s in orch.specs},
        "system": sysinfo,
        "ffmpeg": ff.as_dict(),
        "scratch": scratch_note,
        "quality_metrics": metrics,
        "sources": [c.as_dict() for c in library.clips()],
        "encoders": [s.as_dict() for s in specs],
        "diagnostics": hints,
        "results": [r.as_dict() for r in orch.results],
        "concurrency": {
            name: {"case": entry["case"].as_dict(),
                   "max_level": entry.get("max_level"),
                   "steps": [s.as_dict() for s in entry["steps"]]}
            for name, entry in orch.ramps.items()
        },
        "quality": orch.quality_results,
        "skips": [s.as_dict() for s in orch.skips],
    }


def _write_outputs(args, payload, results_dir, run_id):
    if args.no_report_files:
        return
    json_path = args.json or os.path.join(results_dir, "%s.json" % run_id)
    html_path = args.html or os.path.join(results_dir, "%s.html" % run_id)
    try:
        report.write_json(json_path, payload)
        print("  %s %s" % (util.grey("results:"), json_path))
    except OSError as e:
        warn("could not write JSON results: %s" % e)
    try:
        report.write_html(html_path, payload)
        print("  %s %s" % (util.grey("report: "), html_path))
    except OSError as e:
        warn("could not write HTML report: %s" % e)


def _results_dir(args):
    if args.json:
        d = os.path.dirname(os.path.abspath(args.json))
    else:
        d = os.path.join(os.getcwd(), "results")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.getcwd()
    return d


def compare_runs(path_a, path_b):
    import json
    try:
        with open(path_a) as fh:
            a = json.load(fh)
        with open(path_b) as fh:
            b = json.load(fh)
    except (OSError, ValueError) as e:
        error("cannot read comparison inputs: %s" % e)
        return 1
    report.render_comparison(a, b)
    return 0


# --------------------------------------------------------------------------

def _split(value):
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


def _resolutions(args, profile):
    chosen = _split(args.resolutions) or profile.resolutions
    bad = [r for r in chosen if r not in sources.RES_BY_KEY]
    if bad:
        raise RuntimeError("unknown resolution(s): %s (valid: %s)"
                           % (", ".join(bad), ", ".join(sources.RES_ORDER)))
    return sorted(chosen, key=sources.RES_ORDER.index)


def _fps(args, profile):
    raw = _split(args.fps)
    if not raw:
        return profile.fps_list
    try:
        return sorted({int(x) for x in raw})
    except ValueError:
        raise RuntimeError("--fps takes integers, e.g. --fps 30,60")


def _complexities(args, profile):
    chosen = _split(args.complexity) or profile.complexities
    bad = [c for c in chosen if c not in sources.COMPLEXITIES]
    if bad:
        raise RuntimeError("unknown complexity: %s (valid: low, high)" % ", ".join(bad))
    return chosen


def _bitrates(args, profile):
    chosen = _split(args.bitrates) or profile.bitrates
    bad = [b for b in chosen if b not in matrix.BITRATE_NAMES]
    if bad:
        raise RuntimeError("unknown bitrate tier: %s (valid: low, target, high)"
                           % ", ".join(bad))
    return chosen


if __name__ == "__main__":
    sys.exit(main())
