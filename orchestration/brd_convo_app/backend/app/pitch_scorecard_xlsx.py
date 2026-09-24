"""
pitch_scorecard_xlsx.py — render a scorecard as the team's own workbook.

The Sales Enablement team already works in "Pitch Checklist_v1.3.xlsx". This
writes the same two tabs, with the same look, so a reviewer can carry on in
Excel, mail it, or diff it against a hand-filled one.

THE EXPORT IS LIVE, NOT A PICTURE OF THE NUMBERS. Column H holds the rating and
column I derives the score from it:

    =IF(H4="✅ Best Practice",2,IF(H4="⚠️ Minimally Meets",1,IF(H4="❌ Does Not Meet",0,"—")))

and the summary tab averages column I. Change a rating in Excel and the whole
scorecard recalculates, exactly as it does in the source workbook. That is why
the rating strings carry their emoji — the formula compares against them
literally, so "Best Practice" without the ✅ silently scores as "—".

It also explains the N/A mechanism: the IF chain emits the *text* "—", and
Excel's AVERAGE skips text. Nothing special is needed to exclude an N/A row —
but a category where *every* row is N/A averages nothing at all, which is
#DIV/0!, so every summary formula is IFERROR-guarded. See _build_summary.

Styling is taken from the source workbook: the three anchor columns carry
Excel's standard status palette (red / amber / green), the rating and score
columns get the same palette through conditional formatting so they recolour
when someone edits a rating in Excel, and row heights are estimated from content
because openpyxl cannot autofit — an unset height clips a wrapped comment to one
line, which is the one thing that makes the export useless.

Row geometry is taken from pitch_scorecard's parsed rubric, which asserts at load
that it still matches the workbook's layout — see _EXPECTED_ROW_SPANS there.
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app import pitch_scorecard as ps

# ── Palette, from the source workbook ────────────────────────────────────────
# The three status colours are Excel's own Good/Neutral/Bad cell styles, which is
# what the rubric's authors used.
RED_FILL, RED_INK = "FFFFC7CE", "FF9C0006"
AMBER_FILL, AMBER_INK = "FFFFEB9C", "FF9C5700"
GREEN_FILL, GREEN_INK = "FFC6EFCE", "FF006100"

NAVY = "FF0E2841"        # checklist title bar
NAVY_DEEP = "FF1F3864"   # summary title + TOTAL row
BLUE = "FF2E5496"        # header rows
BAND_FILL = "FFD9E1F2"   # the readiness band cell
GREY_ROW = "FFE8E8E8"    # criterion name + comment columns
GREY_LIGHT = "FFF2F2F2"  # legend + summary body rows
WHITE = "FFFFFFFF"
INK = "FF1A1A1A"

FONT_NAME = "Aptos Narrow"
FONT_SIZE = 11

_THIN = Side(style="thin", color="FFBFBFBF")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

_WRAP_L = Alignment(wrap_text=True, vertical="center", horizontal="left")
_WRAP_C = Alignment(wrap_text=True, vertical="center", horizontal="center")
_WRAP_TL = Alignment(wrap_text=True, vertical="top", horizontal="left")

CHECKLIST_SHEET = "Pitch Checklist"
SUMMARY_SHEET = "Summary and Scoring"

CHECKLIST_HEADERS = [
    "Category", "Checklist Description", "Criteria",
    "❌ Does Not Meet", "⚠️ Minimally Meets", "✅ Best Practice",
    "Evaluation Rating", "Score (0–2)", "Comments / Feedback",
]
SUMMARY_HEADERS = [
    "Category", "Weight %", "Avg Score (0-2)", "Score %", "Weighted Score %", "Notes",
]

# Column B..J. F, G and I are unset in the source workbook, which leaves the two
# widest anchor columns at Excel's 8.43 default — an oversight, so they get E's
# width. J is widened from the source's 29.71 because our comments carry the
# finding, the recommended fix and the evidence, and a narrow column turns that
# into very tall rows.
CHECKLIST_WIDTHS = {
    "A": 3.14, "B": 21.86, "C": 38.0, "D": 22.0, "E": 28.0,
    "F": 28.0, "G": 28.0, "H": 16.0, "I": 9.0, "J": 52.0,
}
SUMMARY_WIDTHS = {"A": 4.0, "B": 38.0, "C": 14.0, "D": 15.0, "E": 12.0, "F": 16.0, "G": 44.0}

LEGEND = (
    "Scoring: ✅ Best Practice = 2  |  ⚠️ Minimally Meets = 1  |  "
    "❌ Does Not Meet = 0  |  Not Applicable = excluded from the average. "
    "Change a rating in column H and the Score and the Summary tab recalculate "
    "automatically."
)
PAGE_HEADER = "Bristlecone Pitch Check List"

NOTE_LINES = [
    "Notes: (1) If a whole category is Not Applicable for a deal (e.g. PARTNERSHIPS on "
    "a pitch with no partner), set its Weight % to 0 and redistribute across the others "
    "so weights still total 100%.",
    "(2) Weights are a starting suggestion — tune them to your deal's priorities.",
]

# Row-height estimation. Excel's width unit is roughly one character wide, and
# the ratio is deliberately under 1 so the estimate errs tall: a row a few
# points too deep costs nothing, a row too short hides the reviewer's finding.
_CHARS_PER_WIDTH = 1.0
# Aptos Narrow 11pt wraps at about 14 points of leading. The line COUNT is
# simulated exactly by _lines_needed, so this needs no fudge factor on top —
# earlier versions inflated it only to compensate for under-counting lines.
_LINE_PT = 14.0
# A small cushion for the cell's own padding and border.
_ROW_PAD_PT = 5.0
# The cap has to clear the tallest realistic comment (finding + fix + evidence
# on a criterion with a lot to say) or the cap itself becomes the clipper.
_ROW_MIN, _ROW_MAX = 30.0, 420.0


def _score_formula(row: int) -> str:
    """Column I: derive the 0-2 score from the rating string in column H."""
    return (f'=IF(H{row}="{ps.EXCEL_RATINGS[ps.RATING_BEST]}",2,'
            f'IF(H{row}="{ps.EXCEL_RATINGS[ps.RATING_MIN]}",1,'
            f'IF(H{row}="{ps.EXCEL_RATINGS[ps.RATING_FAIL]}",0,"—")))')


def _lines_needed(text: Optional[str], width: float) -> int:
    """How many wrapped lines `text` takes in a column of this width.

    A greedy word-wrap rather than len/width, because Excel breaks on spaces.
    Dividing by width under-counts whenever a line cannot be filled to the edge
    — and these comments cite documents by name, so a 50-character unbreakable
    filename is normal. That under-count is what clips a row.
    """
    if not text:
        return 1
    per_line = max(8, int(width * _CHARS_PER_WIDTH))
    total = 0
    for para in str(text).split("\n"):
        words = para.split()
        if not words:
            total += 1
            continue
        lines, used = 1, 0
        for word in words:
            length = len(word)
            if length > per_line:
                # Longer than the column: Excel starts it on a fresh line and
                # breaks it mid-word across as many lines as it needs.
                if used:
                    lines += 1
                lines += (length - 1) // per_line
                used = length % per_line or per_line
            elif used == 0:
                used = length
            elif used + 1 + length <= per_line:
                used += 1 + length
            else:
                lines += 1
                used = length
        total += lines
    return total


def _row_height(cells: Iterable[tuple], extra_lines: int = 0,
                minimum: float = _ROW_MIN) -> float:
    """Estimate a row height that will not clip. (text, column_width) pairs.

    openpyxl has no autofit, and Excel only autofits on edit — so an unset
    height leaves a wrapped 300-character comment showing its first line and
    hiding the rest, which is exactly the content the reviewer needs.

    `extra_lines` is the share of a vertically-merged cell's content that this
    row has to carry.
    """
    lines = max(
        [_lines_needed(text, width) for text, width in cells] + [extra_lines, 1])
    return min(_ROW_MAX, max(minimum, lines * _LINE_PT + _ROW_PAD_PT))


def _style(cell, *, bold=False, size=FONT_SIZE, color=INK, fill=None,
           align=_WRAP_L, number_format=None, border=True):
    cell.font = Font(name=FONT_NAME, bold=bold, size=size, color=color)
    if fill:
        cell.fill = PatternFill("solid", fgColor=fill)
    cell.alignment = align
    if border:
        cell.border = _BORDER
    if number_format:
        cell.number_format = number_format
    return cell


def _comment_for(crit: Dict) -> str:
    """The reviewer-facing cell: the finding, the fix, then the evidence.

    Citations are grouped by document — repeating a 30-character filename once
    per slide is what made these cells enormous.
    """
    parts = []
    if crit.get("scoring_failed"):
        # Must not read as "reviewed and found nothing" — it was never rated.
        parts.append("⚠ Automated scoring could not rate this criterion. "
                     "It needs a reviewer's judgement.")
    if crit.get("comment"):
        parts.append(crit["comment"].strip())
    if crit.get("to_improve"):
        parts.append(f"→ To improve: {crit['to_improve'].strip()}")

    by_doc: Dict[str, List[int]] = {}
    for cite in crit.get("slides") or []:
        if isinstance(cite, dict):
            doc, slide = cite.get("doc") or "", cite.get("slide")
        else:
            doc, slide = "", cite
        try:
            by_doc.setdefault(doc, []).append(int(slide))
        except (TypeError, ValueError):
            continue
    if by_doc:
        refs = []
        for doc, slides in by_doc.items():
            listed = ", ".join(str(s) for s in sorted(set(slides)))
            plural = "slides" if len(set(slides)) > 1 else "slide"
            refs.append(f"{doc} — {plural} {listed}" if doc else f"{plural} {listed}")
        parts.append("Evidence: " + "; ".join(refs))
    return "\n".join(parts)


def _build_checklist(ws, categories: List[Dict]) -> None:
    ws.sheet_view.showGridLines = False
    w = CHECKLIST_WIDTHS

    ws.merge_cells("B1:J1")
    _style(ws["B1"], bold=True, size=18, color=WHITE, fill=NAVY, align=_WRAP_C, border=False)
    ws["B1"] = CHECKLIST_SHEET
    ws.row_dimensions[1].height = 26

    ws.merge_cells("B2:J2")
    _style(ws["B2"], size=10, color=INK, fill=GREY_LIGHT, border=False)
    ws["B2"] = LEGEND
    ws.row_dimensions[2].height = 28

    for offset, title in enumerate(CHECKLIST_HEADERS):
        col = get_column_letter(2 + offset)
        cell = ws[f"{col}3"]
        cell.value = title
        _style(cell, bold=True, size=10, color=WHITE, fill=BLUE,
               align=_WRAP_C if col in ("B", "H", "I") else _WRAP_L)
    ws.row_dimensions[3].height = 30

    for cat in categories:
        first, last = cat["row_start"], cat["row_end"]
        description = cat.get("description", "")
        n_rows = last - first + 1
        # The description is merged down the category, so its height demand is
        # shared across those rows rather than landing on the first one.
        desc_share = math.ceil(_lines_needed(description, w["C"]) / n_rows)

        ws.merge_cells(f"B{first}:B{last}")
        ws.merge_cells(f"C{first}:C{last}")
        ws[f"B{first}"] = cat["name"]
        ws[f"C{first}"] = description
        _style(ws[f"B{first}"], bold=True, align=Alignment(
            wrap_text=True, vertical="center", horizontal="center"))
        _style(ws[f"C{first}"], align=_WRAP_TL)
        # Merged cells only take style from the anchor; border the rest so the
        # category block does not look open-sided.
        for r in range(first, last + 1):
            ws[f"B{r}"].border = _BORDER
            ws[f"C{r}"].border = _BORDER

        for offset, crit in enumerate(cat["criteria"]):
            row = first + offset
            anchors = crit["anchors"]
            rating = ps.normalize_rating(crit.get("rating"))
            comment = _comment_for(crit)

            ws[f"D{row}"] = crit["name"]
            ws[f"E{row}"] = anchors.get(ps.RATING_FAIL, "")
            ws[f"F{row}"] = anchors.get(ps.RATING_MIN, "")
            ws[f"G{row}"] = anchors.get(ps.RATING_BEST, "")
            ws[f"H{row}"] = ps.EXCEL_RATINGS[rating] if rating else ""
            ws[f"I{row}"] = _score_formula(row)
            ws[f"J{row}"] = comment

            _style(ws[f"D{row}"], bold=True, fill=GREY_ROW)
            _style(ws[f"E{row}"], color=RED_INK, fill=RED_FILL)
            _style(ws[f"F{row}"], color=AMBER_INK, fill=AMBER_FILL)
            _style(ws[f"G{row}"], color=GREEN_INK, fill=GREEN_FILL)
            # H and I are left unfilled: the conditional formatting below colours
            # them, so they follow along when a rating is changed in Excel.
            _style(ws[f"H{row}"], align=_WRAP_C)
            _style(ws[f"I{row}"], align=_WRAP_C)
            _style(ws[f"J{row}"], fill=GREY_ROW, align=_WRAP_TL)

            ws.row_dimensions[row].height = _row_height(
                [(crit["name"], w["D"]),
                 (anchors.get(ps.RATING_FAIL), w["E"]),
                 (anchors.get(ps.RATING_MIN), w["F"]),
                 (anchors.get(ps.RATING_BEST), w["G"]),
                 (comment, w["J"])],
                extra_lines=desc_share)

    _apply_rating_colours(ws, categories)

    for col, width in w.items():
        ws.column_dimensions[col].width = width

    # Keep the headings, and the criterion you are reading, on screen.
    ws.freeze_panes = "D4"
    ws.auto_filter.ref = f"B3:J{categories[-1]['row_end']}"
    _page_setup(ws, landscape=True, title_rows="$3:$3")


def _apply_rating_colours(ws, categories: List[Dict]) -> None:
    """Colour the rating and score cells from the rating's own text.

    Conditional formatting rather than a static fill, so the colour follows the
    value when a reviewer changes a dropdown in Excel — the source workbook does
    the same.
    """
    first = categories[0]["row_start"]
    last = categories[-1]["row_end"]
    rng = f"H{first}:I{last}"
    for rating, fill, ink in (
        (ps.RATING_BEST, GREEN_FILL, GREEN_INK),
        (ps.RATING_MIN, AMBER_FILL, AMBER_INK),
        (ps.RATING_FAIL, RED_FILL, RED_INK),
    ):
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="equal",
            formula=[f'"{ps.EXCEL_RATINGS[rating]}"'],
            fill=PatternFill(start_color=fill, end_color=fill, fill_type="solid"),
            font=Font(name=FONT_NAME, size=FONT_SIZE, color=ink)))


def _page_setup(ws, *, landscape: bool, title_rows: Optional[str] = None,
                fit_height: int = 0) -> None:
    """fit_height=0 means 'as many pages tall as it takes'; 1 forces one page."""
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = fit_height
    if title_rows:
        ws.print_title_rows = title_rows
    ws.oddHeader.center.text = PAGE_HEADER
    ws.oddHeader.center.size = 9
    ws.oddFooter.center.text = "Page &P of &N"
    ws.oddFooter.center.size = 8
    ws.oddFooter.right.text = "Confidential"
    ws.oddFooter.right.size = 8


def _build_summary(ws, summary: Dict) -> None:
    categories = summary["categories"]
    ws.sheet_view.showGridLines = False

    ws.merge_cells("B1:G1")
    _style(ws["B1"], bold=True, size=14, color=WHITE, fill=NAVY_DEEP,
           align=_WRAP_C, border=False)
    ws["B1"] = "PROPOSAL REVIEW — SCORECARD SUMMARY"
    ws.row_dimensions[1].height = 24

    for offset, title in enumerate(SUMMARY_HEADERS):
        cell = ws[f"{get_column_letter(2 + offset)}3"]
        cell.value = title
        _style(cell, bold=True, size=10, color=WHITE, fill=BLUE, align=_WRAP_C)
    ws.row_dimensions[3].height = 30

    for offset, cat in enumerate(categories):
        row = 4 + offset
        start, end = cat["row_start"], cat["row_end"]
        note = _summary_note(cat)

        ws[f"B{row}"] = cat["name"]
        # The EFFECTIVE weight, not the declared one. A category rated entirely
        # Not Applicable drops to 0 and the rest are scaled back up to total
        # 100% — the workbook's own instruction, which it expects a human to do
        # by hand. Writing the declared weights instead would leave the
        # workbook's total disagreeing with the app's.
        ws[f"C{row}"] = cat.get("effective_weight", cat["weight"])
        # IFERROR is load-bearing: when every criterion in a category is N/A,
        # column I holds the text "—" throughout and AVERAGE over text-only
        # cells is #DIV/0!. Unguarded, that error propagates through E and F
        # into SUM(F4:F17) and takes the total and the readiness band with it.
        ws[f"D{row}"] = f"=IFERROR(AVERAGE('{CHECKLIST_SHEET}'!I{start}:I{end}),\"\")"
        ws[f"E{row}"] = f'=IFERROR(D{row}/2,"")'
        ws[f"F{row}"] = f"=IFERROR(E{row}*C{row},0)"
        ws[f"G{row}"] = note

        band = GREY_LIGHT if offset % 2 == 0 else None
        _style(ws[f"B{row}"], size=10, fill=band)
        _style(ws[f"C{row}"], size=10, fill=band, align=_WRAP_C, number_format="0%")
        _style(ws[f"D{row}"], size=10, fill=band, align=_WRAP_C, number_format="0.00")
        _style(ws[f"E{row}"], size=10, fill=band, align=_WRAP_C, number_format="0%")
        _style(ws[f"F{row}"], size=10, fill=band, align=_WRAP_C, number_format="0%")
        _style(ws[f"G{row}"], size=9, fill=band)
        # A tighter floor than the checklist's: the summary is a 14-row table
        # that has to read as one glance, and it must fit on a single page.
        ws.row_dimensions[row].height = _row_height(
            [(cat["name"], SUMMARY_WIDTHS["B"]), (note, SUMMARY_WIDTHS["G"])],
            minimum=18.0)

    ws["B18"] = "TOTAL"
    ws["C18"] = "=SUM(C4:C17)"
    ws["F18"] = "=SUM(F4:F17)"
    ws["G18"] = "Overall weighted readiness score"
    for col in ("B", "C", "D", "E", "F", "G"):
        _style(ws[f"{col}18"], bold=True, color=WHITE, fill=NAVY_DEEP,
               align=_WRAP_C if col in ("C", "D", "E", "F") else _WRAP_L)
    ws["C18"].number_format = "0%"
    ws["F18"].number_format = "0.0%"
    ws.row_dimensions[18].height = 22

    # The readiness band, as a formula so it tracks edits made in Excel.
    ws.merge_cells("B20:G20")
    ws["B20"] = (
        f'=IF(F18>=0.8,"{ps.BAND_READY}",'
        f'IF(F18>=0.6,"{ps.BAND_WORK}",'
        f'IF(F18>0,"{ps.BAND_NOT_READY}","{ps.BAND_AWAITING}")))')
    _style(ws["B20"], bold=True, size=12, color=NAVY_DEEP, fill=BAND_FILL,
           align=_WRAP_C, border=False)
    ws.row_dimensions[20].height = 26

    ws.merge_cells("B22:G24")
    notes = "\n".join(NOTE_LINES + [_provenance(summary)])
    ws["B22"] = notes
    _style(ws["B22"], size=9, align=_WRAP_TL, border=False)
    ws["B22"].font = Font(name=FONT_NAME, size=9, italic=True, color=INK)

    for col, width in SUMMARY_WIDTHS.items():
        ws.column_dimensions[col].width = width
    # The summary is the one-page answer to "how did this deck do" — never let
    # the band and the notes break onto a second sheet of paper.
    _page_setup(ws, landscape=True, fit_height=1)


def _summary_note(cat: Dict) -> str:
    """Why a category's number reads the way it does — N/A, partial, or nothing."""
    if cat.get("all_na"):
        return "Not Applicable for this deal — weight redistributed."
    if cat.get("none_rated"):
        return "Not yet rated."
    if cat.get("unrated_count"):
        return f"{cat['unrated_count']} of {cat['criteria_count']} criteria not yet rated."
    if cat.get("na_count"):
        return f"{cat['na_count']} criteria marked Not Applicable and excluded."
    return ""


