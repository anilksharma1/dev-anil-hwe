"""Convert legacy binary Office formats to modern OOXML via Microsoft Office COM."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
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

# Image name of the Office process each converter launches via DispatchEx, so a
# timeout can find and kill the right one.
_PROC_NAME = {
    ".doc": "WINWORD.EXE",
    ".xls": "EXCEL.EXE",
    ".ppt": "POWERPNT.EXE",
}

_CONVERTERS = {}  # populated below, after the _convert_* functions are defined


class ConversionTimeout(Exception):
    """Raised when a legacy Office conversion exceeded its timeout and the hung Office
    process had to be force-killed. Deliberately a distinct type from an ordinary
    conversion failure: a genuine hang is a "lost cause" that will hang again for the
    same duration on a retry, so a caller can (and should) treat this one differently
    -- fail the file once and move on, rather than spending WORKER_MAX_ATTEMPTS more
    rounds re-discovering the same hang."""


def _running_pids(image_name: str) -> set[int]:
    """Best-effort PIDs of all processes currently running under this image name."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        logging.warning("tasklist failed while probing for %s", image_name, exc_info=True)
        return set()
    pids: set[int] = set()
    for line in out.stdout.splitlines():
        fields = [f.strip('"') for f in line.split('","')]
        if len(fields) >= 2:
            try:
                pids.add(int(fields[1]))
            except ValueError:
                pass  # e.g. tasklist's "INFO: No tasks..." line when none are running
    return pids


def _force_kill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    except Exception:
        logging.warning("failed to force-kill pid %d after a conversion timeout", pid,
                        exc_info=True)


def _run_with_timeout(fn, image_name: str, timeout_s: int):
    """Run a blocking COM conversion `fn` on a worker thread and enforce `timeout_s`.

    win32com's DispatchEx calls are otherwise not interruptible -- a hung Office
    process (a corrupt file, a blocking "convert file?"/password modal, a huge
    legacy workbook) would wedge the calling thread forever, with FILE_TIMEOUT_S
    never actually enforced. Here, if the thread is still running past the
    deadline, DispatchEx's guarantee of always starting a *new* Office instance
    lets us identify the PID it spawned (present after the call started, absent
    before) and force-kill it, which unblocks or errors out the wedged thread
    instead of leaving it -- and the worker slot it occupies -- stuck indefinitely.

    Returns fn()'s result. Raises ConversionTimeout if the deadline was hit (a lost
    cause); other exceptions from fn() propagate as themselves (an ordinary failure,
    possibly transient, worth a normal retry).
    """
    before = _running_pids(image_name)
    outcome: dict = {}

    def target():
        import pythoncom
        pythoncom.CoInitialize()
        try:
            outcome["value"] = fn()
        except Exception as exc:
            outcome["error"] = exc
        finally:
            pythoncom.CoUninitialize()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        new_pids = _running_pids(image_name) - before
        for pid in new_pids:
            _force_kill(pid)
        detail = f"force-killed pid(s): {sorted(new_pids) or 'none found'}"
        logging.warning("legacy Office conversion exceeded %ss timeout (%s); %s",
                        timeout_s, image_name, detail)
        raise ConversionTimeout(f"{image_name} exceeded {timeout_s}s timeout; {detail}")

    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def convert_legacy_office(src: str, dest_dir: Path, timeout_s: int = 120) -> Path | None:
    """Convert a legacy Office file to its modern OOXML equivalent.

    Returns the Path of the converted file, or None if conversion failed outright
    (an ordinary, possibly-transient failure -- the caller may retry). Raises
    ConversionTimeout if it hung past `timeout_s` and had to be force-killed (a lost
    cause -- the caller should NOT retry; it will hang again for the same duration).
    """
    ext = Path(src).suffix.lower()
    if ext not in _EXT_MAP:
        return None

    dest = dest_dir / (Path(src).stem + _EXT_MAP[ext])
    src_abs = os.path.abspath(src)
    dest_abs = str(dest.resolve())
    converter = _CONVERTERS[ext]

    try:
        return _run_with_timeout(
            lambda: converter(src_abs, dest_abs, dest), _PROC_NAME[ext], timeout_s,
        )
    except ConversionTimeout:
        raise  # let the caller distinguish a hang from an ordinary failure
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


_CONVERTERS.update({
    ".doc": _convert_word,
    ".xls": _convert_excel,
    ".ppt": _convert_powerpoint,
})
