#!/usr/bin/env python3
"""Build figures and a publication-ready DOCX from the Wattch paper source.

The document intentionally distinguishes deterministic methodology validation,
workload admissibility, and empirical meter evidence. The first two evidence
classes are present in the current results; the third is not.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt, RGBColor


HERE = Path(__file__).resolve().parent
SOURCE = HERE / "paper.md"
RESULTS = HERE / "results"
FIGURES = HERE / "figures"
OUTPUT = HERE / "Wattch_Context_Dependent_Energy_Impact.docx"

DOCUMENT_SKILL = Path(
    "/Users/chakib/.codex/plugins/cache/openai-primary-runtime/"
    "documents/26.715.12143/skills/documents"
)
sys.path.insert(0, str(DOCUMENT_SKILL / "scripts"))
from table_geometry import apply_table_geometry, column_widths_from_weights  # noqa: E402


NAVY = "17324D"
BLUE = "2E74B5"
MID_BLUE = "4E88C7"
PALE_BLUE = "EAF2F8"
GREEN = "287A62"
PALE_GREEN = "EAF5F0"
AMBER = "B56A18"
PALE_AMBER = "FFF4E5"
RED = "A23B3B"
PALE_RED = "FBECEC"
INK = "263238"
MUTED = "586574"
LIGHT = "F4F6F9"
GRID = "D9E1E8"
WHITE = "FFFFFF"


def _rgb(hex_value: str) -> RGBColor:
    return RGBColor.from_string(hex_value)


def _font(size: int, *, bold: bool = False, italic: bool = False) -> ImageFont.FreeTypeFont:
    if bold:
        path = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    elif italic:
        path = "/System/Library/Fonts/Supplemental/Arial Italic.ttf"
    else:
        path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    return ImageFont.truetype(path, size=size)


def _fit_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        proposed = " ".join([*current, word])
        if current and draw.textbbox((0, 0), proposed, font=font)[2] > max_width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: str,
    max_width: int,
    *,
    spacing: int = 8,
    anchor: str | None = None,
) -> int:
    x, y = xy
    lines = _fit_lines(draw, text, font, max_width)
    line_height = font.size + spacing
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill, anchor=anchor)
        y += line_height
    return y


def _rounded_box(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: str,
    outline: str = GRID,
    radius: int = 20,
    width: int = 3,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _base_figure(title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (1800, 1000), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1800, 16), fill=f"#{BLUE}")
    draw.text((90, 62), title, font=_font(44, bold=True), fill=f"#{NAVY}")
    draw.text((90, 122), subtitle, font=_font(25), fill=f"#{MUTED}")
    return image, draw


def build_figure_1() -> None:
    image, draw = _base_figure(
        "From treatment to an admissible energy-impact claim",
        "A declared meter boundary preserves system interactions; interpretation remains offline.",
    )

    columns = [(70, 220, 530, 820), (670, 220, 1130, 820), (1270, 220, 1730, 820)]
    headings = [
        ("1  Matched interventions", PALE_BLUE, BLUE),
        ("2  Raw daemon evidence", PALE_GREEN, GREEN),
        ("3  Offline inference", PALE_AMBER, AMBER),
    ]
    bodies = [
        [
            ("C0", "Background + sham launcher"),
            ("C1", "Same trace + P1"),
            ("C2", "Same trace + P2"),
            ("BLOCK", "Balanced order, seed, host state"),
        ],
        [
            ("METER", "RAPL or powermetrics source"),
            ("CLOCK", "Daemon-stamped lifecycle events"),
            ("TRACE", "Immutable samples + descriptors"),
            ("QUALITY", "Gaps, wrap, drift, topology"),
        ],
        [
            ("EFFECT", "Target minus matched sham"),
            ("CONTRAST", "P1 minus P2 within context"),
            ("REVERSAL", "Opposite isolated/co-tenant signs"),
            ("GATES", "Equal work, SLO, A/A, replication"),
        ],
    ]

    for box, (heading, pale, accent), items in zip(columns, headings, bodies, strict=True):
        _rounded_box(draw, box, fill=f"#{pale}", outline=f"#{accent}", width=4)
        draw.text((box[0] + 28, box[1] + 28), heading, font=_font(29, bold=True), fill=f"#{accent}")
        y = box[1] + 104
        for label, body in items:
            draw.rounded_rectangle(
                (box[0] + 28, y, box[2] - 28, y + 92),
                radius=14,
                fill="#FFFFFF",
                outline=f"#{GRID}",
                width=2,
            )
            draw.text((box[0] + 48, y + 17), label, font=_font(19, bold=True), fill=f"#{accent}")
            _draw_wrapped(
                draw,
                (box[0] + 48, y + 46),
                body,
                _font(19),
                f"#{INK}",
                box[2] - box[0] - 96,
                spacing=5,
            )
            y += 112

    for left, right in ((columns[0], columns[1]), (columns[1], columns[2])):
        start = (left[2] + 18, 520)
        end = (right[0] - 18, 520)
        draw.line((*start, *end), fill=f"#{BLUE}", width=8)
        draw.polygon(
            [(end[0], end[1]), (end[0] - 22, end[1] - 14), (end[0] - 22, end[1] + 14)],
            fill=f"#{BLUE}",
        )

    draw.rounded_rectangle((410, 876, 1390, 946), radius=18, fill=f"#{PALE_RED}")
    draw.text(
        (900, 911),
        "No per-process joule allocation · No cross-boundary pooling · No claim without all gates",
        font=_font(22, bold=True),
        fill=f"#{RED}",
        anchor="mm",
    )
    image.save(FIGURES / "figure-1-study-design.png", dpi=(180, 180))


def _axis(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    minimum: float,
    maximum: float,
    ticks: list[float],
    label: str,
) -> callable:
    x0, y0, x1, y1 = box

    def to_y(value: float) -> int:
        return int(y1 - (value - minimum) / (maximum - minimum) * (y1 - y0))

    for tick in ticks:
        y = to_y(tick)
        draw.line((x0, y, x1, y), fill=f"#{GRID}", width=2)
        draw.text((x0 - 16, y), f"{tick:g}", font=_font(18), fill=f"#{MUTED}", anchor="rm")
    draw.line((x0, y0, x0, y1), fill=f"#{INK}", width=3)
    draw.line((x0, y1, x1, y1), fill=f"#{INK}", width=3)
    draw.text((x0, y0 - 40), label, font=_font(19, bold=True), fill=f"#{INK}")
    return to_y


def build_figure_2() -> None:
    data = json.loads((RESULTS / "deterministic_validation.json").read_text(encoding="utf-8"))
    reversal = data["cases"]["interference_reversal"]["observed"]
    dvfs = data["cases"]["low_priority_dvfs"]["observed"]
    image, draw = _base_figure(
        "Deterministic verification recovers the planted reversal",
        "These scenario truths verify estimand calculations; they are not observations of physical hardware.",
    )

    left = (130, 290, 800, 800)
    to_y = _axis(draw, left, -0.5, 0.5, [-0.5, -0.25, 0, 0.25, 0.5], "P1 − P2 (joules per tick)")
    values = [
        reversal["isolated_p1_minus_p2_joules_per_tick"],
        reversal["background_p1_minus_p2_joules_per_tick"],
    ]
    labels = ["Isolated", "Co-tenant"]
    colors = [f"#{BLUE}", f"#{GREEN}"]
    zero = to_y(0)
    for idx, (value, label, color) in enumerate(zip(values, labels, colors, strict=True)):
        x = 260 + idx * 300
        y = to_y(value)
        draw.rounded_rectangle((x, min(y, zero), x + 150, max(y, zero)), radius=8, fill=color)
        draw.text((x + 75, y - 18 if value >= 0 else y + 18), f"{value:+.2f}", font=_font(24, bold=True), fill=color, anchor="ms" if value >= 0 else "ma")
        draw.text((x + 75, 842), label, font=_font(23, bold=True), fill=f"#{INK}", anchor="mm")

    right = (1010, 290, 1690, 800)
    to_y2 = _axis(draw, right, 0, 14, [0, 2, 4, 6, 8, 10, 12, 14], "Equal-work energy (joules)")
    groups = [
        ("Dynamic", dvfs["dynamic_normal_joules"], dvfs["dynamic_low_priority_joules"]),
        ("Fixed", dvfs["fixed_normal_joules"], dvfs["fixed_low_priority_joules"]),
    ]
    for idx, (label, normal, low) in enumerate(groups):
        center = 1195 + idx * 310
        for offset, value, color in ((-72, normal, f"#{BLUE}"), (32, low, f"#{AMBER}")):
            x = center + offset
            y = to_y2(value)
            draw.rounded_rectangle((x, y, x + 84, right[3]), radius=7, fill=color)
            draw.text((x + 42, y - 14), f"{value:.1f}", font=_font(21, bold=True), fill=color, anchor="ms")
        draw.text((center + 2, 842), label, font=_font(23, bold=True), fill=f"#{INK}", anchor="mm")

    draw.rounded_rectangle((1060, 884, 1640, 944), radius=16, fill=f"#{LIGHT}")
    draw.rectangle((1100, 902, 1130, 927), fill=f"#{BLUE}")
    draw.text((1145, 915), "nice 0", font=_font(20), fill=f"#{INK}", anchor="lm")
    draw.rectangle((1320, 902, 1350, 927), fill=f"#{AMBER}")
    draw.text((1365, 915), "nice +10", font=_font(20), fill=f"#{INK}", anchor="lm")
    image.save(FIGURES / "figure-2-deterministic-results.png", dpi=(180, 180))


def build_figure_3() -> None:
    pilot = json.loads((RESULTS / "workload_pilot.json").read_text(encoding="utf-8"))
    image, draw = _base_figure(
        "Polling and batching complete equivalent work",
        "Mean p99 latency across 12 blocks; error bars show one sample standard deviation. No energy outcome is shown.",
    )

    plot = (160, 280, 1680, 800)
    to_y = _axis(draw, plot, 0, 16, [0, 2, 4, 6, 8, 10, 12, 14, 16], "Target p99 latency (ms)")
    arms = [("C0 sham", BLUE), ("C1 batching", GREEN), ("C2 polling", AMBER)]
    contexts = ["isolated", "cotenant"]
    group_centers = [560, 1280]
    bar_width = 105

    for context, center in zip(contexts, group_centers, strict=True):
        for idx, (arm_label, color) in enumerate(arms):
            arm = f"C{idx}"
            stats = pilot["summaries"][context][arm]["p99_latency_ms"]
            mean = float(stats["mean"])
            sd = float(stats["sample_sd"])
            x = center + (idx - 1) * 180
            y = to_y(mean)
            draw.rounded_rectangle((x - bar_width // 2, y, x + bar_width // 2, plot[3]), radius=7, fill=f"#{color}")
            high = to_y(min(16, mean + sd))
            low = to_y(max(0, mean - sd))
            draw.line((x, high, x, low), fill=f"#{INK}", width=4)
            draw.line((x - 18, high, x + 18, high), fill=f"#{INK}", width=4)
            draw.line((x - 18, low, x + 18, low), fill=f"#{INK}", width=4)
            draw.text((x, y - 14), f"{mean:.2f}", font=_font(20, bold=True), fill=f"#{color}", anchor="ms")
        draw.text((center, 845), "Isolated" if context == "isolated" else "Co-tenant", font=_font(25, bold=True), fill=f"#{INK}", anchor="mm")

    x = 405
    for label, color in arms:
        draw.rectangle((x, 898, x + 30, 924), fill=f"#{color}")
        draw.text((x + 44, 911), label, font=_font(20), fill=f"#{INK}", anchor="lm")
        x += 350
    image.save(FIGURES / "figure-3-workload-pilot.png", dpi=(180, 180))


def build_figures() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    build_figure_1()
    build_figure_2()
    build_figure_3()


def _set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_paragraph_shading(paragraph, fill: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    shd = p_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        p_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _set_paragraph_border(paragraph, *, color: str, side: str = "left", size: int = 18) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = p_pr.find(qn("w:pBdr"))
    if p_bdr is None:
        p_bdr = OxmlElement("w:pBdr")
        p_pr.append(p_bdr)
    border = OxmlElement(f"w:{side}")
    border.set(qn("w:val"), "single")
    border.set(qn("w:sz"), str(size))
    border.set(qn("w:space"), "8")
    border.set(qn("w:color"), color)
    p_bdr.append(border)


def _set_cell_border(cell, *, bottom_color: str = GRID) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.find(qn("w:tcBorders"))
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:color"), bottom_color)
    borders.append(bottom)


def _set_repeat_table_header(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    tr_pr.append(header)


def _prevent_row_split(row) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def _set_update_fields(document: Document) -> None:
    settings = document.settings._element
    update_fields = settings.find(qn("w:updateFields"))
    if update_fields is None:
        update_fields = OxmlElement("w:updateFields")
        settings.append(update_fields)
    update_fields.set(qn("w:val"), "true")


def _remove_child(parent, tag: str) -> None:
    child = parent.find(qn(tag))
    if child is not None:
        parent.remove(child)


def _new_decimal_numbering(document: Document) -> int:
    """Create a real single-level decimal list that restarts at one."""

    numbering = document.part.numbering_part.element
    abstract_ids = [
        int(item.get(qn("w:abstractNumId")))
        for item in numbering.findall(qn("w:abstractNum"))
    ]
    num_ids = [
        int(item.get(qn("w:numId"))) for item in numbering.findall(qn("w:num"))
    ]
    abstract_id = max(abstract_ids, default=-1) + 1
    num_id = max(num_ids, default=0) + 1

    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    multi = OxmlElement("w:multiLevelType")
    multi.set(qn("w:val"), "singleLevel")
    abstract.append(multi)
    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:start")
    start.set(qn("w:val"), "1")
    number_format = OxmlElement("w:numFmt")
    number_format.set(qn("w:val"), "decimal")
    level_text = OxmlElement("w:lvlText")
    level_text.set(qn("w:val"), "%1.")
    suffix = OxmlElement("w:suff")
    suffix.set(qn("w:val"), "space")
    p_pr = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "num")
    tab.set(qn("w:pos"), "720")
    tabs.append(tab)
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), "720")
    indent.set(qn("w:hanging"), "360")
    p_pr.extend([tabs, indent])
    level.extend([start, number_format, level_text, suffix, p_pr])
    abstract.append(level)
    numbering.append(abstract)

    number = OxmlElement("w:num")
    number.set(qn("w:numId"), str(num_id))
    abstract_ref = OxmlElement("w:abstractNumId")
    abstract_ref.set(qn("w:val"), str(abstract_id))
    number.append(abstract_ref)
    numbering.append(number)
    return num_id


def _add_numbered_paragraph(document: Document, text: str, num_id: int) -> None:
    paragraph = document.add_paragraph(style="List Paragraph")
    paragraph.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    paragraph.paragraph_format.space_after = Pt(4)
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = OxmlElement("w:numPr")
    level = OxmlElement("w:ilvl")
    level.set(qn("w:val"), "0")
    number = OxmlElement("w:numId")
    number.set(qn("w:val"), str(num_id))
    num_pr.extend([level, number])
    p_pr.append(num_pr)
    _add_inline(paragraph, text)


def _add_page_field(paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instruction, separate, text, end])


def _add_hyperlink(paragraph, url: str, display: str) -> None:
    rel_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    run_pr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), BLUE)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    run_pr.extend([color, underline])
    text = OxmlElement("w:t")
    text.text = display
    run.extend([run_pr, text])
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _link_label(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc == "doi.org":
        return f"DOI {parsed.path.lstrip('/')}"
    if "usenix.org" in parsed.netloc:
        return "USENIX paper"
    if "kernel.org" in parsed.netloc:
        return "Linux kernel documentation"
    if "github.io" in parsed.netloc and "powermetrics" in parsed.path:
        return "powermetrics manual"
    return f"Source at {parsed.netloc}"


INLINE_RE = re.compile(
    r"(https?://\S+|`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*)"
)


def _add_inline(paragraph, text: str, *, font_size: float | None = None) -> None:
    cursor = 0
    for match in INLINE_RE.finditer(text):
        if match.start() > cursor:
            run = paragraph.add_run(text[cursor : match.start()])
            if font_size:
                run.font.size = Pt(font_size)
        token = match.group(0)
        if token.startswith("http"):
            trailing = ""
            while token and token[-1] in ".,;":
                trailing = token[-1] + trailing
                token = token[:-1]
            _add_hyperlink(paragraph, token, _link_label(token))
            if trailing:
                paragraph.add_run(trailing)
        elif token.startswith("`"):
            run = paragraph.add_run(token[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(font_size or 9.5)
            run.font.color.rgb = _rgb(NAVY)
        elif token.startswith("**"):
            run = paragraph.add_run(token[2:-2])
            run.bold = True
            if font_size:
                run.font.size = Pt(font_size)
        else:
            run = paragraph.add_run(token[1:-1])
            run.italic = True
            if font_size:
                run.font.size = Pt(font_size)
        cursor = match.end()
    if cursor < len(text):
        run = paragraph.add_run(text[cursor:])
        if font_size:
            run.font.size = Pt(font_size)


def _shade_run(run, fill: str) -> None:
    r_pr = run._r.get_or_add_rPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    r_pr.append(shd)


def _configure_styles(document: Document) -> None:
    styles = document.styles

    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.font.color.rgb = _rgb(INK)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
    normal.paragraph_format.line_spacing = 1.333
    normal.paragraph_format.widow_control = True

    heading_tokens = {
        "Heading 1": (16, BLUE, 18, 10),
        "Heading 2": (13, BLUE, 12, 6),
        "Heading 3": (12, "1F4D78", 8, 4),
    }
    for name, (size, color, before, after) in heading_tokens.items():
        style = styles[name]
        style.font.name = "Calibri"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = _rgb(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.keep_together = True

    title = styles["Title"]
    title.font.name = "Calibri"
    title.font.size = Pt(28)
    title.font.bold = True
    title.font.color.rgb = _rgb(NAVY)
    title.paragraph_format.space_after = Pt(16)
    title.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    title.paragraph_format.keep_with_next = True
    _remove_child(title.element.get_or_add_pPr(), "w:pBdr")

    subtitle = styles["Subtitle"]
    subtitle.font.name = "Calibri"
    subtitle.font.size = Pt(15)
    subtitle.font.color.rgb = _rgb(BLUE)
    subtitle.paragraph_format.space_after = Pt(24)
    subtitle.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    subtitle.paragraph_format.keep_with_next = True
    _remove_child(subtitle.element.get_or_add_pPr(), "w:numPr")

    for list_style in ("List Bullet", "List Number"):
        style = styles[list_style]
        style.font.name = "Calibri"
        style.font.size = Pt(11)
        style.font.color.rgb = _rgb(INK)
        style.paragraph_format.space_after = Pt(4)
        style.paragraph_format.line_spacing = 1.15
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT

    if "Figure Caption" not in [style.name for style in styles]:
        caption = styles.add_style("Figure Caption", WD_STYLE_TYPE.PARAGRAPH)
    else:
        caption = styles["Figure Caption"]
    caption.font.name = "Calibri"
    caption.font.size = Pt(9)
    caption.font.italic = True
    caption.font.color.rgb = _rgb(MUTED)
    caption.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    caption.paragraph_format.space_before = Pt(3)
    caption.paragraph_format.space_after = Pt(10)
    caption.paragraph_format.keep_with_next = False

    if "Table Caption" not in [style.name for style in styles]:
        table_caption = styles.add_style("Table Caption", WD_STYLE_TYPE.PARAGRAPH)
    else:
        table_caption = styles["Table Caption"]
    table_caption.font.name = "Calibri"
    table_caption.font.size = Pt(9)
    table_caption.font.bold = True
    table_caption.font.color.rgb = _rgb(MUTED)
    table_caption.paragraph_format.space_before = Pt(6)
    table_caption.paragraph_format.space_after = Pt(4)
    table_caption.paragraph_format.keep_with_next = True

    if "Equation" not in [style.name for style in styles]:
        equation = styles.add_style("Equation", WD_STYLE_TYPE.PARAGRAPH)
    else:
        equation = styles["Equation"]
    equation.font.name = "Cambria Math"
    equation.font.size = Pt(10.5)
    equation.font.italic = True
    equation.font.color.rgb = _rgb(NAVY)
    equation.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    equation.paragraph_format.space_before = Pt(5)
    equation.paragraph_format.space_after = Pt(8)
    equation.paragraph_format.keep_together = True

    if "Reference" not in [style.name for style in styles]:
        reference = styles.add_style("Reference", WD_STYLE_TYPE.PARAGRAPH)
    else:
        reference = styles["Reference"]
    reference.font.name = "Calibri"
    reference.font.size = Pt(9)
    reference.font.color.rgb = _rgb(INK)
    reference.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    reference.paragraph_format.left_indent = Inches(0.25)
    reference.paragraph_format.first_line_indent = Inches(-0.25)
    reference.paragraph_format.space_after = Pt(5)
    reference.paragraph_format.line_spacing = 1.05


def _configure_page(document: Document) -> None:
    section = document.sections[0]
    section.start_type = WD_SECTION_START.NEW_PAGE
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(1.2)
    section.bottom_margin = Inches(1)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.header_distance = Inches(0.492)
    section.footer_distance = Inches(0.492)
    section.different_first_page_header_footer = True

    header = section.header
    header_p = header.paragraphs[0]
    header_p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    header_p.paragraph_format.space_after = Pt(0)
    run = header_p.add_run("WATTCH  ·  CONTEXT-DEPENDENT ENERGY IMPACT")
    run.font.name = "Calibri"
    run.font.size = Pt(8)
    run.font.bold = True
    run.font.color.rgb = _rgb(MUTED)

    footer = section.footer
    footer_p = footer.paragraphs[0]
    footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer_p.paragraph_format.space_after = Pt(0)
    run = footer_p.add_run("Research preprint  ·  19 July 2026  ·  ")
    run.font.name = "Calibri"
    run.font.size = Pt(8)
    run.font.color.rgb = _rgb(MUTED)
    _add_page_field(footer_p)


def _add_cover(document: Document) -> None:
    eyebrow = document.add_paragraph()
    eyebrow.paragraph_format.space_after = Pt(18)
    run = eyebrow.add_run("WATTCH  /  SYSTEMS ENERGY RESEARCH")
    run.font.name = "Calibri"
    run.font.size = Pt(9)
    run.font.bold = True
    run.font.color.rgb = _rgb(BLUE)

    title = document.add_paragraph(style="Title")
    title.add_run("Can Program Energy Rankings Reverse Under Co-tenancy?")

    subtitle = document.add_paragraph(style="Subtitle")
    subtitle.add_run(
        "A Protocol-First Causal Study Design and Preliminary Framework Verification with Wattch"
    )

    metadata = document.add_paragraph()
    metadata.alignment = WD_ALIGN_PARAGRAPH.LEFT
    metadata.paragraph_format.space_after = Pt(24)
    _add_inline(
        metadata,
        "**Chakib Belgaid**\nWattch Project\nResearch preprint / Stage 1-style preregistration draft\n19 July 2026",
    )

    evidence = document.add_paragraph()
    evidence.alignment = WD_ALIGN_PARAGRAPH.LEFT
    evidence.paragraph_format.left_indent = Inches(0.18)
    evidence.paragraph_format.right_indent = Inches(0.18)
    evidence.paragraph_format.space_before = Pt(8)
    evidence.paragraph_format.space_after = Pt(18)
    _set_paragraph_shading(evidence, PALE_AMBER)
    _set_paragraph_border(evidence, color=AMBER)
    run = evidence.add_run("EVIDENCE STATUS\n")
    run.bold = True
    run.font.size = Pt(9)
    run.font.color.rgb = _rgb(AMBER)
    run = evidence.add_run(
        "This paper verifies estimand calculations and workload admissibility, but does not report a real-hardware energy effect. "
        "The ranking-reversal hypothesis remains open until a calibrated privileged meter trial is completed."
    )
    run.font.size = Pt(11)
    run.font.color.rgb = _rgb(INK)

    snapshot = document.add_paragraph()
    snapshot.alignment = WD_ALIGN_PARAGRAPH.LEFT
    snapshot.paragraph_format.space_before = Pt(18)
    snapshot.paragraph_format.space_after = Pt(3)
    run = snapshot.add_run("PRELIMINARY EVIDENCE SNAPSHOT")
    run.font.size = Pt(9)
    run.bold = True
    run.font.color.rgb = _rgb(MUTED)

    for count, text, color in (
        ("6 / 6", "deterministic falsification scenarios passed", GREEN),
        ("72", "polling-versus-batching target trials completed", BLUE),
        ("0", "empirical meter-backed energy outcomes", RED),
    ):
        p = document.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        p.paragraph_format.left_indent = Inches(0.14)
        p.paragraph_format.space_after = Pt(4)
        count_run = p.add_run(count)
        count_run.bold = True
        count_run.font.size = Pt(14)
        count_run.font.color.rgb = _rgb(color)
        p.add_run(f"   {text}")

    document.add_page_break()


def _pretty_equation(raw: str) -> str:
    normalized = " ".join(line.strip() for line in raw.splitlines())
    replacements = {
        "tau_P(B, pi, H, M) = Y_M(P, B, pi, H) - Y_M(empty, B, pi, H).":
            "τP(B, π, H, M) = YM(P, B, π, H) − YM(∅, B, π, H)",
        "D(B) = tau_{P1}(B, pi, H, M) - tau_{P2}(B, pi, H, M).":
            "D(B) = τP1(B, π, H, M) − τP2(B, π, H, M)",
        "D(idle) > 0 \\quad and \\quad D(B) < 0,":
            "D(idle) > 0    and    D(B) < 0",
        "Delta_{min} = max(0.05 \\times mean(C0\\ energy),\\ 2 \\times A/A\\ uncertainty).":
            "Δmin = max(0.05 × mean C0 energy, 2 × A/A uncertainty)",
        "delta_{DiD} = D_{dynamic} - D_{fixed}.":
            "δDiD = Ddynamic − Dfixed",
    }
    return replacements.get(normalized, normalized)


def _add_picture(document: Document, path: Path, alt_text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(2)
    paragraph.paragraph_format.keep_together = True
    run = paragraph.add_run()
    inline_shape = run.add_picture(str(path), width=Inches(6.22))
    doc_pr = inline_shape._inline.docPr
    doc_pr.set("descr", alt_text)
    doc_pr.set("title", alt_text)


def _add_table(document: Document, rows: list[list[str]], caption_text: str) -> None:
    caption = document.add_paragraph(style="Table Caption")
    caption.add_run(caption_text)

    columns = len(rows[0])
    table = document.add_table(rows=len(rows), cols=columns)
    table.style = "Table Grid"
    table.alignment = 0
    table.rows[0].height = None
    _set_repeat_table_header(table.rows[0])

    for row_idx, values in enumerate(rows):
        row = table.rows[row_idx]
        _prevent_row_split(row)
        for col_idx, value in enumerate(values):
            cell = row.cells[col_idx]
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            if row_idx == 0:
                _set_cell_shading(cell, NAVY)
                run = paragraph.add_run(value)
                run.bold = True
                run.font.color.rgb = _rgb(WHITE)
                run.font.size = Pt(8.5)
            else:
                _add_inline(paragraph, value, font_size=8.5)
                if row_idx % 2 == 0:
                    _set_cell_shading(cell, LIGHT)
                _set_cell_border(cell)

    if columns == 3:
        weights = [1.25, 1.75, 3.0]
    elif columns == 5:
        weights = [1.1, 1.1, 1.35, 1.5, 2.65]
    else:
        weights = [1.0] * columns
    widths = column_widths_from_weights(weights, total_width_dxa=9360)
    apply_table_geometry(
        table,
        widths,
        table_width_dxa=9360,
        indent_dxa=120,
        cell_margins_dxa={"top": 80, "bottom": 80, "start": 120, "end": 120},
    )
    document.add_paragraph().paragraph_format.space_after = Pt(0)


def _flush_paragraph(document: Document, buffer: list[str], *, references: bool) -> None:
    if not buffer:
        return
    text = " ".join(line.strip() for line in buffer).strip()
    buffer.clear()
    if not text:
        return
    style = "Reference" if references else "Normal"
    paragraph = document.add_paragraph(style=style)
    _add_inline(paragraph, text)


def _body_lines() -> list[str]:
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "## Abstract":
            return lines[index:]
    raise RuntimeError("paper source does not contain an Abstract heading")


def _build_body(document: Document) -> None:
    lines = _body_lines()
    paragraph_buffer: list[str] = []
    table_rows: list[list[str]] = []
    equation_lines: list[str] = []
    in_equation = False
    references = False
    pending_figure_caption = False
    table_counter = 0
    numbered_list_id: int | None = None

    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if stripped == "$$":
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            if in_equation:
                equation = document.add_paragraph(style="Equation")
                equation.add_run(_pretty_equation("\n".join(equation_lines)))
                equation_lines.clear()
                in_equation = False
            else:
                in_equation = True
            index += 1
            continue

        if in_equation:
            equation_lines.append(raw)
            index += 1
            continue

        if stripped.startswith("|"):
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                index += 1
                continue
            table_rows.append(cells)
            index += 1
            continue
        if table_rows:
            table_counter += 1
            if table_rows[0][0] == "Case":
                caption = "Table 1. Deterministic implementation verification against planted truths."
            elif table_rows[0][0] == "Context":
                caption = "Table 2. Polling-versus-batching workload-admissibility pilot."
            else:
                caption = f"Table {table_counter}. Study results."
            _add_table(document, table_rows, caption)
            table_rows = []

        image_match = re.fullmatch(r"!\[([^]]+)]\(([^)]+)\)", stripped)
        if image_match:
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            _add_picture(document, HERE / image_match.group(2), image_match.group(1))
            pending_figure_caption = True
            index += 1
            continue

        if stripped.startswith("**Figure ") and pending_figure_caption:
            caption = document.add_paragraph(style="Figure Caption")
            _add_inline(caption, stripped)
            pending_figure_caption = False
            index += 1
            continue

        if stripped.startswith("### "):
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            document.add_heading(stripped[4:], level=2)
            index += 1
            continue
        if stripped.startswith("## "):
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            heading = stripped[3:]
            document.add_heading(heading, level=1)
            references = heading == "References"
            index += 1
            continue

        if re.match(r"^\d+\.\s+", stripped):
            _flush_paragraph(document, paragraph_buffer, references=references)
            text = re.sub(r"^\d+\.\s+", "", stripped)
            if numbered_list_id is None:
                numbered_list_id = _new_decimal_numbering(document)
            _add_numbered_paragraph(document, text, numbered_list_id)
            index += 1
            continue

        if stripped.startswith("- "):
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            paragraph = document.add_paragraph(style="List Bullet")
            _add_inline(paragraph, stripped[2:])
            index += 1
            continue

        if stripped.startswith("> "):
            numbered_list_id = None
            _flush_paragraph(document, paragraph_buffer, references=references)
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Inches(0.18)
            paragraph.paragraph_format.right_indent = Inches(0.18)
            _set_paragraph_shading(paragraph, PALE_AMBER)
            _set_paragraph_border(paragraph, color=AMBER)
            _add_inline(paragraph, stripped[2:])
            index += 1
            continue

        if stripped == "":
            _flush_paragraph(document, paragraph_buffer, references=references)
            numbered_list_id = None
        else:
            paragraph_buffer.append(stripped)
        index += 1

    _flush_paragraph(document, paragraph_buffer, references=references)
    if table_rows:
        table_counter += 1
        _add_table(document, table_rows, f"Table {table_counter}. Study results.")


def build_docx() -> None:
    document = Document()
    _configure_page(document)
    _configure_styles(document)
    _set_update_fields(document)
    document.core_properties.title = "Can Program Energy Rankings Reverse Under Co-tenancy?"
    document.core_properties.subject = "A causal study design and preliminary Wattch validation"
    document.core_properties.author = "Chakib Belgaid"
    document.core_properties.keywords = (
        "software energy, causal inference, co-tenancy, RAPL, powermetrics, Wattch"
    )
    document.core_properties.comments = (
        "Evidence status: deterministic methodology validation and workload-admissibility pilot only."
    )
    _add_cover(document)
    _build_body(document)
    document.save(OUTPUT)


def main() -> None:
    build_figures()
    build_docx()
    print(OUTPUT)


if __name__ == "__main__":
    main()
