"""
Builds the client-facing PowerPoint deck for the Raptor Deployment Gate story,
in a clean, consulting-style business presentation format.

Generates docs/raptor_framework_story.pptx from:
  - the architecture/data-flow narrative in docs/raptor_framework_story.html
  - the three real live-run dashboards in data/live_results_*.png

Design rules followed throughout (what makes this read as "professional" rather
than "cluttered"):
  - One idea per slide. No slide tries to show the whole system at once.
  - A consistent header (kicker + title + rule) and footer (brand + page #)
    on every content slide, so the deck has a visible structure.
  - A type scale with a floor: nothing below 11pt anywhere, body copy at
    14-16pt, titles at 28-32pt.
  - Generous whitespace -- margins and gaps are never squeezed just to fit
    one more box on a slide.
  - Flow diagrams use arrow AutoShapes (not line connectors), which render
    reliably and look intentional rather than bent/jagged.

Run:
    .venv\\Scripts\\python.exe scripts\\build_client_ppt.py
"""
from pathlib import Path
import shutil

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "raptor_framework_story.pptx"
OUT_TMP = ROOT / "docs" / "raptor_framework_story.tmp.pptx"

# ---------------------------------------------------------------- palette --
NAVY = RGBColor(0x14, 0x1B, 0x2E)
NAVY_SOFT = RGBColor(0x1A, 0x22, 0x33)
MUTED = RGBColor(0x5B, 0x66, 0x7A)
ACCENT = RGBColor(0x1D, 0x5F, 0xD6)
ACCENT_DARK = RGBColor(0x14, 0x45, 0xA0)
ACCENT_BG = RGBColor(0xEE, 0xF2, 0xF8)
GREEN = RGBColor(0x15, 0x8A, 0x4D)
GREEN_BG = RGBColor(0xE8, 0xF7, 0xEE)
RED = RGBColor(0xC2, 0x2A, 0x3D)
RED_BG = RGBColor(0xFC, 0xEC, 0xEE)
AMBER = RGBColor(0xA8, 0x63, 0x08)
AMBER_BG = RGBColor(0xFD, 0xF2, 0xDF)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
PANEL = RGBColor(0xF5, 0xF7, 0xFA)
BORDER = RGBColor(0xDA, 0xE1, 0xEA)
FOOT_LINE = RGBColor(0xE4, 0xE9, 0xF0)

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"

SLIDE_W = Inches(13.333)
SLIDE_H = Inches(7.5)
MARGIN_X = Inches(0.75)
CONTENT_W = SLIDE_W - 2 * MARGIN_X

prs = Presentation()
prs.slide_width = SLIDE_W
prs.slide_height = SLIDE_H
BLANK = prs.slide_layouts[6]

PAGE = {"n": 0}
# Number of slides that call footer() -- agenda, problem, 2 deployment slides,
# 2 decision-engine slides, 3 scenario slides, common-thread, comparison,
# safety-model = 12. Title/section-divider/closing slides are branded full-bleed
# and intentionally have no page-number footer.
TOTAL_CONTENT_SLIDES = 12


# --------------------------------------------------------------- helpers --
def add_slide():
    return prs.slides.add_slide(BLANK)


def set_bg(slide, color=WHITE):
    bg = slide.background
    bg.fill.solid()
    bg.fill.fore_color.rgb = color


def _no_autosize(tf):
    try:
        tf.word_wrap = True
        tf.auto_size = None
    except Exception:
        pass


def letter_spaced(run, pts_hundredths=120):
    """Add a touch of tracking to a run (for small-caps kicker labels)."""
    rPr = run._r.get_or_add_rPr()
    rPr.set("spc", str(pts_hundredths))


def textbox(slide, left, top, width, height, text, size=16, bold=False,
            color=NAVY, align=PP_ALIGN.LEFT, font=FONT, italic=False,
            anchor=MSO_ANCHOR.TOP, line_spacing=1.15, spaced=False):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    _no_autosize(tf)
    tf.vertical_anchor = anchor
    lines = text.split("\n")
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line_spacing
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.italic = italic
        r.font.color.rgb = color
        r.font.name = font
        if spaced:
            letter_spaced(r)
    return box


def bullet_list(slide, left, top, width, height, items, size=14.5,
                 color=NAVY, dot_color=ACCENT, line_spacing=1.25, gap_pt=10):
    box = slide.shapes.add_textbox(left, top, width, height)
    tf = box.text_frame
    _no_autosize(tf)
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = line_spacing
        p.space_after = Pt(gap_pt)
        r1 = p.add_run()
        r1.text = "\u25CF  "
        r1.font.size = Pt(size)
        r1.font.color.rgb = dot_color
        r1.font.name = FONT
        r2 = p.add_run()
        r2.text = item
        r2.font.size = Pt(size)
        r2.font.color.rgb = color
        r2.font.name = FONT
    return box