def _provenance(summary: Dict) -> str:
    """Say what was reviewed and how, so a stale export can be recognised."""
    bits = []
    docs = summary.get("deck_docs") or []
    if docs:
        counts = summary.get("slide_counts") or {}
        described = [f"{d} ({counts[d]} slides)" if d in counts else d for d in docs]
        bits.append("Reviewed: " + ", ".join(described) + ".")
    if summary.get("unrated_count"):
        bits.append(
            f"{summary['unrated_count']} of {summary.get('criteria_total', 57)} criteria "
            "are unrated, so the total below is partial.")
    if summary.get("failed_count"):
        bits.append(f"{summary['failed_count']} of those could not be scored "
                    "automatically and need a reviewer's judgement.")
    if (summary.get("deck_parts") or 1) > 1:
        bits.append(f"The deck exceeded one context window and was scored in "
                    f"{summary['deck_parts']} parts; every category was scored against "
                    "every part and the best-evidenced verdict kept.")
    bits.append(f"Generated {datetime.now():%Y-%m-%d %H:%M} by the Blueprint pitch scorecard. "
                "Rubric: Pitch Checklist v1.3.")
    return " ".join(bits)


def build_workbook(summary: Dict) -> io.BytesIO:
    """Render a pitch_scorecard.summary() dict as an .xlsx in memory.

    Returns a rewound BytesIO. Takes the summary rather than a project id so it
    stays pure and can be golden-file tested without a project on disk.
    """
    wb = Workbook()
    _build_checklist(wb.active, summary["categories"])
    wb.active.title = CHECKLIST_SHEET
    _build_summary(wb.create_sheet(SUMMARY_SHEET), summary)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def suggested_filename(project_name: Optional[str] = None) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in (project_name or "Pitch")).strip("_") or "Pitch"
    return f"Pitch_Scorecard_{safe}_{datetime.now():%Y%m%d_%H%M}.xlsx"
