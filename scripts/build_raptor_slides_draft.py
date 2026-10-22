"""
Builds docs/raptor_framework_slides_draft.pptx -- 8 standalone slides, one per
the content brief the user gave, so they can be freely reordered/merged by
hand afterward. Built on docs/reference_template.pptx (Ericsson theme/fonts/
logo), same visual language as the earlier raptor_framework.pptx drafts.

Slide map (matches the user's numbered brief exactly):
  1. The use case -- k8s, multiple microservices, the CI/CD upgrade problem,
     and what Raptor does about it.
  2. Metrics + collectors -- which metrics, how collectors are configured,
     example SLA values.
  3. Architecture diagram (left) + component explanations (right), following
     docs/raptor_framework.html chapter 2's diagram (CI/CD -> Kaniko ->
     robot-poc namespace -> collectors -> canary -> verdict).
  4. Health-score flow -- Isolation Forest + SLA engine -> health score ->
     persistent-breach (signal 3) and window-volatility (signal 4) overrides,
     all as one flow.
  5. Scenario A only -- the clean release (PROMOTE), from raptor_framework.html.
  6. Scenario B only -- drift + flickering spike (metrics changed), from
     raptor_framework.html.
  7. Scenario C only -- the degraded canary (ROLLBACK), from
     raptor_framework.html.
  8. Advantages -- without-gate vs. with-Raptor-Gate comparison.

Sources for every figure/formula/threshold used below:
  gate/cli.py, gate/metrics.py, gate/model.py, gate/scoring.py,
  gate/signals/trend.py, gate/signals/volatility.py,
  gate/config/production.yaml, gate/config/collectors.yaml,
  docs/raptor_framework.html (diagram structure + scenario narrative/stats).

Run:
    .venv\\Scripts\\python.exe scripts\\build_raptor_slides_draft.py
"""
import io
import re
import zipfile
from pathlib import Path
import shutil

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEMPLATE = ROOT / "docs" / "reference_template.pptx"
OUT = ROOT / "docs" / "raptor_framework_slides_draft.pptx"
OUT_TMP = ROOT / "docs" / "raptor_framework_slides_draft.tmp.pptx"

# --------------------------------------------------------- Ericsson palette --
DK1 = RGBColor(0x18, 0x18, 0x18)
ACCENT = RGBColor(0x11, 0x74, 0xE6)
ROSE = RGBColor(0xE6, 0x5D, 0x6A)
ORANGE = RGBColor(0xE6, 0x6E, 0x19)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

GREEN = RGBColor(0x15, 0x8A, 0x4D)
GREEN_BG = RGBColor(0xE8, 0xF7, 0xEE)
RED = ROSE
RED_BG = RGBColor(0xFC, 0xEC, 0xEE)
AMBER = ORANGE
AMBER_BG = RGBColor(0xFD, 0xF2, 0xDF)
ACCENT_BG = RGBColor(0xEA, 0xF1, 0xFD)
ACCENT_DARK = RGBColor(0x0B, 0x4E, 0xA8)
MUTED = RGBColor(0x55, 0x55, 0x55)
PANEL = RGBColor(0xF4, 0xF5, 0xF6)
BORDER = RGBColor(0xDD, 0xDD, 0xDD)
FOOT_LINE = RGBColor(0xE2, 0xE2, 0xE2)
MONO_BG = RGBColor(0x1E, 0x24, 0x2B)
MONO_FG = RGBColor(0xDC, 0xE6, 0xF2)
MONO_GREEN = RGBColor(0x7E, 0xE7, 0x9A)
MONO_ROSE = RGBColor(0xF0, 0x8C, 0x97)
CLUSTER_BG = RGBColor(0xFA, 0xFC, 0xFF)

FONT = "Ericsson Hilda"
FONT_LIGHT = "Ericsson Hilda Light"
MONO_FONT = "Consolas"