def rect(slide, left, top, width, height, fill=WHITE, line=BORDER, line_w=1.0,
          shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06):
    shp = slide.shapes.add_shape(shape, left, top, width, height)
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            shp.adjustments[0] = radius
        except Exception:
            pass
    if fill is None:
        shp.fill.background()
    else:
        shp.fill.solid()
        shp.fill.fore_color.rgb = fill
    if line is None:
        shp.line.fill.background()
    else:
        shp.line.color.rgb = line
        shp.line.width = Pt(line_w)
    shp.shadow.inherit = False
    return shp


def card(slide, left, top, width, height, title=None, body=None,
         fill=WHITE, line=BORDER, title_color=NAVY, title_size=15,
         body_color=MUTED, body_size=12, align=PP_ALIGN.LEFT, pad=0.22,
         valign=MSO_ANCHOR.TOP, title_bold=True, line_w=1.0):
    shp = rect(slide, left, top, width, height, fill=fill, line=line, line_w=line_w)
    tf = shp.text_frame
    _no_autosize(tf)
    tf.margin_left = Inches(pad)
    tf.margin_right = Inches(pad)
    tf.margin_top = Inches(pad * 0.8)
    tf.margin_bottom = Inches(pad * 0.8)
    tf.vertical_anchor = valign
    first = True
    if title:
        p0 = tf.paragraphs[0]
        p0.alignment = align
        r = p0.add_run()
        r.text = title
        r.font.size = Pt(title_size)
        r.font.bold = title_bold
        r.font.color.rgb = title_color
        r.font.name = FONT
        first = False
    if body:
        for line in body.split("\n"):
            p = tf.add_paragraph() if not first else tf.paragraphs[0]
            first = False
            p.alignment = align
            p.line_spacing = 1.2
            p.space_before = Pt(4) if title else Pt(0)
            r = p.add_run()
            r.text = line
            r.font.size = Pt(body_size)
            r.font.color.rgb = body_color
            r.font.name = FONT
    return shp


def stat_chip(slide, left, top, width, height, label, value, color=ACCENT,
              fill=WHITE, line=BORDER):
    shp = rect(slide, left, top, width, height, fill=fill, line=line)
    tf = shp.text_frame
    _no_autosize(tf)
    tf.margin_left = Inches(0.18)
    tf.margin_right = Inches(0.18)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p0 = tf.paragraphs[0]
    p0.alignment = PP_ALIGN.LEFT
    r0 = p0.add_run()
    r0.text = label.upper()
    r0.font.size = Pt(10.5)
    r0.font.bold = True
    r0.font.color.rgb = MUTED
    r0.font.name = FONT
    letter_spaced(r0, 60)
    p1 = tf.add_paragraph()
    p1.alignment = PP_ALIGN.LEFT
    p1.space_before = Pt(2)
    r1 = p1.add_run()
    r1.text = value
    r1.font.size = Pt(16.5)
    r1.font.bold = True
    r1.font.color.rgb = color
    r1.font.name = FONT
    return shp


def h_arrow(slide, left, top, width=Inches(0.42), height=Inches(0.22), color=MUTED):
    shp = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, left, top, width, height)
    shp.fill.solid(); shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def v_arrow(slide, left, top, width=Inches(0.22), height=Inches(0.32), color=MUTED):
    shp = slide.shapes.add_shape(MSO_SHAPE.DOWN_ARROW, left, top, width, height)
    shp.fill.solid(); shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def number_badge(slide, cx, cy, diameter, text, fill=ACCENT, color=WHITE, size=16):
    shp = slide.shapes.add_shape(MSO_SHAPE.OVAL, cx, cy, diameter, diameter)
    shp.fill.solid(); shp.fill.fore_color.rgb = fill
    shp.line.fill.background()
    shp.shadow.inherit = False
    tf = shp.text_frame
    tf.margin_left = 0; tf.margin_right = 0; tf.margin_top = 0; tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.bold = True
    r.font.color.rgb = color
    r.font.name = FONT
    return shp


def picture_framed(slide, path, left, top, height, line=BORDER):
    pic = slide.shapes.add_picture(str(path), left, top, height=height)
    frame = rect(slide, pic.left, pic.top, pic.width, pic.height, fill=None,
                 line=line, line_w=1.0, shape=MSO_SHAPE.RECTANGLE)
    return pic


def header(slide, kicker, title, accent=ACCENT):
    r = textbox(slide, MARGIN_X, Inches(0.42), Inches(9), Inches(0.3),
                kicker.upper(), size=11.5, bold=True, color=accent, spaced=True)
    textbox(slide, MARGIN_X, Inches(0.72), CONTENT_W, Inches(0.6),
            title, size=28, bold=True, color=NAVY)
    rule = rect(slide, MARGIN_X, Inches(1.42), CONTENT_W, Pt(1.4), fill=BORDER,
                line=None, shape=MSO_SHAPE.RECTANGLE)
    return rule


