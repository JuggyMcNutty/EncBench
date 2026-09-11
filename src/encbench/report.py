"""Rendering: terminal tables, system/encoder summaries, JSON, HTML."""

from __future__ import annotations

import json
import os
import sys

from . import util
from .util import bold, cyan, dim, green, grey, pad, red, visible_len, yellow

_UNICODE = None


def unicode_ok():
    global _UNICODE
    if _UNICODE is None:
        enc = (getattr(sys.stdout, "encoding", None) or "").lower()
        _UNICODE = "utf" in enc
    return _UNICODE


def rule_char():
    return "─" if unicode_ok() else "-"


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def section(title, out=None):
    out = out or sys.stdout
    width = min(util.term_width(), 100)
    line = rule_char() * width
    out.write("\n%s\n%s\n%s\n" % (grey(line), bold(title.upper()), grey(line)))


def subsection(title, out=None):
    out = out or sys.stdout
    out.write("\n%s\n" % bold(title))


def table(headers, rows, aligns=None, indent=2, out=None, note=None):
    """Render an ANSI-aware fixed-width table. Empty rows render a hint."""
    out = out or sys.stdout
    if not rows:
        out.write("%s%s\n" % (" " * indent, grey(note or "(nothing to show)")))
        return
    ncols = len(headers)
    aligns = aligns or ["left"] * ncols
    widths = [visible_len(str(h)) for h in headers]
    norm_rows = []
    for row in rows:
        cells = [("" if c is None else str(c)) for c in row]
        cells += [""] * (ncols - len(cells))
        norm_rows.append(cells)
        for i, cell in enumerate(cells[:ncols]):
            widths[i] = max(widths[i], visible_len(cell))

    prefix = " " * indent
    head = prefix + "  ".join(pad(bold(str(h)), w, aligns[i])
                              for i, (h, w) in enumerate(zip(headers, widths)))
    out.write(head.rstrip() + "\n")
    out.write(prefix + "  ".join(grey(rule_char() * w) for w in widths) + "\n")
    for cells in norm_rows:
        out.write(prefix + "  ".join(pad(cells[i], widths[i], aligns[i])
                                     for i in range(ncols)).rstrip() + "\n")


def kv(pairs, indent=2, out=None):
    out = out or sys.stdout
    if not pairs:
        return
    width = max(len(k) for k, _ in pairs)
    for k, v in pairs:
        out.write("%s%s  %s\n" % (" " * indent, grey(pad(k + ":", width + 1)), v))


def bar(value, peak, width=18):
    """Small horizontal bar for at-a-glance comparison."""
    if not peak or value is None or value <= 0:
        return grey("." * width) if unicode_ok() is False else grey("·" * width)
    filled = max(1, int(round(width * float(value) / float(peak))))
    filled = min(filled, width)
    block = "█" if unicode_ok() else "#"
    empty = "·" if unicode_ok() else "."
    return cyan(block * filled) + grey(empty * (width - filled))


# --------------------------------------------------------------------------
# system + encoder summaries
# --------------------------------------------------------------------------

