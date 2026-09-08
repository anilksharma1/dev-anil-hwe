#!/usr/bin/env python3
"""build_inventory_excel_report.py

Reusable generator for the "aggregated results" Excel deliverable from a
pii_triage inventory.csv (Doc ID / Estimated Entities / BDE Tag /
Responsive-NR, plus a Summary rollup). Built to be run again on any future
inventory.csv that follows the same pii_triage schema -- column names can be
overridden via flags if a given run's inventory differs.

No PII values are read or written: only rel_path (doc id), estimated entity
counts, and routing/lane labels are touched, per pii_triage's design
guarantee. Do not point this at a file containing PII values themselves.

Usage
-----
    python build_inventory_excel_report.py INVENTORY_CSV --initials HK

    python build_inventory_excel_report.py INVENTORY_CSV --out "my_report.xlsx"

    python build_inventory_excel_report.py INVENTORY_CSV --initials HK \\
        --description "matter 12345 triage results" --bde-threshold 51

    python build_inventory_excel_report.py   # no args -- prompts via tkinter

If INVENTORY_CSV or --bde-threshold is omitted, a tkinter dialog prompts for
it (a file picker for the CSV, a number prompt for the threshold, default 51).

If --out is not given, the output filename is built from the EXO Edge
naming convention: "yymmdd XX description.xlsx" (yymmdd = --date or today,
XX = --initials, uppercased). You'll be prompted for --initials if it's
missing and --out wasn't given either.

Sheet layout
------------
    Doc Detail  -- one row per doc: Doc ID, Estimated Entities, BDE Tag,
                   Responsive/NR, Lane, Stage Used, Consistency Flag. Plain
                   computed values (not formulas) -- fast to write/open at
                   25k+ rows.
    Summary     -- rollup counts/percentages as live COUNTIF/SUM/AVERAGE/
                   COUNTA formulas referencing the Doc Detail sheet, so the
                   Summary recalculates if you hand-edit Doc Detail.
    Methodology -- static documentation of the column mapping and rules.

Final consistency rule (applied after Responsive/NR, Lane and the raw BDE
Tag are computed -- see --bde-consistency for the exact behavior):
    1. NR doc            -> BDE Tag forced to No, Estimated Entities forced
                             to 0 (whatever the raw values were).
    2. Responsive doc     -> BDE Tag keeps the Estimated Entities >= threshold
                             result; a Responsive doc with 0 entities is
                             flagged as a data anomaly for manual review
                             rather than a value being invented for it.
    Error / Needs Review and Non-searchable (Sample Required) rows are left
    untouched by this rule.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

try:
    import tkinter as tk
    from tkinter import filedialog, simpledialog
except ImportError:  # pragma: no cover - tkinter unavailable (headless/minimal Python)
    tk = None

DEFAULT_BDE_THRESHOLD = 51
DEFAULT_DESCRIPTION = "pii triage aggregated results"

# Lane -> Responsive/NR status. Extend this if a rulepack introduces new lanes.
NR_LANES = {"likely_non_responsive"}
RESPONSIVE_LANES = {"standard", "bde", "structured_bde"}
ERROR_LANES = {"review_error", "needs_parser", "structured_unreadable"}
SAMPLE_LANES = {"nonsearchable_sample"}

STATUS_ORDER = ["Responsive", "NR", "Non-searchable (Sample Required)", "Error / Needs Review", "Unknown"]
LANE_ORDER = [
    "standard", "likely_non_responsive", "structured_bde", "bde",
    "review_error", "nonsearchable_sample", "structured_unreadable", "needs_parser",
]

BODY_FONT = Font(name="Calibri", size=11)
TITLE_FONT = Font(name="Calibri", size=14, bold=True)
SUBHEAD_FONT = Font(name="Calibri", size=11, bold=True)
NOTE_FONT = Font(name="Calibri", size=9, italic=True)
HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
THIN = Side(style="thin", color="D0D0D0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build the aggregated Doc ID / Estimated Entities / BDE Tag / "
                     "Responsive-NR Excel report from a pii_triage inventory.csv.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("inventory_csv", nargs="?", default=None,
                    help="Path to the inventory.csv to aggregate. If omitted, a tkinter "
                         "file-picker dialog prompts for it.")
    p.add_argument("--out", help="Output .xlsx path. If omitted, built from "
                                  "--date/--initials/--description per the "
                                  "'yymmdd XX description.xlsx' naming convention.")
    p.add_argument("--bde-threshold", type=int, default=None,
                    help="Estimated-entities threshold for the BDE Tag column. If omitted, "
                         f"a tkinter dialog prompts for it (default {DEFAULT_BDE_THRESHOLD}).")
    p.add_argument("--initials", help="2-letter author initials (used in the default filename).")
    p.add_argument("--description", default=DEFAULT_DESCRIPTION,
                    help="Short filename description (used in the default filename).")
    p.add_argument("--date", help="yymmdd for the default filename. Defaults to today.")
    p.add_argument("--doc-title", default="PII TRIAGE AGGREGATED RESULTS",
                    help="Title text shown in worksheet headers and sheet titles.")
    p.add_argument("--id-col", default="rel_path", help="Source column used as Doc ID.")
    p.add_argument("--entities-col", default="estimated_entities",
                    help="Source column used as Estimated Entities.")
    p.add_argument("--suggested-lane-col", default="suggested_lane",
                    help="Source column for the Stage 1 (rules) lane.")
    p.add_argument("--s2-lane-col", default="s2_lane",
                    help="Source column for the Stage 2 (LLM-graded) lane, if present.")
    p.add_argument("--s2-ran-col", default="s2_ran",
                    help="Source column flagging whether Stage 2 ran, if present.")
    p.add_argument("--bde-consistency", choices=["guardrail", "collapse", "off"], default="guardrail",
                    help="Final NR/Responsive consistency rule applied to BDE Tag and Estimated "
                         "Entities. 'guardrail' (default): NR forces BDE=No/Entities=0; a "
                         "Responsive doc with 0 entities is flagged as an anomaly, BDE Tag stays "
                         "threshold-based. 'collapse': same NR handling, but BDE Tag is set to Yes "
                         "for every Responsive doc (drops the entity-count threshold entirely). "
                         "'off': no correction, BDE Tag/Entities are reported exactly as computed.")
    return p.parse_args(argv)


def build_output_path(args: argparse.Namespace) -> Path:
    if args.out:
        return Path(args.out)

    initials = args.initials
    if not initials:
        initials = input("Please provide your 2-letter author code (your initials): ").strip()
    initials = initials.upper()

    date_str = args.date or date.today().strftime("%y%m%d")
    description = args.description.replace("_", " ").replace("-", " ")
    filename = f"{date_str} {initials} {description}.xlsx"
    return Path(args.inventory_csv).resolve().parent / filename


def prompt_missing_inputs(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in inventory_csv / bde_threshold via tkinter dialogs if not passed on the CLI."""
    if args.inventory_csv and args.bde_threshold is not None:
        return args

    if tk is None:
        if not args.inventory_csv:
            sys.exit("error: inventory_csv not given and tkinter is unavailable to prompt for it")
        args.bde_threshold = DEFAULT_BDE_THRESHOLD
        return args

    root = tk.Tk()
    root.withdraw()

    if not args.inventory_csv:
        selected = filedialog.askopenfilename(
            title="Select inventory.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not selected:
            root.destroy()
            sys.exit("error: no inventory.csv selected")
        args.inventory_csv = selected

    if args.bde_threshold is None:
        threshold = simpledialog.askinteger(
            "BDE Threshold",
            "Estimated-entities threshold for the BDE Tag column:",
            initialvalue=DEFAULT_BDE_THRESHOLD, minvalue=1,
        )
        if threshold is None:
            root.destroy()
            sys.exit("error: no BDE threshold provided")
        args.bde_threshold = threshold

    root.destroy()
    return args


# --------------------------------------------------------------------------- #
# Data loading + classification
# --------------------------------------------------------------------------- #

def load_records(csv_path: Path, args: argparse.Namespace) -> list[dict]:
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        for required in (args.id_col, args.entities_col, args.suggested_lane_col):
            if required not in fieldnames:
                sys.exit(f"error: column '{required}' not found in {csv_path} "
                          f"(available columns: {', '.join(fieldnames)})")
        has_stage2 = args.s2_lane_col in fieldnames and args.s2_ran_col in fieldnames
        rows = [r for r in reader if r.get(args.id_col)]

    records = []
    for r in rows:
        try:
            raw_entities = int(float(r.get(args.entities_col) or 0))
        except ValueError:
            raw_entities = 0

        s2_ran = str(r.get(args.s2_ran_col, "")).strip().upper() == "TRUE" if has_stage2 else False
        s2_lane = r.get(args.s2_lane_col, "") if has_stage2 else ""
        lane = s2_lane if (s2_ran and s2_lane) else r.get(args.suggested_lane_col, "")
        stage = "Stage 2 (LLM graded)" if (s2_ran and s2_lane) else "Stage 1 (rules)"
        status = lane_to_status(lane)
        raw_bde_tag = "Yes" if raw_entities >= args.bde_threshold else "No"

        final_entities, final_bde_tag, flag = apply_consistency_rule(
            status, raw_entities, raw_bde_tag, args.bde_consistency)

        # Lane correction: a Responsive doc with a weak entity count (more than one
        # signal but below the BDE threshold) and BDE Tag = No belongs in the
        # "standard" lane, regardless of what the source lane column says.
        if (status == "Responsive" and final_bde_tag == "No"
                and 1 < final_entities < args.bde_threshold and lane != "standard"):
            flag = f"{flag}; " if flag else ""
            flag += f"lane forced to standard (was {lane})"
            lane = "standard"

        records.append({
            "doc_id": r.get(args.id_col, ""),
            "estimated_entities": final_entities,
            "raw_estimated_entities": raw_entities,
            "bde_tag": final_bde_tag,
            "responsive_status": status,
            "lane": lane,
            "stage": stage,
            "consistency_flag": flag,
        })
    return records


def apply_consistency_rule(status: str, raw_entities: int, raw_bde_tag: str,
                            mode: str) -> tuple[int, str, str]:
    """Final-step rule reconciling BDE Tag / Estimated Entities against Responsive/NR.

    Returns (final_entities, final_bde_tag, consistency_flag). Only touches rows
    already classified Responsive or NR; Error/Sample/Unknown rows pass through
    unchanged. Never invents a nonzero entity count -- a Responsive doc with 0
    entities is flagged for manual review instead.
    """
    if mode == "off":
        return raw_entities, raw_bde_tag, ""

    flags = []
    if status == "NR":
        final_entities = 0
        if raw_entities != 0:
            flags.append(f"entities forced to 0 (was {raw_entities})")
        final_bde_tag = "No"
        if raw_bde_tag != "No":
            flags.append("BDE forced to No")
        return final_entities, final_bde_tag, "; ".join(flags)

    if status == "Responsive":
        final_entities = raw_entities
        if raw_entities == 0:
            flags.append("ANOMALY: Responsive with 0 entities -- needs manual review")
        if mode == "collapse":
            final_bde_tag = "Yes"
            if raw_bde_tag != "Yes":
                flags.append(f"BDE forced to Yes (was {raw_bde_tag}, entity-count rule)")
        else:
            final_bde_tag = raw_bde_tag
        return final_entities, final_bde_tag, "; ".join(flags)

    # Error / Needs Review, Non-searchable (Sample Required), Unknown -- left as-is
    return raw_entities, raw_bde_tag, ""


def lane_to_status(lane: str) -> str:
    if lane in NR_LANES:
        return "NR"
    if lane in RESPONSIVE_LANES:
        return "Responsive"
    if lane in ERROR_LANES:
        return "Error / Needs Review"
    if lane in SAMPLE_LANES:
        return "Non-searchable (Sample Required)"
    return "Unknown"


# --------------------------------------------------------------------------- #
# Workbook building
# --------------------------------------------------------------------------- #

def style_sheet_page(ws, doc_title: str):
    ws.oddHeader.left.text = "EXO Edge"
    ws.oddHeader.left.font = "Calibri,Regular"
    ws.oddHeader.left.size = 11
    ws.oddHeader.center.text = doc_title
    ws.oddHeader.center.font = "Calibri,Regular"
    ws.oddHeader.center.size = 11
    ws.oddFooter.left.text = "Page &P of &N"
    ws.oddFooter.left.font = "Calibri,Regular"
    ws.oddFooter.left.size = 11


def write_header_row(ws, row: int, headers: list[str]):
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.border = BORDER
        c.alignment = Alignment(horizontal="center", wrap_text=True)


def build_doc_detail_sheet(wb: Workbook, records: list[dict], doc_title: str) -> tuple:
    ws = wb.create_sheet("Doc Detail")
    style_sheet_page(ws, doc_title)

    ws["A1"] = f"{doc_title} - PER-DOCUMENT RESULTS"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:H1")

    header_row = 3
    write_header_row(ws, header_row, [
        "DOC ID", "ESTIMATED ENTITIES", "BDE TAG", "RESPONSIVE / NR", "LANE", "STAGE USED",
        "CONSISTENCY FLAG", "RAW ESTIMATED ENTITIES (PRE-OVERRIDE)",
    ])

    first_data_row = header_row + 1
    flag_fill = PatternFill(start_color="FDECEA", end_color="FDECEA", fill_type="solid")
    for offset, rec in enumerate(records):
        r = first_data_row + offset
        ws.cell(row=r, column=1, value=rec["doc_id"]).font = BODY_FONT
        ws.cell(row=r, column=2, value=rec["estimated_entities"]).font = BODY_FONT
        ws.cell(row=r, column=3, value=rec["bde_tag"]).font = BODY_FONT
        ws.cell(row=r, column=4, value=rec["responsive_status"]).font = BODY_FONT
        ws.cell(row=r, column=5, value=rec["lane"]).font = BODY_FONT
        ws.cell(row=r, column=6, value=rec["stage"]).font = BODY_FONT
        flag_cell = ws.cell(row=r, column=7, value=rec["consistency_flag"])
        flag_cell.font = BODY_FONT
        if rec["consistency_flag"]:
            flag_cell.fill = flag_fill
        ws.cell(row=r, column=8, value=rec["raw_estimated_entities"]).font = BODY_FONT
        for col in range(1, 9):
            ws.cell(row=r, column=col).border = BORDER

    last_row = header_row + len(records)
    ws.auto_filter.ref = f"A{header_row}:H{last_row}"
    ws.freeze_panes = f"A{header_row + 1}"

    widths = {"A": 60, "B": 18, "C": 10, "D": 26, "E": 22, "F": 20, "G": 40, "H": 28}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.column_dimensions["H"].hidden = True

    return ws, header_row, last_row


def add_countif_table(ws, row: int, title: str, col_letter: str, labels: list[str],
                       total_cell: str, headers=("Label", "Doc Count", "% of Total")) -> int:
    ws.cell(row=row, column=1, value=title).font = SUBHEAD_FONT
    row += 1
    write_header_row(ws, row, list(headers))
    row += 1
    for label in labels:
        ws.cell(row=row, column=1, value=label).font = BODY_FONT
        ws.cell(row=row, column=2, value=f"=COUNTIF('Doc Detail'!{col_letter}:{col_letter},A{row})").font = BODY_FONT
        pct = ws.cell(row=row, column=3, value=f"=B{row}/{total_cell}")
        pct.font = BODY_FONT
        pct.number_format = "0.0%"
        for col in range(1, 4):
            ws.cell(row=row, column=col).border = BORDER
        row += 1
    return row


def build_summary_sheet(wb: Workbook, csv_path: Path, doc_title: str, bde_threshold: int,
                         bde_consistency: str) -> None:
    ws = wb.create_sheet("Summary", 0)
    style_sheet_page(ws, doc_title)

    ws["A1"] = f"{doc_title} - SUMMARY"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:C1")

    ws["A2"] = f"Source: {csv_path}"
    ws["A2"].font = BODY_FONT
    ws["A3"] = f"Generated: {date.today().isoformat()}"
    ws["A3"].font = BODY_FONT
    ws["A4"] = "Total documents:"
    ws["A4"].font = BODY_FONT
    ws["B4"] = "=COUNTA('Doc Detail'!A:A)-2"
    ws["B4"].font = BODY_FONT
    total_cell = "$B$4"

    row = 6
    row = add_countif_table(ws, row, "RESPONSIVENESS BREAKDOWN", "D", STATUS_ORDER, total_cell,
                             headers=("Status", "Doc Count", "% of Total"))

    row += 1
    row = add_countif_table(ws, row, f"BDE TAG BREAKDOWN (Estimated Entities >= {bde_threshold})",
                             "C", ["Yes", "No"], total_cell, headers=("BDE Tag", "Doc Count", "% of Total"))

    row += 1
    ws.cell(row=row, column=1, value="ENTITY VOLUME").font = SUBHEAD_FONT
    row += 1
    ws.cell(row=row, column=1, value="Total estimated entities (all docs)").font = BODY_FONT
    ws.cell(row=row, column=2, value="=SUM('Doc Detail'!B:B)").font = BODY_FONT
    row += 1
    ws.cell(row=row, column=1, value="Average estimated entities / doc").font = BODY_FONT
    c = ws.cell(row=row, column=2, value="=AVERAGE('Doc Detail'!B:B)")
    c.font = BODY_FONT
    c.number_format = "0.0"
    row += 2

    row = add_countif_table(ws, row, "LANE BREAKDOWN (routing detail)", "E", LANE_ORDER, total_cell,
                             headers=("Lane", "Doc Count", "% of Total"))

    if bde_consistency != "off":
        row += 1
        ws.cell(row=row, column=1, value="CONSISTENCY RULE CORRECTIONS / ANOMALIES").font = SUBHEAD_FONT
        row += 1
        write_header_row(ws, row, ["Check", "Doc Count", "% of Total"])
        row += 1
        correction_rows = [
            ("NR docs: entities forced to 0", '"*entities forced to 0*"'),
            ("NR docs: BDE forced to No", '"*BDE forced to No*"'),
        ]
        if bde_consistency == "collapse":
            correction_rows.append(("Responsive docs: BDE forced to Yes", '"*BDE forced to Yes*"'))
        correction_rows.append(("Responsive docs: 0-entity anomaly (needs review)", '"*ANOMALY*"'))
        for label, criteria in correction_rows:
            ws.cell(row=row, column=1, value=label).font = BODY_FONT
            ws.cell(row=row, column=2,
                    value=f"=COUNTIF('Doc Detail'!G:G,{criteria})").font = BODY_FONT
            pct = ws.cell(row=row, column=3, value=f"=B{row}/{total_cell}")
            pct.font = BODY_FONT
            pct.number_format = "0.0%"
            for col in range(1, 4):
                ws.cell(row=row, column=col).border = BORDER
            row += 1

    row += 1
    ws.cell(row=row, column=1, value="Notes").font = SUBHEAD_FONT
    row += 1
    notes = [
        f"BDE Tag: Estimated Entities >= {bde_threshold}, then the final consistency rule below "
        f"is applied (mode: {bde_consistency}).",
        "Responsive/NR and Lane: Stage 2 (LLM graded responsiveness) result where it ran, else "
        "Stage 1 rules-based result (files that were errors, unsupported, or routed to "
        "non-searchable sampling before Stage 2 could run). See the Methodology sheet.",
        "Final consistency rule: NR docs have BDE Tag forced to No and Estimated Entities forced "
        "to 0. Responsive docs with 0 entities are flagged as an anomaly rather than having a "
        "value invented for them (see the Consistency Flag column on Doc Detail; the pre-override "
        "entity count is kept in a hidden column for audit). Error/Non-searchable rows are left "
        "untouched. See the Methodology sheet for full detail.",
        "All counts/percentages on this sheet are COUNTIF/SUM/AVERAGE/COUNTA formulas against the "
        "Doc Detail sheet -- they recalculate automatically if Doc Detail is edited. Doc Detail "
        "itself holds plain computed values, not formulas.",
        "No PII values are contained in this workbook -- only entity-type counts and routing "
        "labels, per pii_triage's design guarantee.",
    ]
    for note in notes:
        ws.cell(row=row, column=1, value=note).font = NOTE_FONT
        row += 1

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 12


def build_methodology_sheet(wb: Workbook, csv_path: Path, doc_title: str, bde_threshold: int,
                             args: argparse.Namespace) -> None:
    ws = wb.create_sheet("Methodology")
    style_sheet_page(ws, doc_title)

    ws["A1"] = "COLUMN MAPPING & METHODOLOGY"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:C1")

    ws["A2"] = f"Source file: {csv_path}"
    ws["A2"].font = BODY_FONT

    row = 4
    ws.cell(row=row, column=1, value="OUTPUT COLUMN MAPPING").font = SUBHEAD_FONT
    row += 1
    write_header_row(ws, row, ["Output Column", "Source Column(s)", "Logic"])
    row += 1
    mapping_rows = [
        ("DOC ID", args.id_col, "Used as-is (unique per doc)."),
        ("ESTIMATED ENTITIES", args.entities_col, "Used as-is, cast to int."),
        ("BDE TAG", args.entities_col,
         f"Estimated Entities >= {bde_threshold} -> Yes, else No."),
        ("RESPONSIVE / NR", f"{args.s2_lane_col} or {args.suggested_lane_col}",
         f"If {args.s2_ran_col}=TRUE and {args.s2_lane_col} is non-blank, use it; else fall back "
         f"to {args.suggested_lane_col}. Mapped through the lane table below."),
        ("LANE", f"{args.s2_lane_col} or {args.suggested_lane_col}", "Same fallback rule as above."),
        ("STAGE USED", f"{args.s2_ran_col}, {args.s2_lane_col}",
         "'Stage 2 (LLM graded)' if Stage 2 ran and produced a lane, else 'Stage 1 (rules)'."),
        ("CONSISTENCY FLAG", "derived", "Notes any correction the final consistency rule made to "
         "this row, or an anomaly it found. Blank = no correction/anomaly."),
        ("RAW ESTIMATED ENTITIES (hidden col H)", args.entities_col,
         "The entity count before the consistency rule ran, kept for audit."),
    ]
    for out_col, src_col, logic in mapping_rows:
        ws.cell(row=row, column=1, value=out_col).font = BODY_FONT
        ws.cell(row=row, column=2, value=src_col).font = BODY_FONT
        c = ws.cell(row=row, column=3, value=logic)
        c.font = BODY_FONT
        c.alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, 4):
            ws.cell(row=row, column=col).border = BORDER
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="LANE -> RESPONSIVE/NR MAPPING").font = SUBHEAD_FONT
    row += 1
    write_header_row(ws, row, ["Lane Value", "Mapped Status"])
    row += 1
    lane_map_rows = [
        (", ".join(sorted(NR_LANES)), "NR"),
        (", ".join(sorted(RESPONSIVE_LANES)), "Responsive"),
        (", ".join(sorted(ERROR_LANES)), "Error / Needs Review"),
        (", ".join(sorted(SAMPLE_LANES)), "Non-searchable (Sample Required)"),
    ]
    for lane_val, status in lane_map_rows:
        ws.cell(row=row, column=1, value=lane_val).font = BODY_FONT
        ws.cell(row=row, column=2, value=status).font = BODY_FONT
        for col in range(1, 3):
            ws.cell(row=row, column=col).border = BORDER
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="Note on BDE Tag").font = SUBHEAD_FONT
    row += 1
    bde_note = (
        f"This report's BDE Tag starts as a pure Estimated Entities >= {bde_threshold} rule, then "
        "the final consistency rule below is applied. The pipeline's own is_bde/s2_is_bde flags "
        "(not used here) can differ from the raw threshold result, since they're driven by a "
        "dedicated LLM person-count call that can catch sparse-token rosters this rule misses, or "
        "correct an inflated token count down for single-subject files. Re-run with "
        "--bde-threshold to match a different job's BDE_THRESHOLD."
    )
    c = ws.cell(row=row, column=1, value=bde_note)
    c.font = NOTE_FONT
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
    ws.row_dimensions[row].height = 60
    row += 2

    ws.cell(row=row, column=1,
            value=f"Final consistency rule (--bde-consistency={args.bde_consistency})").font = SUBHEAD_FONT
    row += 1
    consistency_rows = [
        ("NR", "BDE Tag -> No; Estimated Entities -> 0 (unconditionally, whatever the raw values were)."),
        ("Responsive", "BDE Tag -> Yes if mode=collapse; else kept as the entity-count result. "
         "Estimated Entities kept as computed; a 0-entity Responsive doc is flagged as an anomaly "
         "(CONSISTENCY FLAG column) rather than a value being invented for it."),
        ("Error / Needs Review, Non-searchable (Sample Required), Unknown", "Left untouched."),
    ]
    for status_val, rule_text in consistency_rows:
        ws.cell(row=row, column=1, value=status_val).font = BODY_FONT
        c = ws.cell(row=row, column=2, value=rule_text)
        c.font = BODY_FONT
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=3)
        for col in range(1, 3):
            ws.cell(row=row, column=col).border = BORDER
        ws.row_dimensions[row].height = 45
        row += 1

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 70


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    args = parse_args(argv)
    args = prompt_missing_inputs(args)
    csv_path = Path(args.inventory_csv)
    if not csv_path.exists():
        sys.exit(f"error: {csv_path} not found")

    out_path = build_output_path(args)

    records = load_records(csv_path, args)
    if not records:
        sys.exit(f"error: no rows found in {csv_path}")

    wb = Workbook()
    build_doc_detail_sheet(wb, records, args.doc_title)
    build_summary_sheet(wb, csv_path, args.doc_title, args.bde_threshold, args.bde_consistency)
    build_methodology_sheet(wb, csv_path, args.doc_title, args.bde_threshold, args)
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    try:
        wb.save(out_path)
    except PermissionError:
        sys.exit(f"error: could not write {out_path} -- is it currently open in Excel? "
                  f"Close it and re-run.")

    print(f"Wrote {out_path}")
    print(f"total_docs={len(records)}")
    if args.bde_consistency != "off":
        flagged = [r for r in records if r["consistency_flag"]]
        print(f"consistency_rule={args.bde_consistency} rows_flagged={len(flagged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