def _strip_template_slides(src_path: Path) -> bytes:
    """Strip the template's own demo slides at the zip level before
    python-pptx opens it -- see build_raptor_framework_pptx.py for why this
    has to happen before load rather than via sldIdLst.remove()."""
    with zipfile.ZipFile(src_path, "r") as zin:
        pres_xml = zin.read("ppt/presentation.xml").decode("utf-8")
        rels_xml = zin.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
        ct_xml = zin.read("[Content_Types].xml").decode("utf-8")

        rids = re.findall(r'<p:sldId\b[^>]*\br:id="([^"]+)"', pres_xml)
        pres_xml = re.sub(r"<p:sldIdLst>.*?</p:sldIdLst>", "<p:sldIdLst/>",
                           pres_xml, flags=re.S)

        slide_targets = []
        for rid in rids:
            m = re.search(
                rf'<Relationship\b[^>]*\bId="{re.escape(rid)}"[^>]*\bTarget="([^"]+)"[^>]*/>',
                rels_xml,
            )
            if m:
                slide_targets.append(m.group(1))
            rels_xml = re.sub(
                rf'<Relationship\b[^>]*\bId="{re.escape(rid)}"[^>]*/>', "", rels_xml
            )

        slide_parts = {f"ppt/{t}" for t in slide_targets}
        slide_rels_parts = {
            f"ppt/slides/_rels/{Path(t).name}.rels" for t in slide_targets
        }

        for part in slide_parts:
            partname = "/" + part
            ct_xml = re.sub(
                rf'<Override\b[^>]*\bPartName="{re.escape(partname)}"[^>]*/>',
                "",
                ct_xml,
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                name = item.filename
                if name in slide_parts or name in slide_rels_parts:
                    continue
                if name == "ppt/presentation.xml":
                    zout.writestr(item, pres_xml)
                elif name == "ppt/_rels/presentation.xml.rels":
                    zout.writestr(item, rels_xml)
                elif name == "[Content_Types].xml":
                    zout.writestr(item, ct_xml)
                else:
                    zout.writestr(item, zin.read(name))
        return buf.getvalue()


_clean_template = io.BytesIO(_strip_template_slides(TEMPLATE))
prs = Presentation(_clean_template)

SLIDE_W = prs.slide_width
SLIDE_H = prs.slide_height
MARGIN_X = Inches(0.6)
CONTENT_W = SLIDE_W - 2 * MARGIN_X

LAYOUTS = {l.name: l for l in prs.slide_masters[0].slide_layouts}
BLANK = LAYOUTS["Blank"]
TITLE_PAGE = LAYOUTS["Title Page"]

PAGE = {"n": 0}
TOTAL_CONTENT_SLIDES = 8


def add_slide(layout=BLANK):
    return prs.slides.add_slide(layout)


def set_bg(slide, color=None):
    if color is None:
        return
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
    rPr = run._r.get_or_add_rPr()
    rPr.set("spc", str(pts_hundredths))


def textbox(slide, left, top, width, height, text, size=16, bold=False,
            color=DK1, align=PP_ALIGN.LEFT, font=FONT, italic=False,
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


def bullet_list(slide, left, top, width, height, items, size=13.5,
                 color=DK1, dot_color=ACCENT, line_spacing=1.25, gap_pt=8,
                 font=FONT):
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
        r1.font.name = font
        r2 = p.add_run()
        r2.text = item
        r2.font.size = Pt(size)
        r2.font.color.rgb = color
        r2.font.name = font
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
         fill=WHITE, line=BORDER, title_color=DK1, title_size=13,
         body_color=MUTED, body_size=10.5, align=PP_ALIGN.LEFT, pad=0.16,
         valign=MSO_ANCHOR.TOP, title_bold=True, line_w=1.0, font=FONT):
    shp = rect(slide, left, top, width, height, fill=fill, line=line, line_w=line_w)
    tf = shp.text_frame
    _no_autosize(tf)
    tf.margin_left = Inches(pad)
    tf.margin_right = Inches(pad)
    tf.margin_top = Inches(pad * 0.75)
    tf.margin_bottom = Inches(pad * 0.75)
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
        r.font.name = font
        first = False
    if body:
        for line in body.split("\n"):
            p = tf.add_paragraph() if not first else tf.paragraphs[0]
            first = False
            p.alignment = align
            p.line_spacing = 1.15
            p.space_before = Pt(3) if title else Pt(0)
            r = p.add_run()
            r.text = line
            r.font.size = Pt(body_size)
            r.font.color.rgb = body_color
            r.font.name = font
    return shp


def stat_chip(slide, left, top, width, height, label, value, color=ACCENT,
              fill=WHITE, line=BORDER):
    shp = rect(slide, left, top, width, height, fill=fill, line=line)
    tf = shp.text_frame
    _no_autosize(tf)
    tf.margin_left = Inches(0.16)
    tf.margin_right = Inches(0.16)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p0 = tf.paragraphs[0]
    r0 = p0.add_run()
    r0.text = label.upper()
    r0.font.size = Pt(10)
    r0.font.bold = True
    r0.font.color.rgb = MUTED
    r0.font.name = FONT
    letter_spaced(r0, 50)
    p1 = tf.add_paragraph()
    p1.space_before = Pt(2)
    r1 = p1.add_run()
    r1.text = value
    r1.font.size = Pt(15.5)
    r1.font.bold = True
    r1.font.color.rgb = color
    r1.font.name = FONT
    return shp


def code_block(slide, left, top, width, height, lines, size=11.5):
    shp = rect(slide, left, top, width, height, fill=MONO_BG, line=None,
               shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
    tf = shp.text_frame
    _no_autosize(tf)
    tf.margin_left = Inches(0.22)
    tf.margin_right = Inches(0.2)
    tf.margin_top = Inches(0.14)
    tf.margin_bottom = Inches(0.14)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.line_spacing = 1.15
        segs = line if isinstance(line, list) else [(line, MONO_FG)]
        for text, color in segs:
            r = p.add_run()
            r.text = text
            r.font.size = Pt(size)
            r.font.name = MONO_FONT
            r.font.color.rgb = color
    return shp


def h_arrow(slide, left, top, width=Inches(0.4), height=Inches(0.2), color=MUTED):
    shp = slide.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, left, top, width, height)
    shp.fill.solid(); shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def v_arrow(slide, cx, top, width=Inches(0.22), height=Inches(0.22), color=MUTED):
    """Vertical down-arrow, horizontally centered on `cx`."""
    shp = slide.shapes.add_shape(MSO_SHAPE.DOWN_ARROW, cx - width / 2, top, width, height)
    shp.fill.solid(); shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def number_badge(slide, cx, cy, diameter, text, fill=ACCENT, color=WHITE, size=13):
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
    rect(slide, pic.left, pic.top, pic.width, pic.height, fill=None,
         line=line, line_w=1.0, shape=MSO_SHAPE.RECTANGLE)
    return pic


def header(slide, kicker, title, accent=ACCENT):
    textbox(slide, MARGIN_X, Inches(0.35), Inches(10.5), Inches(0.28),
            kicker.upper(), size=11, bold=True, color=accent, spaced=True)
    textbox(slide, MARGIN_X, Inches(0.64), CONTENT_W, Inches(0.55),
            title, size=24, bold=True, color=DK1, font=FONT_LIGHT)
    rect(slide, MARGIN_X, Inches(1.28), CONTENT_W, Pt(1.4), fill=BORDER,
         line=None, shape=MSO_SHAPE.RECTANGLE)


def footer(slide, brand="RAPTOR DEPLOYMENT GATE  \u00b7  TECHNICAL BRIEFING"):
    PAGE["n"] += 1
    rect(slide, MARGIN_X, Inches(7.05), CONTENT_W, Pt(1), fill=FOOT_LINE, line=None,
         shape=MSO_SHAPE.RECTANGLE)
    textbox(slide, MARGIN_X, Inches(7.13), Inches(9), Inches(0.28), brand,
            size=9, color=MUTED, spaced=True)
    textbox(slide, SLIDE_W - MARGIN_X - Inches(1.5), Inches(7.13), Inches(1.5), Inches(0.28),
            f"{PAGE['n']:02d} / {TOTAL_CONTENT_SLIDES:02d}", size=9, color=MUTED,
            align=PP_ALIGN.RIGHT)


def clean_table(slide, left, top, width, height, rows_data, col_widths,
                 header_fill=ACCENT, header_color=WHITE, body_size=12,
                 header_size=12, align_cols=None, font=FONT):
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
            p.font.name = font
            p.font.bold = (ri == 0)
            p.alignment = (align_cols[ci] if align_cols else PP_ALIGN.LEFT)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.12)
            cell.margin_right = Inches(0.12)
            cell.margin_top = Inches(0.05)
            cell.margin_bottom = Inches(0.05)
            cell.fill.solid()
            if ri == 0:
                cell.fill.fore_color.rgb = header_fill
                p.font.color.rgb = header_color
            else:
                cell.fill.fore_color.rgb = PANEL if ri % 2 == 0 else WHITE
                p.font.color.rgb = DK1
    return tbl