def render_system(sysinfo, ff, scratch_note, out=None):
    out = out or sys.stdout
    section("System", out)
    cpu = sysinfo["cpu"]
    cpu_line = "%s  (%d physical / %d logical%s)" % (
        cpu["model"], cpu["physical"], cpu["logical"],
        ", SMT" if cpu["smt"] else "")
    if cpu.get("max_mhz"):
        cpu_line += "  @ %.2f GHz max" % (cpu["max_mhz"] / 1000.0)

    pairs = [
        ("Host", sysinfo["hostname"]),
        ("Distro", "%s  (kernel %s, %s)" % (sysinfo["distro"], sysinfo["kernel"], sysinfo["arch"])),
        ("CPU", cpu_line),
        ("Memory", "%s total, %s available" % (
            util.human_bytes(sysinfo["memory_bytes"]),
            util.human_bytes(sysinfo["memory_available_bytes"]))),
    ]
    if cpu.get("quota"):
        pairs.append(("CPU quota", yellow("%.2f cores (cgroup limited)" % cpu["quota"])))
    if sysinfo.get("governor"):
        gov = sysinfo["governor"]
        pairs.append(("Governor", yellow(gov) if gov in ("powersave", "conservative") else gov))
    if sysinfo.get("container"):
        pairs.append(("Container", yellow(sysinfo["container"])))
    load = sysinfo.get("loadavg") or [0, 0, 0]
    pairs.append(("Load avg", "%.2f  %.2f  %.2f" % tuple(load[:3])))

    for i, gpu in enumerate(sysinfo.get("gpus", [])):
        name = gpu.get("name") or gpu.get("vendor") or "unknown"
        extra = []
        if gpu.get("render_node"):
            extra.append(gpu["render_node"])
        if gpu.get("driver"):
            extra.append("driver %s" % gpu["driver"])
        label = "GPU %d" % i if len(sysinfo.get("gpus", [])) > 1 else "GPU"
        pairs.append((label, "%s%s" % (name, ("  [%s]" % ", ".join(extra)) if extra else "")))
    if not sysinfo.get("gpus"):
        pairs.append(("GPU", grey("none detected (no /dev/dri render node)")))

    origin_note = {
        "system": green("system install"),
        "downloaded": yellow("downloaded static build"),
        "user": "user-specified",
    }.get(ff.origin, ff.origin)
    pairs.append(("ffmpeg", "%s  %s  %s" % (ff.version_str, grey(ff.path), origin_note)))
    if scratch_note:
        pairs.append(("Scratch", "%s  %s" % (
            scratch_note["path"],
            grey("(%s, %s free)" % (scratch_note["fstype"],
                                    util.human_bytes(scratch_note["free_bytes"]))))))
    kv(pairs, out=out)


def render_encoders(specs, out=None, show_failed=True):
    out = out or sys.stdout
    section("Encoders", out)

    ok = [s for s in specs if s.ok]
    bad = [s for s in specs if not s.ok]

    rows = []
    for s in ok:
        kind = green("HW") if s.hardware else "SW"
        knob = "-"
        if s.speed_knob and s.speed_values:
            vals = s.speed_values
            knob = "%s: %s" % (s.speed_knob, ",".join(vals[:3]) + ("..." if len(vals) > 3 else ""))
        rows.append([s.name, kind, s.codec,
                     s.family if s.hardware else grey("cpu"),
                     s.device or "", knob])
    table(["ENCODER", "TYPE", "CODEC", "BACKEND", "DEVICE", "SPEED KNOB"], rows,
          out=out, note="no usable video encoders found")

    out.write("\n%s\n" % ("  " + green("%d usable" % len(ok)) +
                          grey("  |  %d listed by ffmpeg but not usable here" % len(bad))))

    if show_failed and bad:
        subsection("  Unavailable (listed by ffmpeg, rejected by live probe)", out)
        rows = [[s.name, s.codec, grey(s.error or "failed")] for s in bad]
        table(["ENCODER", "CODEC", "REASON"], rows, indent=4, out=out)


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------

def write_html(path, payload):
    from .html_report import write_html as _write
    return _write(path, payload)


def write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False, default=str)
    os.replace(tmp, path)
    return path


def render_diagnostics(hints, out=None):
    """Actionable guidance first; 'no such hardware' notes dimmed below it."""
    out = out or sys.stdout
    if not hints:
        return
    actionable = [h for h in hints if h["severity"] == "actionable"]
    info_only = [h for h in hints if h["severity"] != "actionable"]

    if actionable:
        section("What this system could also do", out)
        for h in actionable:
            out.write("  %s %s\n" % (yellow("!"), bold(h["title"])))
            for line in h["lines"]:
                out.write("      %s\n" % line)
            out.write("      %s\n\n" % grey("affects: " + ", ".join(h["encoders"])))

    if info_only:
        if not actionable:
            section("Unavailable backends", out)
        else:
            subsection("  Expected absences", out)
        for h in info_only:
            out.write("  %s %s\n" % (grey("-"), grey(h["title"])))
            for line in h["lines"][:2]:
                out.write("      %s\n" % grey(line))


# --------------------------------------------------------------------------
# benchmark results
# --------------------------------------------------------------------------

def _pick(results, **criteria):
    out = []
    for r in results:
        case = r.case
        if all(getattr(case, k) == v for k, v in criteria.items()):
            out.append(r)
    return out


def fps_cell(result, target_fps):
    return _fps_cell(result, target_fps)


def _fps_cell(result, target_fps):
    if result is None or result.encode_fps is None:
        return grey("-")
    rt = result.realtime_x or 0
    text = "%s %sx" % (_fps_str(result.encode_fps), util.fmt_num(rt, 1))
    if result.decode_bound:
        return yellow(text + "*")
    return green(text) if rt >= 1.0 else text