def footer(slide, brand="RAPTOR DEPLOYMENT GATE  \u00b7  CLIENT BRIEFING"):
    PAGE["n"] += 1
    rect(slide, MARGIN_X, Inches(7.02), CONTENT_W, Pt(1), fill=FOOT_LINE, line=None,
         shape=MSO_SHAPE.RECTANGLE)
    textbox(slide, MARGIN_X, Inches(7.1), Inches(8), Inches(0.3), brand,
            size=9.5, color=MUTED, spaced=True)
    textbox(slide, SLIDE_W - MARGIN_X - Inches(1.5), Inches(7.1), Inches(1.5), Inches(0.3),
            f"{PAGE['n']:02d} / {TOTAL_CONTENT_SLIDES:02d}", size=9.5, color=MUTED,
            align=PP_ALIGN.RIGHT)


def full_bleed(slide, color=NAVY):
    rect(slide, 0, 0, SLIDE_W, SLIDE_H, fill=color, line=None, shape=MSO_SHAPE.RECTANGLE)


def clean_table(slide, left, top, width, height, rows_data, col_widths,
                 header_fill=ACCENT, header_color=WHITE, body_size=13,
                 header_size=13, row_colors=None, align_cols=None):
    n_rows = len(rows_data)
    n_cols = len(rows_data[0])
    gfx = slide.shapes.add_table(n_rows, n_cols, left, top, width, height)
    tbl = gfx.table
    tbl.first_row = False
    tbl.horz_banding = False
    for ci, w in enumerate(col_widths):
        tbl.columns[ci].width = w
    for ri, row in enumerate(rows_data):
        for ci, val in enumerate(row):
            cell = tbl.cell(ri, ci)
            cell.text = str(val)
            p = cell.text_frame.paragraphs[0]
            p.font.size = Pt(header_size if ri == 0 else body_size)
            p.font.name = FONT
            p.font.bold = (ri == 0)
            p.alignment = (align_cols[ci] if align_cols else PP_ALIGN.LEFT)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.14)
            cell.margin_right = Inches(0.14)
            cell.margin_top = Inches(0.06)
            cell.margin_bottom = Inches(0.06)
            cell.fill.solid()
            if ri == 0:
                cell.fill.fore_color.rgb = header_fill
                p.font.color.rgb = header_color
            else:
                bg = PANEL if (row_colors is None and ri % 2 == 0) else WHITE
                cell.fill.fore_color.rgb = row_colors[ri] if row_colors else bg
                p.font.color.rgb = NAVY
    return tbl


# =============================================================================
# SLIDE 1 — Title
# =============================================================================
s = add_slide(); set_bg(s, WHITE)
rect(s, 0, 0, SLIDE_W, Inches(0.12), fill=ACCENT, line=None, shape=MSO_SHAPE.RECTANGLE)
rect(s, MARGIN_X, Inches(2.5), Inches(0.08), Inches(2.55), fill=ACCENT, line=None,
     shape=MSO_SHAPE.RECTANGLE)
textbox(s, MARGIN_X + Inches(0.35), Inches(2.55), Inches(10), Inches(0.35),
        "CANARY DEPLOYMENT AUTOMATION", size=13, bold=True, color=ACCENT, spaced=True)
textbox(s, MARGIN_X + Inches(0.35), Inches(2.95), Inches(11.2), Inches(1.2),
        "Raptor Deployment Gate", size=48, bold=True, color=NAVY)
textbox(s, MARGIN_X + Inches(0.35), Inches(4.05), Inches(10.3), Inches(1.0),
        "How an automated, ML-driven quality gate decides \u2014 in seconds, from real "
        "production metrics \u2014 whether a new deployment is promoted or rolled back.",
        size=16, color=MUTED, line_spacing=1.35)

rect(s, MARGIN_X, Inches(6.4), CONTENT_W, Pt(1), fill=BORDER, line=None, shape=MSO_SHAPE.RECTANGLE)
textbox(s, MARGIN_X, Inches(6.6), Inches(10), Inches(0.35),
        "Client Briefing  \u00b7  eric-cenx-rest-api, robot-poc namespace  \u00b7  October 2026",
        size=11.5, color=MUTED)

# =============================================================================
# SLIDE 2 — Agenda
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "Overview", "What we'll walk through")

agenda = [
    ("01", "The problem", "Why canary releases need an automatic, repeatable decision"),
    ("02", "How it's deployed", "The services, pods, and network paths involved in robot-poc"),
    ("03", "How the decision is made", "Live metrics, an ML model, and an explicit rule engine"),
    ("04", "Three real deployments", "A clean release, a flickering noise event, and a real failure"),
    ("05", "Why it matters", "The business case, and the safety guarantees underneath it"),
]
y = Inches(1.75)
row_h = Inches(0.92)
for num, title, sub in agenda:
    number_badge(s, MARGIN_X, y + Inches(0.08), Inches(0.5), num, fill=ACCENT_BG, color=ACCENT, size=15)
    textbox(s, MARGIN_X + Inches(0.75), y, Inches(10.5), Inches(0.4), title, size=17, bold=True, color=NAVY)
    textbox(s, MARGIN_X + Inches(0.75), y + Inches(0.42), Inches(10.5), Inches(0.35), sub, size=12.5, color=MUTED)
    if num != "05":
        rect(s, MARGIN_X, y + row_h - Inches(0.1), CONTENT_W, Pt(0.75), fill=FOOT_LINE, line=None, shape=MSO_SHAPE.RECTANGLE)
    y += row_h