def fill_placeholder(slide, idx, text):
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == idx:
            ph.text_frame.text = text
            return ph
    return None


# =============================================================================
# TITLE SLIDE (unnumbered)
# =============================================================================
s = add_slide(TITLE_PAGE)
fill_placeholder(s, 0, "Raptor Deployment Gate")
fill_placeholder(
    s, 1,
    "8-slide technical draft \u2014 use case, architecture, scoring, and live results\n"
    "eric-cenx-rest-api \u00b7 robot-poc namespace \u00b7 October 2026",
)
fill_placeholder(s, 10, "Engineering Briefing")
fill_placeholder(s, 11, "Raptor Deployment Gate")
fill_placeholder(s, 12, "2026-10-22")

# =============================================================================
# SLIDE 1 -- The use case
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "01 \u00b7 Use Case", "Why Raptor Deployment Gate exists")

textbox(s, MARGIN_X, Inches(1.45), CONTENT_W, Inches(0.75),
        "The deployment: a Kubernetes-based platform with multiple microservices "
        "running in namespace robot-poc (e.g. eric-cenx-rest-api), each shipped "
        "independently through CI/CD (GitLab CI / Jenkins + Kaniko in-cluster builds).",
        size=13, color=DK1, line_spacing=1.3)

col_w = Inches(5.55)
gap = Inches(0.3)
left_x = MARGIN_X
right_x = MARGIN_X + col_w + gap