# One-pass ABR needs roughly this much encoded video before the achieved
# bitrate settles near the target -- measured, not guessed: at 1080p30 a
# libx264 run lands +70% at 4s, +18% at 8s, +9% at 10s and +4% at 15s.
CONVERGENCE_SECONDS = 10.0


def converged(result):
    return result.frames >= result.case.fps * CONVERGENCE_SECONDS


def _rate_cell(result):
    """Achieved bitrate, but only when the run was long enough to mean anything."""
    if result.achieved_bitrate is None:
        return grey("-")
    if not converged(result):
        return grey("short run")
    text = util.human_rate(result.achieved_bitrate)
    if result.bitrate_error_pct is not None and abs(result.bitrate_error_pct) > 25:
        return yellow(text)
    return text


def _fps_str(fps):
    if fps is None:
        return "-"
    if fps >= 1000:
        return "%.0f" % fps
    if fps >= 100:
        return "%.0f" % fps
    return "%.1f" % fps


def _encoder_order(results):
    seen, order = {}, []
    for r in results:
        name = r.case.encoder.name
        if name not in seen:
            seen[name] = r.case.encoder
            order.append(r.case.encoder)
    # Same ranking the run executes in (probe.popularity), so the table leads
    # with the encoders most readers came for rather than with whatever codec
    # sorts first alphabetically -- and so a partial report reads top-down.
    from .probe import popularity
    order.sort(key=lambda s: (popularity(s), s.codec, not s.hardware, s.name))
    return order


def _kind(spec):
    return green("HW") if spec.hardware else "SW"


def render_headline(results, ramps, base_res, base_fps, out=None,
                    latency_records=None):
    """The three numbers most people actually came for."""
    out = out or sys.stdout
    base = [r for r in results
            if r.case.res_key == base_res and r.case.fps == base_fps
            and r.case.kind == "throughput" and r.encode_fps]
    if not base:
        return
    section("Headline", out)

    fastest = max(base, key=lambda r: r.encode_fps)
    lines = [("Fastest at %s%d" % (base_res, base_fps),
              "%s  %s fps  (%sx realtime)%s" % (
                  bold(fastest.case.encoder.name),
                  _fps_str(fastest.encode_fps),
                  util.fmt_num(fastest.realtime_x, 1),
                  grey("  preset %s" % fastest.case.preset)
                  if fastest.case.preset is not None else ""))]

    hw = [r for r in base if r.case.encoder.hardware]
    if hw:
        best_hw = max(hw, key=lambda r: r.encode_fps)
        lines.append(("Fastest hardware",
                      "%s  %s fps  (%sx realtime)" % (
                          bold(best_hw.case.encoder.name),
                          _fps_str(best_hw.encode_fps),
                          util.fmt_num(best_hw.realtime_x, 1))))

    efficient = [r for r in base if r.cpu_cores and r.cpu_cores > 0.05]
    if efficient:
        best_eff = max(efficient, key=lambda r: r.encode_fps / r.cpu_cores)
        lines.append(("Most CPU-efficient",
                      "%s  %s fps per core used" % (
                          bold(best_eff.case.encoder.name),
                          _fps_str(best_eff.encode_fps / best_eff.cpu_cores))))

    if ramps:
        best_name, best_streams = None, 0
        for name, entry in ramps.items():
            from .runner import max_realtime_streams
            n = max_realtime_streams(entry["steps"])
            if n > best_streams:
                best_name, best_streams = name, n
        if best_name:
            lines.append(("Most parallel streams",
                          "%s  %s simultaneous %s%d streams" % (
                              bold(best_name), green(str(best_streams)),
                              base_res, base_fps)))

    # Deliberately the default-settings figure: the point of the headline is
    # that the fastest encoder in the table above is often not the one to reach
    # for when latency matters.
    lat = [r for r in (latency_records or [])
           if r.get("ok") and r.get("mode") == "default"
           and r.get("axis") == "baseline" and r.get("delay_ms") is not None]
    if lat:
        best = min(lat, key=lambda r: r["delay_ms"])
        lines.append(("Lowest latency",
                      "%s  %.0f ms  (%.0f frames held) at default settings" % (
                          bold(best["encoder"]), best["delay_ms"],
                          best["delay_frames"])))
    kv(lines, out=out)


