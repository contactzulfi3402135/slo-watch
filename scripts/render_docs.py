#!/usr/bin/env python3
"""Builds the images in docs/ from a database and a transcript.

A script rather than a screenshot so the README numbers stay traceable to a run and can be
rebuilt when the code moves.

    python scripts/render_docs.py --db demo.db --out docs --transcript transcript.txt

Pillow is needed here and nowhere else; the monitor itself is Flask and PyYAML.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
SANS = FONT_DIR / "DejaVuSans.ttf"
SANS_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
MONO = FONT_DIR / "DejaVuSansMono.ttf"

BG = (16, 20, 24)
PANEL = (23, 28, 34)
LINE = (36, 44, 53)
TEXT = (215, 222, 231)
DIM = (125, 139, 154)
OK = (67, 181, 129)
WARN = (224, 163, 65)
BAD = (217, 86, 79)
ACCENT = (90, 169, 230)


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), BG)
    return image, ImageDraw.Draw(image)


# --------------------------------------------------------------------- architecture

def box(draw, x, y, w, h, title, lines=(), accent=LINE, title_font=None, body_font=None):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=7, fill=PANEL, outline=accent, width=2)
    draw.text((x + 16, y + 12), title, fill=TEXT, font=title_font or font(SANS_BOLD, 16))
    for i, line in enumerate(lines):
        draw.text((x + 16, y + 38 + i * 19), line, fill=DIM, font=body_font or font(SANS, 13))


def arrow(draw, x1, y1, x2, y2, color=DIM, width=2, head=8):
    draw.line([x1, y1, x2, y2], fill=color, width=width)
    if x1 == x2:                                    # vertical
        direction = 1 if y2 > y1 else -1
        draw.polygon([(x2, y2), (x2 - head // 2, y2 - direction * head),
                      (x2 + head // 2, y2 - direction * head)], fill=color)
    else:                                           # horizontal
        direction = 1 if x2 > x1 else -1
        draw.polygon([(x2, y2), (x2 - direction * head, y2 - head // 2),
                      (x2 - direction * head, y2 + head // 2)], fill=color)


def render_architecture(out: Path) -> None:
    image, draw = canvas(1480, 720)
    title, small, body = font(SANS_BOLD, 22), font(SANS, 13), font(SANS, 14)
    tiny = font(SANS, 12)
    box_title = font(SANS_BOLD, 15)

    draw.text((48, 34), "slo-watch: one process, one SQLite file, nothing else to run",
              fill=TEXT, font=title)
    draw.text((48, 68), "config.yml is validated at startup; a probe result is written once and "
                        "then read by the API, the report and the evaluator",
              fill=DIM, font=small)

    h = 118
    row1, row2, row3 = 130, 300, 470

    box(draw, 48, row1, 210, h, "config.yml",
        ["services, probes, SLOs,", "alert rules, notifiers.", "Validated eagerly: the", "error names the key."],
        accent=ACCENT, title_font=box_title, body_font=tiny)
    box(draw, 316, row1, 250, h, "Scheduler",
        ["three threads: probe,", "evaluate, prune. Each", "loop catches its own", "exception."],
        accent=ACCENT, title_font=box_title, body_font=tiny)
    box(draw, 624, row1, 236, h, "Probe",
        ["http (urllib) or shell,", "timeout on each.", "A failure comes back as", "a value, not an error."],
        accent=ACCENT, title_font=box_title, body_font=tiny)
    box(draw, 918, row1, 230, h, "store.py · SQLite",
        ["probe_results, alerts.", "WAL, one writer + lock,", "indexed on (service, ts),", "pruned by retention."],
        accent=OK, title_font=box_title, body_font=tiny)

    for x1, x2 in ((258, 316), (566, 624), (860, 918)):
        arrow(draw, x1, row1 + h // 2, x2, row1 + h // 2)

    box(draw, 918, row2, 230, h, "sli.py",
        ["burn = bad/total ÷ budget.", "below min_events it returns", "None, so an empty window", "never reads as 0.0x."],
        accent=WARN, title_font=box_title, body_font=tiny)
    box(draw, 624, row2, 236, h, "alerts.py · Evaluator",
        ["both windows must exceed", "to fire, either one", "recovering resolves,", "renotify on an interval."],
        accent=WARN, title_font=box_title, body_font=tiny)
    box(draw, 316, row2, 250, h, "Notifiers",
        ["log, webhook (POST JSON,", "one retry), recording.", "A broken notifier gets", "logged, not raised."],
        accent=WARN, title_font=box_title, body_font=tiny)

    arrow(draw, 998, row1 + h, 998, row2, color=LINE)
    arrow(draw, 918, row2 + h // 2, 860, row2 + h // 2, color=LINE)
    arrow(draw, 624, row2 + h // 2, 566, row2 + h // 2, color=WARN)

    box(draw, 918, row3, 230, h, "Monitor",
        ["the one object both front", "ends call: probe, evaluate,", "prune, slo_table. A manual", "probe is not a 2nd path."],
        accent=LINE, title_font=box_title, body_font=tiny)
    box(draw, 624, row3, 236, h, "HTTP API · Flask",
        ["/ · dashboard, /api/summary,", "/api/alerts, manual probe,", "/metrics for Prometheus,", "/healthz for liveness."],
        accent=ACCENT, title_font=box_title, body_font=tiny)
    box(draw, 316, row3, 250, h, "CLI",
        ["init · probe · report ·", "eval · serve. `probe` exits", "1 on failure, so cron can", "page on the exit code."],
        accent=ACCENT, title_font=box_title, body_font=tiny)

    arrow(draw, 860, row3 + 34, 918, row3 + 34, color=ACCENT)
    # The CLI calls the same Monitor, routed underneath the API box so the two paths stay
    # visually distinct without implying one calls the other.
    elbow = [(441, row3 + h), (441, row3 + h + 26), (1033, row3 + h + 26), (1033, row3 + h)]
    draw.line(elbow, fill=ACCENT, width=2)
    draw.polygon([(1033, row3 + h), (1029, row3 + h + 9), (1037, row3 + h + 9)], fill=ACCENT)

    # the monitor reads what the write path wrote
    read_back = [(1148, row3 + 20), (1178, row3 + 20), (1178, row1 + 78), (1148, row1 + 78)]
    draw.line(read_back, fill=OK, width=2)
    draw.polygon([(1148, row1 + 78), (1157, row1 + 74), (1157, row1 + 82)], fill=OK)
    draw.text((1188, row3 - 66), "reads the", fill=OK, font=tiny)
    draw.text((1188, row3 - 50), "same rows", fill=OK, font=tiny)

    draw.text((48, 660), "one file holds all of it: the dashboard, the report and the "
                         "evaluator all read the rows the probe loop wrote",
              fill=DIM, font=body)
    draw.text((48, 684), "alert state lives there too, so restarting re-sends nothing it "
                         "already sent",
              fill=DIM, font=body)
    image.save(out / "architecture.png", optimize=True)


# --------------------------------------------------------------------- burn chart

def _buckets(db: Path, bucket_seconds: int, objective: float, slo: str = "availability"):
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, SUM(ok) AS good, COUNT(*) AS total "
        "FROM probe_results GROUP BY bucket ORDER BY bucket",
        (bucket_seconds, bucket_seconds),
    ).fetchall()
    # Only this SLO's alerts: a latency alert firing at the same moment would otherwise
    # paint a band on a chart about availability.
    alerts = conn.execute(
        "SELECT rule, state, started_at, resolved_at, severity FROM alerts "
        "WHERE slo = ? ORDER BY started_at", (slo,)
    ).fetchall()
    conn.close()
    allowed_bad = 1.0 - objective / 100.0
    series = []
    for bucket, good, total in rows:
        if total == 0:
            continue
        series.append((float(bucket), (total - good) / total / allowed_bad, total, total - good))
    return series, alerts


def render_burn_chart(db: Path, out: Path, objective: float = 99.0) -> None:
    series, alerts = _buckets(db, 15, objective)
    if not series:
        raise SystemExit("no probe rows in the database; run a demo first")

    width, height = 1400, 620
    image, draw = canvas(width, height)
    heading = font(SANS_BOLD, 22)
    small = font(SANS, 13)
    tiny = font(SANS, 12)
    label = font(SANS_BOLD, 12)

    left, right, top, bottom = 92, width - 56, 132, height - 96
    plot_w, plot_h = right - left, bottom - top

    t0, t1 = series[0][0], series[-1][0] + 15
    peak = max(b for _, b, _, _ in series)
    ymax = max(8.0, math.ceil(peak))
    ymax = min(ymax, 100.0)

    def sx(t: float) -> float:
        return left + (t - t0) / max(t1 - t0, 1) * plot_w

    def sy(v: float) -> float:
        # log1p: a 40x spike and a 2x threshold have to be legible on the same axis. The
        # floor is raised a few pixels so a healthy 0x line does not hide inside the axis.
        return bottom - 6 - math.log1p(max(v, 0.0)) / math.log1p(ymax) * plot_h

    draw.text((left, 40), "Availability burn rate, 15s buckets, 99.0% objective",
              fill=TEXT, font=heading)
    draw.text((left, 72), "burn 1x spends the error budget exactly at the end of the window; "
                          "5x spends it in a fifth of it",
              fill=DIM, font=small)

    # firing bands first, so everything else draws on top
    for rule, state, started, resolved, severity in alerts:
        band_end = resolved if resolved else t1
        color = (62, 27, 27) if rule == "fast-burn" else (58, 45, 22)
        draw.rectangle([sx(started), top, sx(min(band_end, t1)), bottom], fill=color)

    # grid + y ticks
    for tick in (0, 1, 2, 5, 10, 20, 50, 100):
        if tick > ymax:
            continue
        y = sy(tick)
        draw.line([left, y, right, y], fill=LINE)
        draw.text((left - 14, y - 7), f"{tick}x", fill=DIM, font=tiny, anchor="ra")

    # thresholds
    for value, text, color in ((5, "fast-burn threshold 5x", BAD), (2, "slow-burn threshold 2x", WARN)):
        y = sy(value)
        for x in range(left, right, 14):
            draw.line([x, y, x + 7, y], fill=color, width=2)
        draw.text((right - 6, y - 18), text, fill=color, font=label, anchor="ra")

    # the series itself
    points = [(sx(t), sy(v)) for t, v, _, _ in series]
    draw.line(points, fill=ACCENT, width=2, joint="curve")
    for (x, y), (_, value, total, bad) in zip(points, series):
        if value >= 5:
            draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=BAD)

    # x axis
    draw.line([left, bottom, right, bottom], fill=LINE)
    span = int(t1 - t0)
    step = 60 if span > 360 else 30
    for offset in range(0, span + 1, step):
        x = sx(t0 + offset)
        draw.line([x, bottom, x, bottom + 5], fill=LINE)
        draw.text((x, bottom + 12), f"{offset // 60}:{offset % 60:02d}", fill=DIM, font=tiny, anchor="ma")
    draw.text((right, bottom + 34), "elapsed since first probe (m:ss)", fill=DIM, font=tiny, anchor="ra")

    # annotations at the state changes, staggered so two rules firing together stay legible
    firing_slot = resolved_slot = 0
    for rule, state, started, resolved, severity in alerts:
        tint = BAD if rule == "fast-burn" else WARN
        x = sx(started)
        draw.line([x, top, x, bottom], fill=tint, width=1)
        draw.text((x + 6, top + 8 + firing_slot * 22), f"{rule} fired", fill=tint, font=label)
        firing_slot += 1
        if resolved is None:
            continue
        xr = sx(resolved)
        draw.line([xr, top, xr, bottom], fill=OK, width=1)
        draw.text((xr + 6, top + 84 + resolved_slot * 22), f"{rule} resolved", fill=OK, font=label)
        resolved_slot += 1

    worst = max(series, key=lambda row: row[1])
    draw.text((left, bottom + 58),
              f"{sum(r[2] for r in series)} probes over {span // 60} minutes · "
              f"peak {worst[1]:.1f}x in a 15s bucket ({worst[3]} bad of {worst[2]}) · "
              f"red dots mark buckets above the page threshold",
              fill=DIM, font=small)

    image.save(out / "burn-rate.png", optimize=True)


# --------------------------------------------------------------------- transcript

def render_transcript(path: Path, out: Path) -> None:
    lines = path.read_text().rstrip("\n").splitlines()
    body = font(MONO, 15)
    leading = 24
    pad = 28
    width = 1180
    height = pad * 2 + 44 + leading * len(lines)

    image, draw = canvas(width, height)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=8, fill=(13, 16, 20), outline=LINE, width=2)
    draw.rounded_rectangle([0, 0, width - 1, 40], radius=8, fill=PANEL, outline=LINE, width=2)
    for i, color in enumerate(((217, 86, 79), (224, 163, 65), (67, 181, 129))):
        draw.ellipse([20 + i * 20, 15, 32 + i * 20, 27], fill=color)
    draw.text((width // 2, 20), "slo-watch, live run", fill=DIM, font=font(MONO, 13), anchor="mm")

    y = pad + 40
    for line in lines:
        color = TEXT
        if "FIRING" in line:
            color = BAD
        elif "RESOLVED" in line:
            color = OK
        elif line.startswith("$"):
            color = ACCENT
        elif line.startswith("#") or line.startswith("    "):
            color = DIM
        draw.text((pad, y), line, fill=color, font=body)
        y += leading

    image.save(out / "cli.png", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="render docs images")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("docs"))
    parser.add_argument("--transcript", type=Path, default=Path("transcript.txt"))
    parser.add_argument("--objective", type=float, default=99.0)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    render_architecture(args.out)
    render_burn_chart(args.db, args.out, args.objective)
    if args.transcript.exists():
        render_transcript(args.transcript, args.out)
    for png in sorted(args.out.glob("*.png")):
        print(f"{png}  {png.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
