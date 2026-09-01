"""Convert legacy binary Office formats to modern OOXML via Microsoft Office COM."""
from __future__ import annotations

import logging
import os
from pathlib import Path

# win32com FileFormat constants
_WD_FORMAT_DOCX = 16          # wdFormatDocumentDefault
_XL_FORMAT_XLSX = 51          # xlOpenXMLWorkbook
_PP_FORMAT_PPTX = 24          # ppSaveAsOpenXMLPresentation

_EXT_MAP = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}


def convert_legacy_office(src: str, dest_dir: Path, timeout_s: int = 120) -> Path | None:
    """Convert a legacy Office file to its modern OOXML equivalent.

    Returns the Path of the converted file, or None if conversion failed.
    """
    ext = Path(src).suffix.lower()
    if ext not in _EXT_MAP:
        return None

    dest = dest_dir / (Path(src).stem + _EXT_MAP[ext])
    src_abs = os.path.abspath(src)
    dest_abs = str(dest.resolve())

    try:
        if ext == ".doc":
            return _convert_word(src_abs, dest_abs, dest)
        if ext == ".xls":
            return _convert_excel(src_abs, dest_abs, dest)
        if ext == ".ppt":
            return _convert_powerpoint(src_abs, dest_abs, dest)
    except Exception:
        logging.warning("conversion failed for %s", src, exc_info=True)
        return None


def _convert_word(src: str, dest: str, dest_path: Path) -> Path | None:
    import win32com.client  # noqa: PLC0415
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(src, ReadOnly=True)
        try:
            doc.SaveAs2(dest, FileFormat=_WD_FORMAT_DOCX)
        finally:
            doc.Close(SaveChanges=False)
    finally:
        word.Quit()
    return dest_path if dest_path.exists() else None


def _convert_excel(src: str, dest: str, dest_path: Path) -> Path | None:
    import win32com.client  # noqa: PLC0415
    excel = win32com.client.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    try:
        wb = excel.Workbooks.Open(src, ReadOnly=True)
        try:
            wb.SaveAs(dest, FileFormat=_XL_FORMAT_XLSX)
        finally:
            wb.Close(SaveChanges=False)
    finally:
        excel.Quit()
    return dest_path if dest_path.exists() else None


def _convert_powerpoint(src: str, dest: str, dest_path: Path) -> Path | None:
    import win32com.client  # noqa: PLC0415
    ppt = win32com.client.DispatchEx("PowerPoint.Application")
    try:
        presentation = ppt.Presentations.Open(src, WithWindow=False)
        try:
            presentation.SaveAs(dest, _PP_FORMAT_PPTX)
        finally:
            presentation.Close()
    finally:
        ppt.Quit()
    return dest_path if dest_path.exists() else None