def render_resolution_scaling(results, base_fps, base_cx, out=None):
    out = out or sys.stdout
    # Only the resolution sweep's own rows: at the baseline resolution the
    # preset sweep contributes extra rows for the same encoder, and mixing
    # those in would compare one resolution's slow preset against another's
    # balanced one.
    from .matrix import bitrate_for
    rows_source = [r for r in results
                   if r.case.fps == base_fps and r.case.complexity == base_cx
                   and r.case.kind == "throughput"
                   and r.case.preset == _baseline_preset(r.case.encoder)
                   # Pin the bitrate as well: the bitrate sweep contributes
                   # further rows at the baseline resolution, and without this
                   # the cell is filled by whichever happened to land first.
                   and r.case.bitrate == bitrate_for(r.case.res_key, "target")]
    if not rows_source:
        return
    res_keys = [k for k in _res_order()
                if any(r.case.res_key == k for r in rows_source)]
    if not res_keys:
        return

    section("Throughput by resolution", out)
    out.write("  %s\n\n" % grey(
        "encode fps and multiple of realtime, at %d fps, %s-complexity content, "
        "target bitrate" % (base_fps, base_cx)))

    encoders = _encoder_order(rows_source)
    rows = []
    for spec in encoders:
        row = [spec.name, _kind(spec), spec.codec]
        for key in res_keys:
            match = [r for r in rows_source
                     if r.case.encoder.name == spec.name and r.case.res_key == key]
            row.append(_fps_cell(match[0] if match else None, base_fps))
        rows.append(row)
    headers = ["ENCODER", "TYPE", "CODEC"] + [k.upper() for k in res_keys]
    aligns = ["left", "left", "left"] + ["right"] * len(res_keys)
    table(headers, rows, aligns=aligns, out=out)
    _footnote(rows_source, out)


def _baseline_preset(spec):
    from .matrix import balanced_preset
    return balanced_preset(spec)


def _res_order():
    from .sources import RES_ORDER
    return RES_ORDER


def _footnote(results, out):
    if any(r.decode_bound for r in results):
        out.write("\n  %s\n" % yellow(
            "* decode-bound: the encoder outran the source decoder, so the true "
            "encode rate is higher than shown"))


def render_preset_sweep(results, base_res, base_fps, out=None):
    out = out or sys.stdout
    from .matrix import bitrate_for
    target = bitrate_for(base_res, "target")
    sel = [r for r in results
           if r.case.res_key == base_res and r.case.fps == base_fps
           and r.case.kind == "throughput" and r.case.preset is not None
           and r.case.bitrate == target]
    if not sel:
        return
    by_encoder = {}
    for r in sel:
        by_encoder.setdefault(r.case.encoder.name, []).append(r)
    by_encoder = {k: v for k, v in by_encoder.items() if len(v) > 1}
    if not by_encoder:
        return

    section("Speed / efficiency tradeoff", out)
    out.write("  %s\n\n" % grey(
        "each encoder's own speed knob at %s%d - shows what a preset actually buys"
        % (base_res, base_fps)))

    rows = []
    peak = max(r.encode_fps for r in sel if r.encode_fps)
    for name in sorted(by_encoder):
        entries = sorted(by_encoder[name], key=lambda r: -(r.encode_fps or 0))
        knob = entries[0].case.encoder.speed_knob or "preset"
        for i, r in enumerate(entries):
            rows.append([
                name if i == 0 else "",
                "%s=%s" % (knob, r.case.preset),
                _fps_str(r.encode_fps),
                "%sx" % util.fmt_num(r.realtime_x, 1),
                _rate_cell(r),
                bar(r.encode_fps, peak, 16),
            ])
    table(["ENCODER", "SETTING", "FPS", "REALTIME", "ACTUAL RATE", ""],
          rows, aligns=["left", "left", "right", "right", "right", "left"], out=out)
    if any(not converged(r) for r in sel):
        out.write("\n  %s\n" % grey(
            "'short run' = under %ds of encoded video, where one-pass rate control "
            "has not converged; the achieved bitrate would be misleading there. "
            "The fps figures are unaffected." % int(CONVERGENCE_SECONDS)))