footer(s)

# =============================================================================
# SLIDE 3 — The problem
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "01 \u00b7 The Problem", "Deployment safety shouldn't be a judgment call")

col_w = Inches(5.55)
gap = Inches(0.35)
left_x = MARGIN_X
right_x = MARGIN_X + col_w + gap

rect(s, left_x, Inches(1.75), col_w, Inches(0.55), fill=RED_BG, line=RED, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
textbox(s, left_x + Inches(0.25), Inches(1.9), col_w - Inches(0.5), Inches(0.3),
        "Today, without automation", size=15, bold=True, color=RED)
bullet_list(s, left_x, Inches(2.55), col_w, Inches(3.8), [
    "A person watches dashboards after every deploy and makes the call",
    "Judgment varies by who's on call, and how tired they are",
    "Subtle, cross-metric issues are easy to miss in the moment",
    "A bad deployment can sit in production for minutes or hours",
    "A recurring minor issue can keep slipping through, release after release",
], dot_color=RED, size=14)

rect(s, right_x, Inches(1.75), col_w, Inches(0.55), fill=GREEN_BG, line=GREEN, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
textbox(s, right_x + Inches(0.25), Inches(1.9), col_w - Inches(0.5), Inches(0.3),
        "With the Raptor Gate", size=15, bold=True, color=GREEN)
bullet_list(s, right_x, Inches(2.55), col_w, Inches(3.8), [
    "The same 9 metrics, scored the same way, on every deployment",
    "Machine learning, explicit SLA rules, and cross-deployment memory \u2014 combined",
    "A clear verdict in seconds: PROMOTE or ROLLBACK",
    "Full supporting evidence for every decision \u2014 audit-ready",
    "Fits existing CI/CD: the whole contract is a single exit code",
], dot_color=GREEN, size=14)
footer(s)

# =============================================================================
# SLIDE 4 — From commit to cluster (build/deploy flow)
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "02 \u00b7 Deployment", "From commit to cluster")

textbox(s, MARGIN_X, Inches(1.62), CONTENT_W, Inches(0.4),
        "The gate ships through the same CI/CD path as any other service \u2014 no special-cased pipeline.",
        size=13, color=MUTED)

steps = [
    ("CI/CD pipeline", "GitLab CI / Jenkins\ntriggers the build"),
    ("Kaniko build job", "Unprivileged in-cluster\nimage build"),
    ("Push to registry", "Image pushed to the\nclient's own registry"),
    ("kubectl apply", "Deployment + Service\napplied to robot-poc"),
]
n = len(steps)
box_w = Inches(2.55)
arrow_w = Inches(0.45)
total_w = box_w * n + arrow_w * (n - 1)
start_x = MARGIN_X + (CONTENT_W - total_w) / 2
y = Inches(2.9)
x = start_x
for i, (title, sub) in enumerate(steps):
    card(s, x, y, box_w, Inches(1.5), title=title, body=sub, fill=ACCENT_BG, line=ACCENT,
         title_color=ACCENT_DARK, title_size=14, body_color=MUTED, body_size=11.5,
         align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
    x += box_w
    if i < n - 1:
        h_arrow(s, x + Inches(0.02), y + Inches(0.64), width=arrow_w - Inches(0.1))
        x += arrow_w

result_y = Inches(4.9)
card(s, MARGIN_X + Inches(1.5), result_y, Inches(9.3), Inches(1.15),
     title="Result: Deployment/raptor-gate (2 replicas) + Service/raptor-gate (ClusterIP)",
     body="The serving API runs continuously inside robot-poc, reachable in-cluster by the pipeline or via port-forward.",
     fill=PANEL, line=BORDER, title_color=NAVY, title_size=14.5, body_color=MUTED, body_size=12,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
footer(s)

# =============================================================================
# SLIDE 5 — Inside robot-poc (runtime data flow)
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "02 \u00b7 Deployment", "Inside robot-poc \u2014 what talks to what")

cluster_top = Inches(1.65)
cluster_h = Inches(5.1)
rect(s, MARGIN_X, cluster_top, CONTENT_W, cluster_h, fill=RGBColor(0xFA, 0xFC, 0xFF), line=ACCENT, line_w=1.25)
textbox(s, MARGIN_X + Inches(0.3), cluster_top + Inches(0.18), Inches(7), Inches(0.3),
        "KUBERNETES NAMESPACE \u2014 ROBOT-POC", size=11, bold=True, color=ACCENT, spaced=True)

gate_w = Inches(4.2)
gate_x = MARGIN_X + (CONTENT_W - gate_w) / 2
card(s, gate_x, cluster_top + Inches(0.6), gate_w, Inches(0.85),
     title="Raptor Gate", body="FastAPI service \u00b7 Isolation Forest model",
     fill=ACCENT, line=ACCENT_DARK, title_color=WHITE, title_size=16,
     body_color=RGBColor(0xDC, 0xE6, 0xF8), body_size=11.5,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

v_arrow(s, gate_x + gate_w / 2 - Inches(0.11), cluster_top + Inches(1.5), height=Inches(0.28))
textbox(s, gate_x + gate_w, cluster_top + Inches(1.55), Inches(3), Inches(0.25),
        "reads live metrics", size=10.5, color=MUTED, italic=True)

sources = [
    ("Prometheus", "CPU \u00b7 memory"),
    ("Kubernetes API", "pod restarts"),
    ("Redis", "cache latency"),
    ("Live HTTP probe", "latency \u00b7 errors \u00b7 availability \u00b7 packet loss"),
]
sw = Inches(2.75)
sgap = Inches(0.2)
stotal = sw * 4 + sgap * 3
sx = MARGIN_X + (CONTENT_W - stotal) / 2
sy = cluster_top + Inches(1.95)
for name, sub in sources:
    card(s, sx, sy, sw, Inches(0.95), title=name, body=sub, fill=WHITE, line=BORDER,
         title_color=NAVY, title_size=13, body_color=MUTED, body_size=10.5,
         align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
    sx += sw + sgap

v_arrow(s, gate_x + gate_w / 2 - Inches(0.11), sy + Inches(1.05), height=Inches(0.28))
textbox(s, gate_x + gate_w, sy + Inches(1.1), Inches(3.4), Inches(0.25),
        "probes the live service", size=10.5, color=MUTED, italic=True)

target_w = Inches(4.6)
target_x = MARGIN_X + (CONTENT_W - target_w) / 2
card(s, target_x, sy + Inches(1.45), target_w, Inches(0.85),
     title="Service: eric-cenx-rest-api", body="the canary workload being evaluated",
     fill=AMBER_BG, line=AMBER, title_color=AMBER, title_size=14.5,
     body_color=MUTED, body_size=11, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

footer(s)

# =============================================================================
# SLIDE 6 — The 9 metrics
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "03 \u00b7 The Decision", "Nine metrics, every single deployment")

textbox(s, MARGIN_X, Inches(1.6), CONTENT_W, Inches(0.4),
        "Four independent collectors run on every scoring pass \u2014 covering compute, platform, cache, and live user experience.",
        size=13, color=MUTED)

metrics_rows = [
    ("Metric", "Source", "SLA threshold"),
    ("CPU usage", "Prometheus", "\u2264 80%"),
    ("Memory usage", "Prometheus", "\u2264 75%"),
    ("Pod restarts", "Kubernetes API", "\u2264 3"),
    ("HTTP error rate", "Live HTTP probe", "\u2264 1%"),
    ("p99 latency", "Live HTTP probe", "\u2264 200 ms"),
    ("Availability", "Live HTTP probe", "\u2265 99.5%"),
    ("Redis latency", "Redis", "\u2264 10 ms"),
    ("Endpoint latency", "Live HTTP probe", "\u2264 200 ms"),
    ("Packet loss", "Live HTTP probe", "\u2264 1%"),
]
clean_table(s, MARGIN_X, Inches(2.2), Inches(8.0), Inches(4.5), metrics_rows,
            col_widths=[Inches(3.1), Inches(2.9), Inches(2.0)],
            header_size=13.5, body_size=13,
            align_cols=[PP_ALIGN.LEFT, PP_ALIGN.LEFT, PP_ALIGN.CENTER])

note_x = Inches(9.0)
card(s, note_x, Inches(2.2), Inches(3.55), Inches(4.5),
     title="Fail-closed, always",
     body="If any one of these 9 metrics can't be reliably collected, the gate does "
          "not guess. It returns ROLLBACK outright.\n\nUncertainty is treated as a "
          "reason not to promote \u2014 never a reason to proceed.",
     fill=AMBER_BG, line=AMBER, title_color=AMBER, title_size=15, body_color=NAVY,
     body_size=12.5, valign=MSO_ANCHOR.TOP)
footer(s)

# =============================================================================
# SLIDE 7 — Decision engine flow
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "03 \u00b7 The Decision", "How a sample becomes a verdict")

box_w = Inches(7.2)
bx = MARGIN_X + (CONTENT_W - box_w) / 2
y = Inches(1.65)

pair_w = Inches(3.45)
card(s, bx, y, pair_w, Inches(0.8), title="Isolation Forest",
     body="ML, trained on this service's own healthy history",
     fill=ACCENT_BG, line=ACCENT, title_color=ACCENT_DARK, title_size=13.5,
     body_color=MUTED, body_size=10.5, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, bx + pair_w + Inches(0.3), y, pair_w, Inches(0.8), title="SLA rule engine",
     body="9 explicit thresholds \u2014 each breach deducts 20 points",
     fill=ACCENT_BG, line=ACCENT, title_color=ACCENT_DARK, title_size=13.5,
     body_color=MUTED, body_size=10.5, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

y2 = y + Inches(0.95)
v_arrow(s, bx + box_w / 2 - Inches(0.11), y2, height=Inches(0.25))
y2 += Inches(0.32)
card(s, bx, y2, box_w, Inches(0.65), title="Health Score (0\u2013100)  =  anomaly score \u2212 (20 \u00d7 SLA breaches)",
     fill=AMBER_BG, line=AMBER, title_color=AMBER, title_size=14, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

y3 = y2 + Inches(0.72)
v_arrow(s, bx + box_w / 2 - Inches(0.11), y3, height=Inches(0.25))
y3 += Inches(0.32)
half_w = Inches(3.45)
card(s, bx, y3, half_w, Inches(0.6), title="Below 70 \u2192 ROLLBACK", fill=RED_BG, line=RED,
     title_color=RED, title_size=14, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, bx + half_w + Inches(0.3), y3, half_w, Inches(0.6), title="\u2265 70 \u2192 provisionally PROMOTE",
     fill=GREEN_BG, line=GREEN, title_color=GREEN, title_size=14, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

y4 = y3 + Inches(0.75)
v_arrow(s, bx + box_w / 2 - Inches(0.11), y4, height=Inches(0.25))
y4 += Inches(0.32)
card(s, bx, y4, box_w, Inches(0.95),
     title="Did this same metric also breach its SLA on a recent prior deployment?",
     body="If yes \u2192 forced ROLLBACK, regardless of the score above. A recurring problem\nis never allowed to hide behind one passing sample.",
     fill=WHITE, line=AMBER, title_color=AMBER, title_size=13.5, body_color=MUTED,
     body_size=11, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
footer(s)

# =============================================================================
# SLIDE 8 — Section divider: Three real deployments
# =============================================================================
s = add_slide(); set_bg(s, WHITE)
rect(s, 0, 0, SLIDE_W, Inches(0.12), fill=ACCENT, line=None, shape=MSO_SHAPE.RECTANGLE)
textbox(s, MARGIN_X, Inches(2.5), Inches(11.8), Inches(0.35),
        "04 \u00b7 PROOF, NOT A DEMO SCRIPT", size=13, bold=True, color=ACCENT, spaced=True)
textbox(s, MARGIN_X, Inches(2.9), Inches(11.8), Inches(1.0),
        "Three real deployments, three outcomes", size=36, bold=True, color=NAVY)
textbox(s, MARGIN_X, Inches(3.95), Inches(10.8), Inches(0.8),
        "Every chart on the next three pages was captured live against eric-cenx-rest-api "
        "in robot-poc on October 21, 2026 \u2014 not simulated.", size=15, color=MUTED,
        line_spacing=1.3)

chips = [("A", "Clean release", "PROMOTE", GREEN, GREEN_BG), ("B", "Drift + flicker", "MOSTLY PROMOTE", AMBER, AMBER_BG),
         ("C", "Degraded canary", "ROLLBACK", RED, RED_BG)]
cw = Inches(3.5); cgap = Inches(0.3)
ctotal = cw * 3 + cgap * 2
cx = MARGIN_X + (Inches(11.8) - ctotal) / 2
cy = Inches(5.1)
for letter, label, verdict, color, bg in chips:
    rect(s, cx, cy, cw, Inches(1.25), fill=bg, line=color, line_w=1.25)
    number_badge(s, cx + Inches(0.2), cy + Inches(0.2), Inches(0.5), letter, fill=color, color=WHITE, size=16)
    textbox(s, cx + Inches(0.85), cy + Inches(0.2), cw - Inches(1.0), Inches(0.35), label, size=13.5, bold=True, color=NAVY)
    textbox(s, cx + Inches(0.85), cy + Inches(0.58), cw - Inches(1.0), Inches(0.35), verdict, size=12, bold=True, color=color)
    cx += cw + cgap

# =============================================================================
# SLIDES 9-11 — Three real scenarios
# =============================================================================
scenarios = [
    dict(
        tag="Scenario A", letter="A",
        headline="The clean release", verdict="PROMOTE", color=GREEN, bg=GREEN_BG,
        img=DATA / "live_results_eric-cenx-rest-api_20261021_110134_promote.png",
        desc=("82 consecutive samples, about 90 seconds of live traffic. Every metric stays "
              "comfortably inside its SLA line throughout \u2014 memory usage even settles "
              "down partway through, from roughly 37% to 7\u201310%."),
        stats=[("Samples scored", "82"), ("Verdict", "PROMOTE \u00b7 82 / 82"),
               ("Health score range", "78.4 \u2013 93.5"), ("SLA breaches", "0")],
        note="This is the baseline for \u201cnormal.\u201d The gate re-checks every few seconds, so a "
             "service that's healthy at minute one has to stay healthy at minute two to keep clearing the bar.",
    ),
    dict(
        tag="Scenario B", letter="B",
        headline="Drift, and a flickering spike", verdict="MOSTLY PROMOTE", color=AMBER, bg=AMBER_BG,
        img=DATA / "live_results_eric-cenx-rest-api_20261021_110731_change_values.png",
        desc=("Memory usage climbs across the window, from about 20% to 24%, but never crosses "
              "its 75% SLA line \u2014 the exact slow drift a fixed dashboard alert would miss. "
              "Separately, Redis latency spikes three times, just over its 10ms limit."),
        stats=[("Samples scored", "63"), ("Verdicts", "60 PROMOTE \u00b7 3 ROLLBACK"),
               ("Health score range", "51.9 \u2013 83.9"), ("SLA breaches", "3 \u00d7 redis_latency")],
        note="Each spike drops that one sample to ROLLBACK, then recovers on the very next sample. "
             "Because the breach didn't repeat across enough recent samples, the persistent-breach "
             "override never fires \u2014 this reads as transient noise, not a standing problem.",
    ),
    dict(
        tag="Scenario C", letter="C",
        headline="The degraded canary", verdict="ROLLBACK", color=RED, bg=RED_BG,
        img=DATA / "live_results_eric-cenx-rest-api_20261021_130754_rollback.png",
        desc=("p99 latency repeatedly breaks its 200ms SLA line \u2014 spiking as high as ~300ms \u2014 "
              "twelve separate times, and Redis latency breaches its 10ms limit three more times. "
              "Unlike Scenario B, this problem keeps recurring throughout the run."),
        stats=[("Samples scored", "56"), ("Verdicts", "41 PROMOTE \u00b7 15 ROLLBACK"),
               ("Health score range", "51.9 \u2013 78.5"), ("SLA breaches", "12 \u00d7 p99 \u00b7 3 \u00d7 redis")],
        note="Over a quarter of samples in this window came back as a breach. In a CI/CD pipeline, "
             "this canary never gets to see 100% of production traffic \u2014 the gate reverts it "
             "automatically, before a human would have finished opening the dashboard.",
    ),
]

for sc in scenarios:
    s = add_slide(); set_bg(s)
    header(s, f"04 \u00b7 {sc['tag']}", sc["headline"])
    rect(s, SLIDE_W - MARGIN_X - Inches(2.6), Inches(0.5), Inches(2.6), Inches(0.55),
         fill=sc["bg"], line=sc["color"], line_w=1.25)
    textbox(s, SLIDE_W - MARGIN_X - Inches(2.6), Inches(0.62), Inches(2.6), Inches(0.35),
            sc["verdict"], size=14, bold=True, color=sc["color"], align=PP_ALIGN.CENTER)

    img_h = Inches(4.75)
    picture_framed(s, sc["img"], MARGIN_X, Inches(1.65), img_h)

    rx = MARGIN_X + Inches(5.35)
    rw = SLIDE_W - MARGIN_X - rx
    textbox(s, rx, Inches(1.65), rw, Inches(1.35), sc["desc"], size=13, color=NAVY, line_spacing=1.3)

    chip_w = (rw - Inches(0.2)) / 2
    chip_h = Inches(0.78)
    gy = Inches(3.15)
    positions = [(rx, gy), (rx + chip_w + Inches(0.2), gy),
                 (rx, gy + chip_h + Inches(0.15)), (rx + chip_w + Inches(0.2), gy + chip_h + Inches(0.15))]
    for (cx2, cy2), (label, value) in zip(positions, sc["stats"]):
        stat_chip(s, cx2, cy2, chip_w, chip_h, label, value, color=sc["color"])

    note_y = gy + 2 * (chip_h + Inches(0.15)) + Inches(0.2)
    card(s, rx, note_y, rw, Inches(1.5), body=sc["note"], fill=PANEL, line=BORDER,
         body_color=MUTED, body_size=11.5, valign=MSO_ANCHOR.MIDDLE)
    footer(s, "Captured live against eric-cenx-rest-api, robot-poc  \u00b7  Oct 21, 2026")

# =============================================================================
# SLIDE 12 — The common thread
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "04 \u00b7 Three Real Deployments", "The thread connecting all three")

card(s, MARGIN_X + Inches(1.0), Inches(2.3), CONTENT_W - Inches(2.0), Inches(3.3),
     body="It's the exact same engine, the exact same 9 thresholds, and the exact same "
          "health-score formula \u2014 run unchanged against three different moments of the "
          "same real service.\n\nThe only thing that changes is what the metrics actually "
          "did. Nothing about the verdict is subjective.",
     fill=GREEN_BG, line=GREEN, body_color=NAVY, body_size=18, align=PP_ALIGN.CENTER,
     valign=MSO_ANCHOR.MIDDLE)
footer(s)

# =============================================================================
# SLIDE 13 — Why it matters
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "05 \u00b7 Why It Matters", "The case for an automatic gate")

compare_rows = [
    ("Without an automated gate", "With the Raptor Gate"),
    ("A human watches dashboards and decides, under time pressure", "Automatic, consistent, takes seconds"),
    ("Judgment varies by who's on call", "Every deployment scored by the same rules and model"),
    ("Subtle cross-metric issues are easy to miss", "The ML model is built to catch combinations no single rule would flag"),
    ("Bad deployments can sit in prod for minutes or hours", "Rollback happens automatically, as part of the pipeline itself"),
    ("A minor, recurring issue can keep slipping through", "A metric that keeps breaking its SLA forces a rollback outright"),
]
half_col = Emu(int(CONTENT_W / 2))
tbl = clean_table(s, MARGIN_X, Inches(1.75), CONTENT_W, Inches(4.95), compare_rows,
                   col_widths=[half_col, half_col], header_size=14.5, body_size=13,
                   header_fill=NAVY)
# Recolor per column so the contrast reads at a glance: red-tinted "without",
# green-tinted "with" -- header row keeps a strong fill, body rows a soft tint.
for ri in range(len(compare_rows)):
    for ci in range(2):
        cell = tbl.cell(ri, ci)
        if ri == 0:
            cell.fill.fore_color.rgb = RED if ci == 0 else GREEN
            cell.text_frame.paragraphs[0].font.color.rgb = WHITE
        else:
            cell.fill.fore_color.rgb = RED_BG if ci == 0 else GREEN_BG
            cell.text_frame.paragraphs[0].font.color.rgb = NAVY
footer(s)

# =============================================================================
# SLIDE 14 — Safety model
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "05 \u00b7 Why It Matters", "Built-in safety model")

safety = [
    ("Fails closed, never open", "If metrics can't be reliably collected, the gate never guesses \u2014 it returns ROLLBACK."),
    ("A recurring issue overrides a passing score", "A metric breaking its SLA across multiple recent deployments forces a rollback outright."),
    ("Notification failures never change the decision", "GitLab / Jenkins / webhook notifications are additive \u2014 never a point of failure for the core decision."),
    ("Secrets are never stored in config", "Credentials are read from the environment only. Nothing sensitive is committed or hard-coded."),
]
gw = Inches(5.75); gh = Inches(1.9); ggap = Inches(0.35)
gx0 = MARGIN_X
gy0 = Inches(1.75)
for i, (title, body) in enumerate(safety):
    col = i % 2
    row = i // 2
    gx = gx0 + col * (gw + ggap)
    gy = gy0 + row * (gh + ggap)
    card(s, gx, gy, gw, gh, title=title, body=body, fill=PANEL, line=BORDER,
         title_color=ACCENT_DARK, title_size=15, body_color=NAVY, body_size=12.5,
         valign=MSO_ANCHOR.TOP)
footer(s)

# =============================================================================
# SLIDE 15 — Closing
# =============================================================================
s = add_slide(); set_bg(s, WHITE)
rect(s, 0, 0, SLIDE_W, Inches(0.12), fill=ACCENT, line=None, shape=MSO_SHAPE.RECTANGLE)
textbox(s, MARGIN_X, Inches(2.1), Inches(11.8), Inches(0.35),
        "FITS EXISTING CI/CD \u2014 NO RIP AND REPLACE", size=13, bold=True,
        color=ACCENT, align=PP_ALIGN.CENTER, spaced=True)
textbox(s, MARGIN_X, Inches(2.55), Inches(11.8), Inches(0.9),
        "One contract: exit code 0 to promote,\nexit code 1 to roll back.",
        size=28, bold=True, color=NAVY, align=PP_ALIGN.CENTER, line_spacing=1.25)
textbox(s, Inches(2.2), Inches(4.1), Inches(9.0), Inches(0.8),
        "Any pipeline \u2014 GitLab CI, Jenkins, or a custom script \u2014 can branch on that "
        "directly, with no proprietary platform required.",
        size=15, color=MUTED, align=PP_ALIGN.CENTER, line_spacing=1.3)

rect(s, Inches(2.0), Inches(5.15), Inches(9.3), Inches(1.5), fill=ACCENT_BG, line=ACCENT, line_w=1.25)
textbox(s, Inches(2.35), Inches(5.35), Inches(8.6), Inches(1.15),
        "Raptor turns \u201cis this deployment safe to promote\u201d from a manual, inconsistent, "
        "delay-prone judgment call into an automatic, auditable, sub-second decision \u2014 "
        "running live against your infrastructure today.",
        size=13.5, color=NAVY, align=PP_ALIGN.CENTER, line_spacing=1.35)

prs.save(str(OUT_TMP))
try:
    shutil.move(str(OUT_TMP), str(OUT))
    print(f"Saved: {OUT}  ({len(prs.slides._sldIdLst)} slides)")
except PermissionError:
    print(f"Target locked; wrote to {OUT_TMP} instead. Close any app with the pptx open and retry, "
          f"or rename {OUT_TMP.name} to {OUT.name} manually.")
