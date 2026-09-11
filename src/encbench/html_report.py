"""Self-contained HTML report.

No external CSS, fonts, scripts or images: the file must be openable from a USB
stick on a machine with no network. Charts are SVG generated here in Python.
Built from the saved JSON payload, so a report can be regenerated later from
results alone.
"""

from __future__ import annotations

import html
import os

from . import util

PALETTE = ["#3b82f6", "#ef4444", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899",
           "#14b8a6", "#f97316", "#6366f1", "#84cc16", "#06b6d4", "#a855f7"]

RES_ORDER = ["360p", "480p", "720p", "1080p", "1440p", "2160p"]


def _esc(text):
    return html.escape("" if text is None else str(text), quote=True)


def _color(index):
    return PALETTE[index % len(PALETTE)]


def _fmt(value, digits=1):
    if value is None:
        return "&mdash;"
    if isinstance(value, (int, float)):
        if value >= 1000:
            return "%.0f" % value
        return "%.*f" % (digits, value)
    return _esc(value)


# --------------------------------------------------------------------------
# charts
# --------------------------------------------------------------------------

def bar_chart(items, unit="fps", width=720, row_height=26):
    """items: list of (label, value, is_hardware). Horizontal bars."""
    items = [i for i in items if i[1]]
    if not items:
        return ""
    peak = max(i[1] for i in items)
    label_w = 150
    chart_w = width - label_w - 90
    height = row_height * len(items) + 12
    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="xMinYMin meet">' % (width, height)]
    for i, (label, value, is_hw) in enumerate(items):
        y = i * row_height + 6
        bar_w = max(2, int(chart_w * value / peak))
        color = "#10b981" if is_hw else _color(i)
        parts.append(
            '<text x="%d" y="%d" class="bar-label" text-anchor="end">%s</text>'
            % (label_w - 8, y + 13, _esc(label)))
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="3" fill="%s"/>'
                     % (label_w, y + 3, bar_w, row_height - 10, color))
        parts.append('<text x="%d" y="%d" class="bar-value">%s %s</text>'
                     % (label_w + bar_w + 8, y + 13, _fmt(value), _esc(unit)))
    parts.append("</svg>")
    return "".join(parts)