def render_axis(results, title, note, axis_attr, base_res, fixed, fmt,
                out=None, headers_fmt=None, incomplete=None):
    """Generic one-axis table: rows are encoders, columns are axis values.

    A sweep with fewer than two columns has nothing to show, but saying nothing
    at all is worse: three of these tables once disappeared from a report
    because the run ran out of time, with the only trace on stderr during the
    run. `incomplete` carries the reason so the section can say so.
    """
    out = out or sys.stdout
    from .matrix import bitrate_for
    sel = [r for r in results if r.case.kind == "throughput"
           and r.case.res_key == base_res
           and r.case.preset == _baseline_preset(r.case.encoder)
           and all(getattr(r.case, k) == v for k, v in fixed.items())]
    if axis_attr != "bitrate":
        target = bitrate_for(base_res, "target")
        sel = [r for r in sel if r.case.bitrate == target]
    values = sorted({getattr(r.case, axis_attr) for r in sel})
    if len(values) < 2:
        if incomplete:
            section(title, out)
            out.write("  %s\n" % yellow(incomplete))
        return

    section(title, out)
    out.write("  %s\n\n" % grey(note))
    encoders = _encoder_order(sel)
    rows = []
    for spec in encoders:
        row = [spec.name, _kind(spec)]
        for value in values:
            match = [r for r in sel if r.case.encoder.name == spec.name
                     and getattr(r.case, axis_attr) == value]
            row.append(fmt(match[0]) if match else grey("-"))
        rows.append(row)
    headers = ["ENCODER", "TYPE"] + [
        (headers_fmt(v) if headers_fmt else str(v)) for v in values]
    table(headers, rows, aligns=["left", "left"] + ["right"] * len(values), out=out)


def render_concurrency(ramps, out=None):
    out = out or sys.stdout
    if not ramps:
        return
    from .runner import max_realtime_streams, peak_aggregate

    section("Concurrent stream capacity", out)
    first = next(iter(ramps.values()))["case"]
    out.write("  %s\n\n" % grey(
        "identical %s%d streams launched together; 'realtime streams' is the most "
        "that ALL held %d fps" % (first.res_key, first.fps, first.fps)))

    rows = []
    peak_all = 0
    for entry in ramps.values():
        agg = peak_aggregate(entry["steps"])
        if agg:
            peak_all = max(peak_all, agg)

    for name in sorted(ramps, key=lambda n: -max_realtime_streams(ramps[n]["steps"])):
        entry = ramps[name]
        steps = entry["steps"]
        best = max_realtime_streams(steps)
        agg = peak_aggregate(steps)
        limit = ""
        limited = [s for s in steps if s.session_limited]
        last = steps[-1] if steps else None
        if limited:
            limit = red("hardware session limit at %d" % limited[0].level)
        elif last is not None and last.failed_streams:
            limit = red("%d of %d streams failed at %d - not a capacity limit"
                        % (last.failed_streams, last.level, last.level))
        elif last is not None and last.at_ceiling:
            # Never present a configured cap as if it were the machine's limit.
            limit = yellow("hit ramp ceiling %d - raise --concurrency-max"
                           % last.level)
        elif last is not None and not last.realtime:
            limit = grey("saturated at %d" % last.level)
        rows.append([
            name,
            _kind(entry["case"].encoder),
            green(str(best)) if best else yellow("0"),
            _fps_str(agg) if agg else "-",
            bar(agg, peak_all, 16),
            limit,
        ])
    table(["ENCODER", "TYPE", "REALTIME STREAMS", "PEAK AGG FPS", "", "LIMIT"],
          rows, aligns=["left", "left", "right", "right", "left", "left"], out=out)


# Order latency rows the way the pass generates them, so the baseline reads
# first and each sweep follows it.
_LATENCY_AXIS_ORDER = {"baseline": 0, "low-latency": 1, "resolution": 2,
                       "preset": 3, "fps": 4}


def _ms(value):
    if value is None:
        return grey("-")
    return "%.0f ms" % value


def _latency_point(record):
    preset = record.get("preset")
    return "%s%s%s" % (record.get("resolution"), record.get("fps"),
                       "" if preset is None else "  " + str(preset))