rect(s, left_x, Inches(2.4), col_w, Inches(0.42), fill=RED_BG, line=RED, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
textbox(s, left_x + Inches(0.2), Inches(2.49), col_w - Inches(0.4), Inches(0.26),
        "The problem during a CI/CD upgrade", size=12.5, bold=True, color=RED)
bullet_list(s, left_x, Inches(2.95), col_w, Inches(3.5), [
    "A new version is deployed to production as a canary -- is it actually safe?",
    "Today, a person watches dashboards for a few minutes and makes the call",
    "Judgment varies by who's on call, and subtle multi-metric issues are easy to miss",
    "A bad deployment can sit in production for minutes or hours before anyone notices",
    "A recurring minor issue can keep slipping through, release after release, if each one looks \u201cgood enough\u201d alone",
], dot_color=RED, size=12, gap_pt=10)

rect(s, right_x, Inches(2.4), col_w, Inches(0.42), fill=GREEN_BG, line=GREEN, shape=MSO_SHAPE.ROUNDED_RECTANGLE)
textbox(s, right_x + Inches(0.2), Inches(2.49), col_w - Inches(0.4), Inches(0.26),
        "What Raptor Deployment Gate does", size=12.5, bold=True, color=GREEN)
bullet_list(s, right_x, Inches(2.95), col_w, Inches(3.5), [
    "Runs as its own service inside robot-poc, next to the workloads it protects",
    "Watches the same 9 metrics, scored the same way, on every single deployment",
    "Combines an ML anomaly model with an explicit SLA rule engine into one health score",
    "Remembers recent deployments per service, so a recurring issue can't hide behind one passing sample",
    "Returns one of exactly two verdicts \u2014 PROMOTE or ROLLBACK \u2014 in seconds, as part of the pipeline itself",
], dot_color=GREEN, size=12, gap_pt=10)
footer(s)

# =============================================================================
# SLIDE 2 -- Metrics + collectors
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "02 \u00b7 Metrics & Collectors", "What we capture, and how it's configured")

lx = MARGIN_X
lw = Inches(5.9)
textbox(s, lx, Inches(1.45), lw, Inches(0.3),
        "FOUR INDEPENDENT COLLECTORS, ONE CONFIG FILE", size=10.5, bold=True, color=ACCENT, spaced=True)
bullet_list(s, lx, Inches(1.85), lw, Inches(2.1), [
    "Prometheus, Kubernetes API, Redis, and a live HTTP probe each run on every scoring pass",
    "All four are declared once in gate/config/collectors.yaml \u2014 target namespace/workload, per-source endpoints, and query definitions",
    "MetricAggregator.from_config() only wires up the collectors with a section present, so each environment can mix and match sources",
    "Fail-closed: if a metric can't be collected, the gate returns ROLLBACK outright \u2014 it never guesses",
], size=11.5, gap_pt=7)

code_block(s, lx, Inches(4.15), lw, Inches(2.55), [
    [("target:", MONO_FG)],
    [("  namespace: robot-poc", MONO_FG), ("    workload: eric-cenx-rest-api", MUTED)],
    [("prometheus:", MONO_FG)],
    [("  base_url: http://localhost:9090", MONO_FG)],
    [("  queries:", MONO_FG)],
    [("    cpu_usage: 100 * avg(java_lang_operatingsystem_", MONO_FG)],
    [("        processcpuload{job=\"eric-cenx-rest-api\"})", MONO_FG)],
    [("redis:", MONO_FG)],
    [("  host: localhost      ", MONO_FG), ("latency_samples: 5", MUTED)],
    [("http_probe:", MONO_FG)],
    [("  base_url: http://localhost:8080   ", MONO_FG), ("probe_count: 10", MUTED)],
], size=10.5)

rx = lx + lw + Inches(0.3)
rw = SLIDE_W - MARGIN_X - rx
textbox(s, rx, Inches(1.45), rw, Inches(0.3),
        "THE 9 METRICS AND THEIR SLA VALUES", size=10.5, bold=True, color=ACCENT, spaced=True)
metrics_rows = [
    ("Metric", "Source", "SLA"),
    ("cpu_usage", "Prometheus", "\u2264 80%"),
    ("memory_usage", "Prometheus", "\u2264 75%"),
    ("pod_restarts", "Kubernetes API", "\u2264 3"),
    ("http_error_rate", "Prometheus", "\u2264 1%"),
    ("p99_latency", "InfluxDB", "\u2264 200 ms"),
    ("availability", "InfluxDB", "\u2265 99.5%"),
    ("redis_latency", "Redis INFO", "\u2264 10 ms"),
    ("endpoint_latency", "Network probe", "\u2264 200 ms"),
    ("packet_loss", "Network probe", "\u2264 1%"),
]
clean_table(s, rx, Inches(1.85), rw, Inches(4.85), metrics_rows,
            col_widths=[Emu(int(rw * 0.42)), Emu(int(rw * 0.33)), Emu(int(rw * 0.25))],
            header_size=12.5, body_size=12,
            align_cols=[PP_ALIGN.LEFT, PP_ALIGN.LEFT, PP_ALIGN.CENTER])
footer(s)

# =============================================================================
# SLIDE 3 -- Architecture diagram (left) + components (right)
# Diagram structure/order follows docs/raptor_framework.html chapter 2.
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "03 \u00b7 Architecture", "Raptor Deployment Gate \u2014 end to end")

dg_x = MARGIN_X
dg_w = Inches(6.35)
dg_cx = dg_x + dg_w / 2

rx = dg_x + dg_w + Inches(0.35)
rw = SLIDE_W - MARGIN_X - rx

y0 = Inches(1.45)