def line_chart(series, x_labels, y_label="fps", width=760, height=320):
    """series: list of (name, [values aligned to x_labels], is_hardware).

    Log-scaled Y, because encoder throughput across 360p..2160p spans orders of
    magnitude and a linear axis would flatten everything interesting.
    """
    import math

    points = [v for _, values, _ in series for v in values if v]
    if not points or len(x_labels) < 2:
        return ""
    lo, hi = min(points), max(points)
    lo = max(lo * 0.8, 0.1)
    hi = hi * 1.25
    log_lo, log_hi = math.log10(lo), math.log10(hi)
    span = (log_hi - log_lo) or 1.0

    pad_l, pad_r, pad_t, pad_b = 56, 130, 16, 40
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def px(i):
        return pad_l + (plot_w * i / float(len(x_labels) - 1))

    def py(value):
        return pad_t + plot_h - plot_h * ((math.log10(value) - log_lo) / span)

    parts = ['<svg class="chart" viewBox="0 0 %d %d" role="img" '
             'preserveAspectRatio="xMinYMin meet">' % (width, height)]

    # gridlines at decade-ish steps
    tick = 1
    while tick < hi * 10:
        if lo <= tick <= hi:
            y = py(tick)
            parts.append('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" class="grid"/>'
                         % (pad_l, y, pad_l + plot_w, y))
            parts.append('<text x="%d" y="%.1f" class="axis" text-anchor="end">%s</text>'
                         % (pad_l - 8, y + 4, tick if tick < 1000 else "%dk" % (tick // 1000)))
        tick *= 10

    for i, label in enumerate(x_labels):
        parts.append('<text x="%.1f" y="%d" class="axis" text-anchor="middle">%s</text>'
                     % (px(i), height - 14, _esc(label)))

    # realtime reference is implicit in the data; draw series
    for idx, (name, values, is_hw) in enumerate(series):
        color = _color(idx)
        coords = [(px(i), py(v)) for i, v in enumerate(values) if v]
        if len(coords) < 1:
            continue
        if len(coords) > 1:
            path = "M" + " L".join("%.1f %.1f" % c for c in coords)
            parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" '
                         'stroke-linejoin="round"%s/>'
                         % (path, color, ' stroke-dasharray="5 3"' if is_hw else ""))
        for c in coords:
            parts.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (c[0], c[1], color))
        last = coords[-1]
        parts.append('<text x="%.1f" y="%.1f" class="legend" fill="%s">%s</text>'
                     % (last[0] + 8, last[1] + 4, color, _esc(name)))

    parts.append('<text x="10" y="%d" class="axis">%s</text>' % (pad_t + 4, _esc(y_label)))
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def _table(headers, rows, classes=""):
    out = ['<div class="table-wrap"><table class="%s">' % classes, "<thead><tr>"]
    for h in headers:
        out.append("<th>%s</th>" % _esc(h))
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for cell in row:
            out.append("<td>%s</td>" % cell)
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _throughput(payload):
    base = payload.get("baseline") or {}
    res, fps = base.get("resolution"), base.get("fps")
    cx = base.get("complexity")
    rows = [r for r in payload.get("results", [])
            if r.get("resolution") == res and r.get("fps") == fps
            and r.get("complexity") == cx and r.get("kind") == "throughput"
            and r.get("encode_fps")]
    best = {}
    for r in rows:
        name = r["encoder"]
        if name not in best or r["encode_fps"] > best[name]["encode_fps"]:
            best[name] = r
    return sorted(best.values(), key=lambda r: -r["encode_fps"]), res, fps


def _resolution_series(payload):
    base = payload.get("baseline") or {}
    fps, cx = base.get("fps"), base.get("complexity")
    rows = [r for r in payload.get("results", [])
            if r.get("fps") == fps and r.get("complexity") == cx
            and r.get("kind") == "throughput" and r.get("encode_fps")]
    keys = [k for k in RES_ORDER if any(r["resolution"] == k for r in rows)]
    # Restrict to the resolution sweep's own preset per encoder, so the
    # baseline resolution is not represented by a preset-sweep row instead.
    baseline_presets = payload.get("baseline_presets") or {}
    by_encoder = {}
    missing = object()
    for r in rows:
        # A baseline preset of None is a real value (hardware encoders anchored
        # on the driver default), not an absent one -- testing "is not None"
        # disabled the filter for exactly those encoders and let preset- and
        # bitrate-sweep rows overwrite the resolution curve.
        expected = baseline_presets.get(r["encoder"], missing)
        if expected is not missing and r.get("preset") != expected:
            continue
        by_encoder.setdefault(r["encoder"], {})
        by_encoder[r["encoder"]][r["resolution"]] = r["encode_fps"]
    hw = {e["name"]: e.get("hardware") for e in payload.get("encoders", [])}
    series = []
    for name in sorted(by_encoder):
        values = [by_encoder[name].get(k) for k in keys]
        if any(values):
            series.append((name, values, bool(hw.get(name))))
    return series, keys


def build_html(payload):
    sysinfo = payload.get("system", {})
    cpu = sysinfo.get("cpu", {})
    ff = payload.get("ffmpeg", {})

    parts = []
    a = parts.append

    a('<title>Encoder Benchmark &mdash; %s</title>' % _esc(sysinfo.get("hostname", "system")))
    a(_STYLE)

    a('<header><h1>Video Encoder Benchmark</h1>')
    a('<p class="sub">%s &middot; %s &middot; profile <b>%s</b> &middot; %s</p></header>'
      % (_esc(sysinfo.get("hostname")), _esc(payload.get("timestamp")),
         _esc(payload.get("profile")),
         _esc(util.human_time(payload.get("duration_seconds") or 0))))

    # ---- headline ----
    rows, base_res, base_fps = _throughput(payload)
    if rows:
        fastest = rows[0]
        hw_rows = [r for r in rows if r.get("hardware")]
        preset_note = ("" if fastest.get("preset") is None
                       else " at %s" % fastest["preset"])
        cards = [("Fastest encoder", fastest["encoder"],
                  "%s fps%s, %s%s (%sx realtime)"
                  % (_fmt(fastest["encode_fps"]), preset_note, base_res,
                     base_fps, _fmt(fastest.get("realtime_x"))))]
        if hw_rows:
            cards.append(("Fastest hardware", hw_rows[0]["encoder"],
                          "%s fps (%sx realtime)"
                          % (_fmt(hw_rows[0]["encode_fps"]),
                             _fmt(hw_rows[0].get("realtime_x")))))
        conc = payload.get("concurrency") or {}
        if conc:
            best_name, best_n = None, 0
            for name, entry in conc.items():
                n = max([s["streams"] for s in entry.get("steps", [])
                         if s.get("all_realtime")] or [0])
                if n > best_n:
                    best_name, best_n = name, n
            if best_name:
                cards.append(("Most parallel streams", best_name,
                              "%d simultaneous %s%s streams" % (best_n, base_res, base_fps)))
        a('<section><div class="cards">')
        for title, value, note in cards:
            a('<div class="card"><div class="card-title">%s</div>'
              '<div class="card-value">%s</div><div class="card-note">%s</div></div>'
              % (_esc(title), _esc(value), _esc(note)))
        a('</div></section>')

        a('<section><h2>Throughput at %s%s</h2>' % (_esc(base_res), _esc(base_fps)))
        a('<p class="note">Each encoder at its fastest sampled preset &mdash; peak '
          'capability, not equal quality. Green bars are hardware encoders.</p>')
        a(bar_chart([(r["encoder"], r["encode_fps"], r.get("hardware")) for r in rows]))
        a('</section>')

    # ---- resolution scaling ----
    series, keys = _resolution_series(payload)
    if series and len(keys) > 1:
        a('<section><h2>Throughput by resolution</h2>')
        a('<p class="note">Log scale. Dashed lines are hardware encoders.</p>')
        a(line_chart(series, keys))
        headers = ["Encoder"] + [k for k in keys]
        trows = []
        for name, values, is_hw in series:
            cells = ['<b>%s</b>%s' % (_esc(name),
                                      ' <span class="tag hw">HW</span>' if is_hw else "")]
            cells += ['%s' % _fmt(v) for v in values]
            trows.append(cells)
        a(_table(headers, trows))
        a('</section>')

    # ---- concurrency ----
    conc = payload.get("concurrency") or {}
    if conc:
        a('<section><h2>Concurrent stream capacity</h2>')
        a('<p class="note">Most simultaneous streams where <em>every</em> stream '
          'held the target framerate.</p>')
        items = []
        trows = []
        for name in sorted(conc):
            steps = conc[name].get("steps", [])
            realtime = max([s["streams"] for s in steps if s.get("all_realtime")] or [0])
            agg = max([s.get("aggregate_fps") or 0 for s in steps] or [0])
            hw = conc[name].get("case", {}).get("hardware")
            items.append((name, realtime, hw))
            limit = "&mdash;"
            limited = [s for s in steps if s.get("session_limited")]
            last = steps[-1] if steps else None
            if limited:
                limit = '<span class="bad">hardware session limit at %d</span>' % limited[0]["streams"]
            elif last and last.get("failed_streams"):
                limit = ('<span class="bad">%d of %d streams failed &mdash; not a '
                         'capacity limit</span>'
                         % (last["failed_streams"], last["streams"]))
            elif last and last.get("at_ceiling"):
                limit = "hit ramp ceiling %d" % last["streams"]
            elif last and not last.get("all_realtime"):
                limit = "saturated at %d" % last["streams"]
            trows.append(['<b>%s</b>' % _esc(name), str(realtime), _fmt(agg), limit])
        a(bar_chart(items, unit="streams"))
        a(_table(["Encoder", "Realtime streams", "Peak aggregate fps", "Limit"], trows))
        a('</section>')

    # ---- latency ----
    latency = payload.get("latency") or []
    ok_latency = [r for r in latency if r.get("ok")]
    if ok_latency:
        a('<section><h2>Encode latency</h2>')
        a('<p class="note">Input paced at realtime. <b>Delay</b> is how many '
          'frames the encoder holds before its first packet emerges &mdash; '
          'lookahead plus frame reordering. Shorter is better; this, not '
          'throughput, decides whether a box can run live.</p>')
        head = [r for r in ok_latency
                if r.get("mode") == "default" and r.get("axis") == "baseline"]
        if head:
            a(bar_chart([(r["encoder"], r.get("delay_ms") or 0, r.get("hardware"))
                         for r in sorted(head, key=lambda x: x.get("delay_ms") or 0)],
                        unit="ms delay"))
        trows = []
        for r in sorted(latency, key=lambda x: (x.get("encoder") or "",
                                                x.get("axis") or "",
                                                x.get("resolution") or "")):
            point = "%s%s%s" % (r.get("resolution"), r.get("fps"),
                                "" if r.get("preset") is None
                                else " " + str(r["preset"]))
            mode = "low-latency" if r.get("mode") == "lowlat" else "default"
            if not r.get("ok"):
                trows.append(['<b>%s</b>' % _esc(r.get("encoder")), _esc(point),
                              _esc(mode),
                              '<span class="note">%s</span>'
                              % _esc(r.get("error") or "not measured"),
                              "&mdash;", "&mdash;", "&mdash;"])
                continue
            trows.append(['<b>%s</b>' % _esc(r.get("encoder")), _esc(point),
                          _esc(mode), _fmt(r.get("delay_frames")),
                          _fmt(r.get("delay_ms"), 0),
                          "+" + _fmt(r.get("worst_excursion_frames")),
                          _fmt(r.get("frame_time_ms"), 2)])
        a(_table(["Encoder", "Point", "Mode", "Delay (frames)", "Delay (ms)",
                  "Worst excursion (frames)", "Frame time (ms)"], trows))
        a('</section>')

    # ---- startup cost ----
    startup = payload.get("startup") or []
    if startup:
        from .latency import startup_by_encoder
        summary = startup_by_encoder(startup)
        if summary:
            a('<section><h2>Startup cost</h2>')
            a('<p class="note">Fixed overhead of one ffmpeg invocation &mdash; '
              'process start, hardware device init, filter setup and teardown. '
              'It matters when something spawns ffmpeg once per file. Derived '
              'from runs the benchmark already made, at no extra cost.</p>')
            a(bar_chart([(name, entry["startup_seconds"] * 1000.0,
                          entry["hardware"])
                         for name, entry in sorted(
                             summary.items(),
                             key=lambda kv: -kv[1]["startup_seconds"])],
                        unit="ms"))
            a('</section>')

    # ---- quality ----
    quality = payload.get("quality") or []
    if quality:
        have_vmaf = any(q.get("vmaf") is not None for q in quality)
        a('<section><h2>Quality per bitrate</h2>')
        a('<p class="note">Scored against the source clip. Higher is better. '
          'This is where a much faster encoder can turn out to be a worse one.</p>')
        headers = ["Encoder", "Target", "Actual"]
        if have_vmaf:
            headers.append("VMAF")
        headers += ["SSIM", "PSNR dB", "fps"]
        trows = []
        for q in sorted(quality, key=lambda x: (x.get("codec", ""), x.get("encoder", ""),
                                                x.get("bitrate") or 0)):
            row = ['<b>%s</b>' % _esc(q.get("encoder")),
                   _esc(util.human_rate(q.get("bitrate"))),
                   _esc(util.human_rate(q.get("achieved_bitrate")))]
            if have_vmaf:
                row.append(_fmt(q.get("vmaf")))
            row += [_fmt(q.get("ssim"), 4), _fmt(q.get("psnr_db"), 2),
                    _fmt(q.get("encode_fps"))]
            trows.append(row)
        a(_table(headers, trows))
        a('</section>')

    # ---- coverage ----
    # What the plan asked for against what ran. A sweep that did not run must be
    # visible as an absence, not simply missing from the page.
    coverage = payload.get("coverage") or {}
    axes = coverage.get("axes") or {}
    partial = {name: e for name, e in axes.items()
               if (e.get("ran") or 0) < (e.get("planned") or 0)}
    lat = coverage.get("latency") or {}
    lat_partial = (lat.get("ran") or 0) < (lat.get("planned") or 0)
    light = coverage.get("light") or []
    lat_gated = coverage.get("latency_gated") or []
    stopped = coverage.get("stopped_early")
    if stopped or lat_partial or light or lat_gated:
        a('<section><h2>Coverage</h2>')
        a('<div class="method"><p>What the plan asked for against what ran. '
          'Points listed as not measured were either skipped deliberately '
          '&mdash; below the rate at which the measurement can say anything '
          '&mdash; or left unrun; the reasons are individually recorded.</p></div>')
        if stopped:
            a('<div class="hint"><div class="hint-title">Incomplete run</div>'
              '<div class="hint-line">%s. %s of %s planned tests ran.</div></div>'
              % (_esc(stopped), _esc(coverage.get("completed")),
                 _esc(coverage.get("planned"))))
        trows = []
        if stopped:
            # Axis shortfalls on a completed run are explained by the skip list;
            # only a run that stopped short needs them called out per sweep.
            for name in sorted(partial):
                entry = partial[name]
                trows.append([_esc(name), _esc(entry.get("ran")),
                              _esc(entry.get("planned"))])
        if lat_partial:
            trows.append(["latency", _esc(lat.get("ran")), _esc(lat.get("planned"))])
        if trows:
            a(_table(["Sweep", "Measured", "Planned"], trows))
        for entry in light:
            a('<div class="hint-line"><b>%s</b> &mdash; %s</div>'
              % (_esc(entry.get("encoder")), _esc(entry.get("reason"))))
        # One line per encoder rather than per point: the same finding repeats
        # across the resolution and preset sweeps.
        gated = {}
        for entry in lat_gated:
            gated.setdefault(entry.get("encoder"), entry)
        for name in sorted(gated):
            entry = gated[name]
            rate = entry.get("measured_fps")
            # Name the preset: the rate that decided this is that preset's, and
            # the encoder's fastest preset is a much larger and unrelated number.
            preset = entry.get("preset")
            detail = "" if rate is None else " (%.1f fps at %s%s%s)" % (
                rate, entry.get("resolution"), entry.get("fps"),
                "" if preset in (None, "") else " %s" % preset)
            a('<div class="hint-line"><b>%s</b> &mdash; latency not measured: '
              '%s%s</div>'
              % (_esc(name), _esc(entry.get("reason")), _esc(detail)))
        a('</section>')

    # ---- encoders ----
    encoders = payload.get("encoders", [])
    usable = [e for e in encoders if e.get("ok")]
    if usable:
        a('<section><h2>Encoders available on this system</h2>')
        trows = []
        for e in sorted(usable, key=lambda x: (x.get("codec"), x.get("name"))):
            knob = "&mdash;"
            if e.get("speed_knob") and e.get("speed_values"):
                knob = "%s: %s" % (_esc(e["speed_knob"]),
                                   _esc(", ".join(e["speed_values"][:5])))
            trows.append(['<b>%s</b>' % _esc(e["name"]),
                          '<span class="tag hw">HW</span>' if e.get("hardware") else "SW",
                          _esc(e.get("codec")),
                          _esc(e.get("device") or e.get("family") or ""),
                          knob])
        a(_table(["Encoder", "Type", "Codec", "Device", "Speed knob"], trows))
        a('</section>')

    # ---- diagnostics ----
    hints = payload.get("diagnostics") or []
    actionable = [h for h in hints if h.get("severity") == "actionable"]
    if actionable:
        a('<section><h2>What this system could also do</h2>')
        for h in actionable:
            a('<div class="hint"><div class="hint-title">%s</div>' % _esc(h["title"]))
            for line in h.get("lines", []):
                a('<div class="hint-line">%s</div>' % _esc(line))
            a('<div class="hint-affects">affects: %s</div></div>'
              % _esc(", ".join(h.get("encoders", []))))
        a('</section>')

    # ---- methodology ----
    a('<section><h2>How these numbers were produced</h2><div class="method">')
    a('<p>Every encoder listed was verified by running a real encode on this '
      'machine, not by trusting the ffmpeg build\'s feature list. Each timed run '
      'is preceded by a short calibration pass so the measurement lasts a '
      'consistent amount of wall time regardless of encoder speed, and GOP length '
      'is pinned to twice the framerate so encoders with different defaults are '
      'compared on equal terms.</p>')
    a('<p>Throughput is frames encoded divided by ffmpeg\'s own reported run time; '
      'output is muxed to <code>/dev/null</code> so storage speed does not enter '
      'the measurement. Bitrate accuracy is only reported for runs of at least 10 '
      'seconds of encoded video, below which one-pass rate control has not '
      'converged.</p>')
    src = payload.get("sources") or []
    if src:
        a('<p>Test footage was generated locally from deterministic filter graphs '
          'at %s, stored %s, so the same content is reproducible on any machine.</p>'
          % (_esc(", ".join(sorted({s.get("resolution") for s in src if s.get("resolution")}))),
             _esc(src[0].get("format", "raw"))))
    a('</div></section>')

    # ---- system ----
    a('<section><h2>System</h2>')
    gpu_lines = []
    for g in sysinfo.get("gpus", []):
        bits = [g.get("name") or g.get("vendor") or "unknown"]
        if g.get("render_node"):
            bits.append(g["render_node"])
        if g.get("driver"):
            bits.append("driver %s" % g["driver"])
        gpu_lines.append(" &middot; ".join(_esc(b) for b in bits))
    pairs = [
        ("Host", _esc(sysinfo.get("hostname"))),
        ("Distro", "%s (kernel %s, %s)" % (_esc(sysinfo.get("distro")),
                                           _esc(sysinfo.get("kernel")),
                                           _esc(sysinfo.get("arch")))),
        ("CPU", "%s &mdash; %s physical / %s logical cores"
         % (_esc(cpu.get("model")), _esc(cpu.get("physical")), _esc(cpu.get("logical")))),
        ("Memory", _esc(util.human_bytes(sysinfo.get("memory_bytes")))),
        ("GPU", "<br>".join(gpu_lines) if gpu_lines else "none detected"),
        ("Governor", _esc(sysinfo.get("governor") or "unknown")),
        ("ffmpeg", "%s <span class='dim'>(%s)</span>"
         % (_esc(ff.get("version")), _esc(ff.get("origin")))),
    ]
    if sysinfo.get("container"):
        pairs.append(("Container", _esc(sysinfo["container"])))
    a(_table(["Property", "Value"], [[ '<b>%s</b>' % k, v] for k, v in pairs]))
    a('</section>')

    a('<footer>Generated by encbench %s &middot; results JSON alongside this file'
      '</footer>' % _esc(payload.get("encbench_version", "")))
    return "\n".join(parts)


def write_html(path, payload):
    content = build_html(payload)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("<!doctype html>\n<html lang=\"en\">\n<head>\n"
                 "<meta charset=\"utf-8\">\n"
                 "<meta name=\"viewport\" content=\"width=device-width, "
                 "initial-scale=1\">\n")
        fh.write(content)
        fh.write("\n</body>\n</html>\n")
    os.replace(tmp, path)
    return path


_STYLE = """<style>
:root{--bg:#ffffff;--fg:#111827;--muted:#6b7280;--line:#e5e7eb;--card:#f9fafb;
--accent:#2563eb;--good:#059669;--bad:#dc2626;--code:#f3f4f6;}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--fg:#e5e7eb;
--muted:#9ca3af;--line:#252a33;--card:#161a21;--accent:#60a5fa;--good:#34d399;
--bad:#f87171;--code:#1c212a;}}
*{box-sizing:border-box}
body{margin:0;padding:0 20px 60px;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
header{max-width:900px;margin:0 auto;padding:36px 0 8px;}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}
h2{font-size:17px;margin:0 0 14px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0;font-size:14px}
section{max-width:900px;margin:0 auto;padding:26px 0;border-top:1px solid var(--line)}
.note{color:var(--muted);font-size:13.5px;margin:-6px 0 14px}
.cards{display:flex;flex-wrap:wrap;gap:12px}
.card{flex:1 1 220px;background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px}
.card-title{color:var(--muted);font-size:12px;text-transform:uppercase;
letter-spacing:.06em}
.card-value{font-size:20px;font-weight:650;margin:4px 0 2px;word-break:break-word}
.card-note{color:var(--muted);font-size:13px}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:14px;min-width:480px}
th,td{text-align:left;padding:7px 12px 7px 0;border-bottom:1px solid var(--line);
white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
letter-spacing:.05em}
td:not(:first-child),th:not(:first-child){text-align:right}
.tag{font-size:10.5px;padding:1px 6px;border-radius:99px;vertical-align:middle}
.tag.hw{background:var(--good);color:#fff}
.chart{width:100%;height:auto;display:block;margin:4px 0 16px;overflow:visible}
.bar-label{font-size:12px;fill:var(--fg)}
.bar-value{font-size:12px;fill:var(--muted)}
.axis{font-size:11px;fill:var(--muted)}
.legend{font-size:11.5px;font-weight:600}
.grid{stroke:var(--line);stroke-width:1}
.hint{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
border-radius:8px;padding:12px 14px;margin-bottom:12px}
.hint-title{font-weight:650;margin-bottom:5px}
.hint-line{color:var(--muted);font-size:13.5px}
.hint-affects{color:var(--muted);font-size:12px;margin-top:6px;font-style:italic}
.method p{color:var(--muted);font-size:13.5px;margin:0 0 10px;max-width:70ch}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:12.5px}
.dim{color:var(--muted)}
.bad{color:var(--bad)}
footer{max-width:900px;margin:0 auto;padding:24px 0;color:var(--muted);
font-size:12.5px;border-top:1px solid var(--line)}
</style>
</head>
<body>"""