def render_latency(records, out=None):
    """Delay per encoder, per operating point.

    A run that could not be measured still gets a row: an encoder that cannot
    hold realtime has no meaningful delay figure, and saying so is the answer,
    not an omission.
    """
    out = out or sys.stdout
    if not records:
        return
    section("Encode latency", out)
    out.write("  %s\n" % grey(
        "input paced at realtime; delay is how many frames the encoder holds "
        "before its first packet emerges - lookahead plus frame reordering."))
    out.write("  %s\n\n" % grey(
        "this, not throughput, is what decides whether a box can run live."))

    ordered = sorted(records, key=lambda r: (
        r.get("codec") or "", not r.get("hardware"), r.get("encoder") or "",
        _LATENCY_AXIS_ORDER.get(r.get("axis"), 9),
        _res_order().index(r["resolution"]) if r.get("resolution") in _res_order() else 9,
        r.get("fps") or 0))

    rows = []
    last = None
    for r in ordered:
        name = r.get("encoder")
        mode = "low-latency" if r.get("mode") == "lowlat" else "default"
        if r.get("partial_mode"):
            mode = yellow(mode + "*")
        if not r.get("ok"):
            # The reason goes in the trailing column, never in a numeric one:
            # table() sizes each column to its widest cell, so a sentence in
            # the delay column would pad every delay figure out to its width.
            rows.append([name if name != last else "", _kind_flag(r),
                         _latency_point(r), mode, "", "", "", "",
                         grey(r.get("error") or "not measured")])
            last = name
            continue
        delay = "%.1f" % r["delay_frames"]
        if r.get("low_confidence"):
            delay = yellow(delay + "?")
        rows.append([
            name if name != last else "",
            _kind_flag(r),
            _latency_point(r),
            mode,
            delay,
            _ms(r.get("delay_ms")),
            "+%.1f" % (r.get("worst_excursion_frames") or 0.0),
            "%.2f ms" % r["frame_time_ms"] if r.get("frame_time_ms") else grey("-"),
            "",
        ])
        last = name

    table(["ENCODER", "TYPE", "POINT", "MODE", "DELAY fr", "DELAY", "WORST fr",
           "FRAME TIME", "NOTE"], rows,
          aligns=["left", "left", "left", "left", "right", "right", "right",
                  "right", "left"], out=out)

    notes = []
    if any(r.get("low_confidence") for r in records if r.get("ok")):
        notes.append(
            "'?' marks a point running under 4x realtime, where the paced and "
            "unpaced runs are close enough together that the delay carries "
            "roughly a frame of uncertainty either way.")
    if any(r.get("partial_mode") for r in records):
        notes.append(
            "'*' means the encoder rejected the full low-latency configuration "
            "and a narrower one was used, so the delay shown is not the lowest "
            "that encoder could reach.")
    if any(r.get("worst_excursion_frames") for r in records if r.get("ok")):
        notes.append(
            "'worst' is the largest transient the output fell behind its own "
            "steady schedule - the stall a live pipeline sees as a glitch, on "
            "top of the constant delay.")
    for note in notes:
        out.write("\n  %s\n" % grey(note))


def _kind_flag(record):
    return green("HW") if record.get("hardware") else "SW"


def render_startup(costs, out=None):
    """Fixed cost of starting an encode, from data the run already produced."""
    out = out or sys.stdout
    if not costs:
        return
    from .latency import startup_by_encoder
    summary = startup_by_encoder(costs)
    if not summary:
        return

    section("Startup cost", out)
    out.write("  %s\n\n" % grey(
        "fixed overhead of one ffmpeg invocation - process start, hardware "
        "device init, filter setup and teardown - separated from encode work. "
        "It costs nothing to measure: every configuration was already run at "
        "two different lengths."))

    peak = max(v["startup_seconds"] for v in summary.values())
    rows = []
    for name in sorted(summary, key=lambda n: -summary[n]["startup_seconds"]):
        entry = summary[name]
        rows.append([
            name,
            green("HW") if entry["hardware"] else "SW",
            "%.0f ms" % (entry["startup_seconds"] * 1000.0),
            bar(entry["startup_seconds"], peak, 16),
            grey("median of %d point%s" % (entry["points"],
                                           "" if entry["points"] == 1 else "s")),
        ])
    table(["ENCODER", "TYPE", "STARTUP", "", ""], rows,
          aligns=["left", "left", "right", "left", "left"], out=out)
    out.write("\n  %s\n" % grey(
        "matters when something spawns ffmpeg once per file; it is not part of "
        "the latency figures above, which cancel it rather than estimate it."))