# -- Row 1: CI/CD pipeline --
box_w1 = Inches(3.1)
box_x1 = dg_cx - box_w1 / 2
card(s, box_x1, y0, box_w1, Inches(0.48), title="CI/CD pipeline",
     body="GitLab CI / Jenkins", fill=ACCENT_BG, line=ACCENT, title_color=ACCENT_DARK,
     title_size=11.5, body_color=MUTED, body_size=9.5, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

a1_top = y0 + Inches(0.48)
v_arrow(s, dg_cx, a1_top, height=Inches(0.2))
textbox(s, dg_cx + Inches(0.18), a1_top + Inches(0.01), Inches(2.4), Inches(0.18),
        "build & push image", size=7.5, italic=True, color=MUTED)

# -- Row 2: Kaniko build job --
y1 = a1_top + Inches(0.22)
card(s, box_x1, y1, box_w1, Inches(0.48), title="Kaniko build job",
     body="unprivileged in-cluster build \u2192 registry", fill=ACCENT_BG, line=ACCENT,
     title_color=ACCENT_DARK, title_size=11.5, body_color=MUTED, body_size=9,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

a2_top = y1 + Inches(0.48)
v_arrow(s, dg_cx, a2_top, height=Inches(0.2))
textbox(s, dg_cx + Inches(0.18), a2_top + Inches(0.01), Inches(2.0), Inches(0.18),
        "kubectl apply", size=7.5, italic=True, color=MUTED)

# -- Cluster box --
cluster_top = a2_top + Inches(0.22)
cluster_pad = Inches(0.15)
label_h = Inches(0.24)
row_depsvc_h = Inches(0.52)
gap_a = Inches(0.16)
row_collectors_h = Inches(0.62)
gap_b = Inches(0.16)
row_target_h = Inches(0.4)
cluster_bottom_pad = Inches(0.12)
cluster_h = (label_h + Inches(0.05) + row_depsvc_h + gap_a + row_collectors_h +
             gap_b + row_target_h + cluster_bottom_pad)

rect(s, dg_x, cluster_top, dg_w, cluster_h, fill=CLUSTER_BG, line=ACCENT, line_w=1.1)
textbox(s, dg_x + cluster_pad, cluster_top + Inches(0.06), dg_w - 2 * cluster_pad, label_h,
        "KUBERNETES NAMESPACE \u2014 ROBOT-POC", size=8.5, bold=True, color=ACCENT, spaced=True)

inner_x = dg_x + cluster_pad
inner_w = dg_w - 2 * cluster_pad

# Row: Deployment + Service
row1_y = cluster_top + label_h + Inches(0.05)
half_w = Emu(int((inner_w - Inches(0.1)) / 2))
card(s, inner_x, row1_y, half_w, row_depsvc_h, title="Deployment/raptor-gate",
     body="2 replicas \u00b7 FastAPI + Isolation Forest", fill=WHITE, line=BORDER,
     title_color=DK1, title_size=9.5, body_color=MUTED, body_size=8,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, inner_x + half_w + Inches(0.1), row1_y, half_w, row_depsvc_h, title="Service/raptor-gate",
     body="ClusterIP :80 \u2192 container :8080", fill=WHITE, line=BORDER,
     title_color=DK1, title_size=9.5, body_color=MUTED, body_size=8,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

a3_top = row1_y + row_depsvc_h
v_arrow(s, dg_cx, a3_top, height=Inches(0.14), width=Inches(0.18))
textbox(s, dg_cx + Inches(0.15), a3_top - Inches(0.02), Inches(2.3), Inches(0.16),
        "reads live metrics", size=7, italic=True, color=MUTED)

# Row: 4 collectors
row2_y = a3_top + gap_a
col_gap = Inches(0.08)
col_w4 = Emu(int((inner_w - 3 * col_gap) / 4))
collectors = [
    ("Prometheus", "CPU \u00b7 memory"),
    ("Kubernetes API", "pod restarts"),
    ("Redis", "cache latency"),
    ("Live HTTP probe", "latency \u00b7 errors \u00b7 avail. \u00b7 loss"),
]
cx_ = inner_x
for name, sub in collectors:
    card(s, cx_, row2_y, col_w4, row_collectors_h, title=name, body=sub,
         fill=WHITE, line=BORDER, title_color=DK1, title_size=8.5, body_color=MUTED,
         body_size=7, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
    cx_ += col_w4 + col_gap

a4_top = row2_y + row_collectors_h
v_arrow(s, dg_cx, a4_top, height=Inches(0.14), width=Inches(0.18))
textbox(s, dg_cx + Inches(0.15), a4_top - Inches(0.02), Inches(2.3), Inches(0.16),
        "probe targets", size=7, italic=True, color=MUTED)

# Row: target service
row3_y = a4_top + gap_b
target_w = Emu(int(inner_w * 0.78))
target_x = inner_x + Emu(int((inner_w - target_w) / 2))
card(s, target_x, row3_y, target_w, row_target_h, title="Service: eric-cenx-rest-api",
     body="the canary workload being evaluated", fill=AMBER_BG, line=AMBER,
     title_color=AMBER, title_size=9.5, body_color=DK1, body_size=8,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

cluster_bottom = cluster_top + cluster_h

# -- Final arrow + verdict row --
a5_top = cluster_bottom + Inches(0.1)
v_arrow(s, dg_cx, a5_top, height=Inches(0.2))
textbox(s, dg_cx + Inches(1.5), a5_top - Inches(0.02), Inches(3.2), Inches(0.18),
        "exit code 0 / 1, or JSON verdict", size=7.5, italic=True, color=MUTED)

verdict_top = a5_top + Inches(0.22)
vw = Emu(int((dg_w - Inches(0.12)) / 2))
card(s, dg_x, verdict_top, vw, Inches(0.42), title="PROMOTE", fill=GREEN_BG, line=GREEN,
     title_color=GREEN, title_size=11, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, dg_x + vw + Inches(0.12), verdict_top, vw, Inches(0.42), title="ROLLBACK", fill=RED_BG, line=RED,
     title_color=RED, title_size=11, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

# -- Right side: component explanations, aligned to each diagram tier --
components = [
    (y0, "CI/CD pipeline", "GitLab CI / Jenkins triggers the build on every commit to the service."),
    (y1, "Kaniko build job", "Unprivileged in-cluster image build \u2014 no Docker daemon required \u2014 then pushed to the registry."),
    (row1_y, "Deployment + Service", "raptor-gate runs as 2 replicas behind a ClusterIP service, reachable in-cluster by the pipeline."),
    (row2_y, "Four collectors", "Prometheus (CPU/memory), Kubernetes API (restarts), Redis (cache latency), and a live HTTP probe (latency, errors, availability, packet loss)."),
    (row3_y, "Target workload", "eric-cenx-rest-api \u2014 the canary service actually being evaluated this pass."),
    (verdict_top, "Verdict", "Exit code 0 (PROMOTE) or 1 (ROLLBACK), or a full JSON verdict for pipeline consumption."),
]
for y, title, desc in components:
    number_badge(s, rx, y, Inches(0.3), str(components.index((y, title, desc)) + 1),
                 fill=ACCENT, color=WHITE, size=10)
    textbox(s, rx + Inches(0.42), y - Inches(0.02), rw - Inches(0.42), Inches(0.26), title,
            size=11.5, bold=True, color=DK1)
    textbox(s, rx + Inches(0.42), y + Inches(0.24), rw - Inches(0.42), Inches(0.55), desc,
            size=9, color=MUTED, line_spacing=1.15)
footer(s)

# =============================================================================
# SLIDE 4 -- Health score flow (ML + SLA + persistent breach + window volatility)
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "04 \u00b7 Scoring Engine", "How a metrics sample becomes a verdict")

fw = Inches(8.4)
fx = MARGIN_X + (CONTENT_W - fw) / 2
fcx = fx + fw / 2

y = Inches(1.45)
card(s, fx, y, fw, Inches(0.42), title="9-metric sample collected this pass",
     fill=PANEL, line=BORDER, title_color=DK1, title_size=11.5, align=PP_ALIGN.CENTER,
     valign=MSO_ANCHOR.MIDDLE)

y += Inches(0.42)
v_arrow(s, fcx, y, height=Inches(0.16))
y += Inches(0.18)

pair_w = Emu(int((fw - Inches(0.3)) / 2))
card(s, fx, y, pair_w, Inches(0.68), title="Isolation Forest",
     body="n_estimators=200 \u00b7 trained on v1 baseline\nanomaly_health = 0-100",
     fill=ACCENT_BG, line=ACCENT, title_color=ACCENT_DARK, title_size=11, body_color=MUTED,
     body_size=9, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, fx + pair_w + Inches(0.3), y, pair_w, Inches(0.68), title="SLA rule engine",
     body="9 explicit thresholds\neach breach \u2212 20 points",
     fill=ACCENT_BG, line=ACCENT, title_color=ACCENT_DARK, title_size=11, body_color=MUTED,
     body_size=9, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

y += Inches(0.68)
v_arrow(s, fcx, y, height=Inches(0.16))
y += Inches(0.18)

card(s, fx, y, fw, Inches(0.44),
     title="health = clamp(anomaly_health \u2212 20\u00d7violations, 0, 100)",
     fill=AMBER_BG, line=AMBER, title_color=AMBER, title_size=11.5, align=PP_ALIGN.CENTER,
     valign=MSO_ANCHOR.MIDDLE)

y += Inches(0.44)
v_arrow(s, fcx, y, height=Inches(0.16))
textbox(s, fcx + Inches(0.15), y - Inches(0.02), Inches(1.5), Inches(0.16),
        "\u2265 70 ?", size=8, italic=True, color=MUTED)
y += Inches(0.18)

half_w = Emu(int((fw - Inches(0.3)) / 2))
card(s, fx, y, half_w, Inches(0.42), title="< 70 \u2192 ROLLBACK (provisional)", fill=RED_BG, line=RED,
     title_color=RED, title_size=10.5, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, fx + half_w + Inches(0.3), y, half_w, Inches(0.42), title="\u2265 70 \u2192 PROMOTE (provisional)",
     fill=GREEN_BG, line=GREEN, title_color=GREEN, title_size=10.5, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

y += Inches(0.42)
v_arrow(s, fcx, y, height=Inches(0.16))
textbox(s, fcx + Inches(0.15), y - Inches(0.02), Inches(3.0), Inches(0.16),
        "then check two overrides", size=8, italic=True, color=MUTED)
y += Inches(0.18)

card(s, fx, y, half_w, Inches(0.95), title="Signal 3 \u00b7 Persistent breach",
     body="Same metric breached SLA on \u2265 2 of the last 5 samples for this component \u2192 forced ROLLBACK, regardless of score.",
     fill=WHITE, line=AMBER, title_color=AMBER, title_size=10, body_color=DK1, body_size=8.5,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
card(s, fx + half_w + Inches(0.3), y, half_w, Inches(0.95), title="Signal 4 \u00b7 Window volatility",
     body="\u2265 50% of the raw sub-window ticks breached \u2014 even though Mean aggregation hid it \u2192 forced ROLLBACK.",
     fill=WHITE, line=AMBER, title_color=AMBER, title_size=10, body_color=DK1, body_size=8.5,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)

y += Inches(0.95)
v_arrow(s, fcx, y, height=Inches(0.16))
y += Inches(0.18)

card(s, fx, y, fw, Inches(0.42),
     title="Final verdict: either override fires \u2192 ROLLBACK  \u00b7  otherwise the provisional decision stands",
     fill=PANEL, line=BORDER, title_color=DK1, title_size=10.5, align=PP_ALIGN.CENTER,
     valign=MSO_ANCHOR.MIDDLE)
footer(s)

# =============================================================================
# Scenario slide builder (used for slides 5, 6, 7)
# =============================================================================
def scenario_slide(kicker, headline, verdict, color, bg, img, desc, stats, note,
                    footer_note):
    s = add_slide(); set_bg(s)
    header(s, kicker, headline)
    rect(s, SLIDE_W - MARGIN_X - Inches(2.5), Inches(0.42), Inches(2.5), Inches(0.5),
         fill=bg, line=color, line_w=1.25)
    textbox(s, SLIDE_W - MARGIN_X - Inches(2.5), Inches(0.54), Inches(2.5), Inches(0.3),
            verdict, size=13, bold=True, color=color, align=PP_ALIGN.CENTER)

    img_h = Inches(4.75)
    picture_framed(s, img, MARGIN_X, Inches(1.5), img_h)

    rx = MARGIN_X + Inches(5.4)
    rw = SLIDE_W - MARGIN_X - rx
    textbox(s, rx, Inches(1.5), rw, Inches(1.5), desc, size=13, color=DK1, line_spacing=1.3)

    chip_w = (rw - Inches(0.2)) / 2
    chip_h = Inches(0.8)
    gy = Inches(3.2)
    positions = [(rx, gy), (rx + chip_w + Inches(0.2), gy),
                 (rx, gy + chip_h + Inches(0.15)), (rx + chip_w + Inches(0.2), gy + chip_h + Inches(0.15))]
    for (cx2, cy2), (label, value) in zip(positions, stats):
        stat_chip(s, cx2, cy2, chip_w, chip_h, label, value, color=color)

    note_y = gy + 2 * (chip_h + Inches(0.15)) + Inches(0.2)
    card(s, rx, note_y, rw, Inches(1.4), body=note, fill=PANEL, line=BORDER,
         body_color=MUTED, body_size=11, valign=MSO_ANCHOR.MIDDLE)
    footer(s, footer_note)


# =============================================================================
# SLIDE 5 -- Scenario A: PROMOTE only
# =============================================================================
scenario_slide(
    kicker="05 \u00b7 Scenario A", headline="The clean release \u2014 PROMOTE",
    verdict="PROMOTE", color=GREEN, bg=GREEN_BG,
    img=DATA / "live_results_eric-cenx-rest-api_20261021_110134_promote.png",
    desc=("82 consecutive samples, roughly a minute and a half of live traffic. "
          "Every single metric sits comfortably inside its SLA line the entire "
          "time \u2014 memory usage actually settles down partway through, from "
          "~37% to ~7\u201310% \u2014 and the few small blips in Redis latency and "
          "p99 never cross a threshold."),
    stats=[("Samples scored", "82"), ("Verdict every time", "PROMOTE (82/82)"),
           ("Health score range", "78.4 \u2013 93.5 (avg 84.2)"), ("SLA breaches", "0")],
    note="This is the baseline for \u201cnormal.\u201d The gate doesn't just check this "
         "once \u2014 it keeps scoring every few seconds, so a service that's "
         "healthy at minute 1 has to stay healthy at minute 2 to keep clearing "
         "the bar.",
    footer_note="Captured live against eric-cenx-rest-api, robot-poc  \u00b7  Oct 21, 2026",
)

# =============================================================================
# SLIDE 6 -- Scenario B: metrics changed (drift + flickering spike)
# =============================================================================
scenario_slide(
    kicker="06 \u00b7 Scenario B", headline="Drift and a flickering spike",
    verdict="MOSTLY PROMOTE", color=AMBER, bg=AMBER_BG,
    img=DATA / "live_results_eric-cenx-rest-api_20261021_110731_change_values.png",
    desc=("Two kinds of change at once. Memory usage visibly climbs across the "
          "whole window \u2014 from about 20% up to about 24% \u2014 but it never "
          "crosses its 75% SLA line, so on its own that drift doesn't trip "
          "anything. Separately, Redis latency spikes three separate times to "
          "15\u201316ms, each one just over its 10ms limit."),
    stats=[("Samples scored", "63"), ("Verdicts", "60 PROMOTE / 3 ROLLBACK"),
           ("Health score range", "51.9 \u2013 83.9 (avg 77.4)"),
           ("SLA breaches", "3 \u00d7 redis_latency (isolated)")],
    note="Each spike drops that one sample's health score to 51.9 and the gate "
         "returns ROLLBACK for that instant \u2014 then recovers to PROMOTE on the "
         "very next sample. Because the same metric did not repeat the breach "
         "across enough recent samples, the persistent-breach override never "
         "fires here; this reads as transient noise, not a standing problem.",
    footer_note="Captured live against eric-cenx-rest-api, robot-poc  \u00b7  Oct 21, 2026",
)

# =============================================================================
# SLIDE 7 -- Scenario C: ROLLBACK only
# =============================================================================
scenario_slide(
    kicker="07 \u00b7 Scenario C", headline="The degraded canary \u2014 ROLLBACK",
    verdict="ROLLBACK", color=RED, bg=RED_BG,
    img=DATA / "live_results_eric-cenx-rest-api_20261021_130754_rollback.png",
    desc=("The service is visibly struggling: p99 latency repeatedly punches "
          "through its 200ms SLA line \u2014 spiking as high as ~300ms \u2014 twelve "
          "separate times across the run, and Redis latency spikes above its "
          "10ms limit three more times. Unlike Scenario B, this isn't a "
          "one-off flicker; the same latency problem keeps recurring "
          "throughout the window."),
    stats=[("Samples scored", "56"), ("Verdicts", "41 PROMOTE / 15 ROLLBACK"),
           ("Health score range", "51.9 \u2013 78.5 (avg 72.7)"),
           ("SLA breaches", "12 \u00d7 p99_latency, 3 \u00d7 redis_latency")],
    note="A quarter of every sample taken during this window came back as a "
         "breach, and p99_latency recurs well past the persistent-breach "
         "threshold \u2014 the gate reverts it automatically, before a human "
         "would have even finished opening the dashboard.",
    footer_note="Captured live against eric-cenx-rest-api, robot-poc  \u00b7  Oct 21, 2026",
)

# =============================================================================
# SLIDE 8 -- Advantages / comparison
# =============================================================================
s = add_slide(); set_bg(s)
header(s, "08 \u00b7 Advantages", "Why an automatic gate, compared to today")

compare_rows = [
    ("Without an automated gate", "With the Raptor Gate"),
    ("A human watches dashboards after every deploy and decides whether it's safe", "The decision is automatic, consistent, and takes seconds"),
    ("Judgment calls vary by who's on call and how tired they are", "Every deployment is scored against the exact same rules and the exact same model, every time"),
    ("Subtle cross-metric issues are easy to miss individually", "The Isolation Forest is trained specifically to catch combinations no single threshold would flag"),
    ("Bad deployments can sit in production for minutes or hours before anyone notices", "Rollback happens automatically, immediately, as part of the pipeline itself"),
    ("A minor, recurring issue can keep slipping through if each deployment looks \u201cgood enough\u201d alone", "The gate remembers recent deployments and forces a rollback the moment the same metric breaks its SLA repeatedly"),
]
half_col = Emu(int(CONTENT_W / 2))
tbl = clean_table(s, MARGIN_X, Inches(1.55), CONTENT_W, Inches(4.9), compare_rows,
                   col_widths=[half_col, half_col], header_size=13, body_size=11.5,
                   header_fill=DK1)
for ri in range(len(compare_rows)):
    for ci in range(2):
        cell = tbl.cell(ri, ci)
        if ri == 0:
            cell.fill.fore_color.rgb = RED if ci == 0 else GREEN
            cell.text_frame.paragraphs[0].font.color.rgb = WHITE
        else:
            cell.fill.fore_color.rgb = RED_BG if ci == 0 else GREEN_BG
            cell.text_frame.paragraphs[0].font.color.rgb = DK1

card(s, MARGIN_X, Inches(6.55), CONTENT_W, Inches(0.42),
     title="One contract: exit code 0 to promote, exit code 1 to roll back \u2014 fits any existing CI/CD pipeline.",
     fill=ACCENT_BG, line=ACCENT, title_color=ACCENT_DARK, title_size=11.5,
     align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE)
footer(s)

# ------------------------------------------------------------------------- save
prs.save(str(OUT_TMP))
try:
    shutil.move(str(OUT_TMP), str(OUT))
    print(f"Saved: {OUT}  ({len(prs.slides._sldIdLst)} slides)")
except PermissionError:
    print(f"Target locked; wrote to {OUT_TMP} instead. Close any app with the pptx open and retry, "
          f"or rename {OUT_TMP.name} to {OUT.name} manually.")