def render_quality(records, out=None):
    out = out or sys.stdout
    if not records:
        return
    section("Quality per bitrate", out)
    out.write("  %s\n\n" % grey(
        "scored against the source clip; higher VMAF/SSIM/PSNR is better. "
        "This is where a fast encoder can turn out to be a worse one."))

    have_vmaf = any(r.get("vmaf") is not None for r in records)
    rows = []
    ordered = sorted(records, key=lambda r: (r["codec"], r["encoder"],
                                             r.get("bitrate") or 0))
    peak = max((r.get("vmaf") or r.get("ssim") or 0) for r in records) or 1
    last = None
    for r in ordered:
        name = r["encoder"]
        actual = util.human_rate(r.get("achieved_bitrate"))
        if r.get("bitrate_error_pct") is not None and abs(r["bitrate_error_pct"]) > 25:
            actual = yellow(actual)
        row = [name if name != last else "",
               util.human_rate(r.get("bitrate")), actual]
        if have_vmaf:
            row.append(util.fmt_num(r.get("vmaf"), 1))
        row += [util.fmt_num(r.get("ssim"), 4),
                util.fmt_num(r.get("psnr_db"), 2),
                _fps_str(r.get("encode_fps")),
                bar(r.get("vmaf") or (r.get("ssim") or 0) * 100, peak
                    if have_vmaf else 100, 14)]
        rows.append(row)
        last = name
    off_target = [r for r in records
                  if r.get("bitrate_error_pct") is not None
                  and abs(r["bitrate_error_pct"]) > 25]
    headers = ["ENCODER", "TARGET", "ACTUAL"]
    aligns = ["left", "right", "right"]
    if have_vmaf:
        headers.append("VMAF")
        aligns.append("right")
    headers += ["SSIM", "PSNR dB", "FPS", ""]
    aligns += ["right", "right", "right", "left"]
    table(headers, rows, aligns=aligns, out=out)
    if off_target:
        names = sorted({r["encoder"] for r in off_target})
        out.write("\n  %s\n" % yellow(
            "%s did not hit the requested bitrate (their rate control would not "
            "accept a hard cap), so their rows are not rate-matched against the "
            "others and the quality columns are not directly comparable."
            % ", ".join(names)))


def render_warnings(results, skips, sysinfo, elapsed, out=None,
                    latency_records=None, coverage=None):
    out = out or sys.stdout
    notes = []

    # First, because it changes how everything below should be read: a run that
    # stopped early is not a measurement of this machine's full capability.
    if coverage and coverage.get("stopped_early"):
        planned = coverage.get("planned") or 0
        completed = coverage.get("completed") or 0
        notes.append((red("incomplete run"),
                      "%s. %d of %d planned tests ran; tables below show only "
                      "what was measured. --resume on the .jsonl continues it."
                      % (coverage["stopped_early"], completed, planned)))

    # Both halves of the same finding: encoders that were measured and came back
    # unusable, and encoders never attempted because a real throughput figure
    # already said what they would cost. Reporting only the first would make the
    # gate look like an omission.
    stalled = {r["encoder"] for r in (latency_records or [])
               if not r.get("ok") and "paced" in (r.get("error") or "")}
    stalled |= {g["encoder"] for g in ((coverage or {}).get("latency_gated") or [])}
    if stalled:
        notes.append((yellow("latency not measured"),
                      "%s could not be timed at every point: the input is paced "
                      "slowly enough for the encoder to be measurable, and for "
                      "these that would cost more wall time than the figure is "
                      "worth. A budget limit, not a property of the encoder."
                      % ", ".join(sorted(stalled))))

    decode_bound = sorted({r.case.encoder.name for r in results if r.decode_bound})
    if decode_bound:
        notes.append((yellow("decode-bound"),
                      "%s outran the source decoder; their real encode rate is "
                      "higher than reported. Re-run with --source-format raw on a "
                      "machine with more scratch space to remove the ceiling."
                      % ", ".join(decode_bound)))

    # Judge rate control only on runs long enough to have converged.
    misses = [r for r in results
              if r.bitrate_error_pct is not None and abs(r.bitrate_error_pct) > 25
              and converged(r)]
    if misses:
        worst = sorted(misses, key=lambda r: -abs(r.bitrate_error_pct))[:3]
        notes.append((yellow("bitrate accuracy"),
                      "%d encoder(s) missed the requested bitrate by more than "
                      "25%% on a converged run (worst: %s at %+.0f%%)"
                      % (len({r.case.encoder.name for r in misses}),
                         worst[0].case.encoder.name, worst[0].bitrate_error_pct)))

    throttled = [r for r in results if r.throttled]
    if throttled:
        notes.append((red("throttled"),
                      "%d test(s) saw the CPU clock fall sharply mid-run or the "
                      "sensor exceed 95 C - those results understate this machine"
                      % len(throttled)))

    temps = [r.temp_c for r in results if r.temp_c]
    if temps and max(temps) >= 85:
        notes.append((red("thermal"),
                      "peak sensor temperature %.0f C during the run - results may "
                      "be throttled and lower than this machine's true capability"
                      % max(temps)))

    gov = sysinfo.get("governor")
    if gov and gov in ("powersave", "conservative"):
        notes.append((yellow("cpu governor"),
                      "governor is '%s'; 'performance' would likely raise software "
                      "encoder scores" % gov))

    if sysinfo.get("cpu", {}).get("quota"):
        notes.append((yellow("cpu quota"),
                      "this container is limited to %.2f cores, so results reflect "
                      "the container, not the host"
                      % sysinfo["cpu"]["quota"]))

    if skips:
        by_reason = {}
        for s in skips:
            by_reason.setdefault(s.reason, []).append("%s@%s" % (s.encoder, s.res_key))
        for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))[:6]:
            notes.append((grey("skipped"), "%s - %s"
                          % (reason, ", ".join(sorted(set(items))[:6]))))

    if not notes:
        return
    section("Notes", out)
    for tag, text in notes:
        out.write("  %s  %s\n" % (tag, text))


# --------------------------------------------------------------------------
# comparing two runs
# --------------------------------------------------------------------------

def _result_key(record):
    return (record.get("encoder"), record.get("resolution"), record.get("fps"),
            record.get("complexity"), record.get("bitrate"), record.get("preset"))


def render_comparison(a, b, out=None):
    """Diff two saved runs: same machine before/after a change, or two machines."""
    out = out or sys.stdout

    def describe(payload):
        sysinfo = payload.get("system", {})
        return "%s  %s  (%s, ffmpeg %s)" % (
            sysinfo.get("hostname", "?"), payload.get("timestamp", "?"),
            sysinfo.get("cpu", {}).get("model", "?"),
            payload.get("ffmpeg", {}).get("version", "?"))

    section("Comparing runs", out)
    kv([("A", describe(a)), ("B", describe(b))], out=out)

    index_a = {_result_key(r): r for r in a.get("results", []) if r.get("encode_fps")}
    index_b = {_result_key(r): r for r in b.get("results", []) if r.get("encode_fps")}
    shared = sorted(set(index_a) & set(index_b), key=lambda k: (k[0] or "", k[1] or ""))

    if not shared:
        out.write("\n  %s\n" % yellow(
            "no directly comparable measurements - the two runs used different "
            "configurations"))
        _compare_encoder_sets(a, b, out)
        return

    # Aggregate per encoder so the table stays readable.
    by_encoder = {}
    for key in shared:
        name = key[0]
        ratio = index_b[key]["encode_fps"] / index_a[key]["encode_fps"]
        by_encoder.setdefault(name, []).append(ratio)

    subsection("  Throughput change, B relative to A", out)
    rows = []
    for name in sorted(by_encoder, key=lambda n: -_mean(by_encoder[n])):
        ratios = by_encoder[name]
        mean = _mean(ratios)
        delta = (mean - 1.0) * 100.0
        if delta > 3:
            cell = green("%+.1f%%" % delta)
        elif delta < -3:
            cell = red("%+.1f%%" % delta)
        else:
            cell = grey("%+.1f%%" % delta)
        rows.append([name, str(len(ratios)), cell,
                     "%.2fx" % min(ratios), "%.2fx" % max(ratios)])
    table(["ENCODER", "TESTS", "MEAN CHANGE", "WORST", "BEST"], rows,
          aligns=["left", "right", "right", "right", "right"], indent=4, out=out)

    _compare_encoder_sets(a, b, out)


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _compare_encoder_sets(a, b, out):
    names_a = {e["name"] for e in a.get("encoders", []) if e.get("ok")}
    names_b = {e["name"] for e in b.get("encoders", []) if e.get("ok")}
    gained = sorted(names_b - names_a)
    lost = sorted(names_a - names_b)
    if gained:
        out.write("\n  %s %s\n" % (green("gained:"), ", ".join(gained)))
    if lost:
        out.write("  %s %s\n" % (red("lost:  "), ", ".join(lost)))
