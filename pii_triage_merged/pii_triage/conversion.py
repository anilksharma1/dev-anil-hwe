"""Convert legacy binary Office formats to modern OOXML via LibreOffice headless conversion.

Runs cross-platform (Linux container, Windows, macOS) via the `soffice`/`libreoffice` CLI --
no Windows-only COM automation and no dedicated Windows VM/queue. This is the whole point of
the switch: a legacy .doc/.xls/.ppt file converts INLINE on whichever worker dequeues it, the
same as every other format, instead of needing a separate Windows leg and a two-hop
Windows-queue -> convert -> forward-to-Linux-queue dance.

Install: `libreoffice-writer libreoffice-calc libreoffice-impress` (apt) covers all three
formats without pulling in the full office suite (Draw, Base, Math). Set SOFFICE_PATH to
override the binary location/name if it isn't `soffice`/`libreoffice` on PATH.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_EXT_MAP = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}

# LibreOffice's --convert-to filter name for each target format.
_TARGET_FORMAT = {
    ".doc": "docx",
    ".xls": "xlsx",
    ".ppt": "pptx",
}

_SOFFICE_CANDIDATES = ("soffice", "libreoffice")


class ConversionTimeout(Exception):
    """Raised when a legacy conversion exceeded its timeout and the soffice process had to be
    killed. Deliberately a distinct type from an ordinary conversion failure: a genuine hang
    is a "lost cause" that will hang again for the same duration on a retry, so a caller can
    (and should) treat this one differently -- fail the file once and move on, rather than
    spending WORKER_MAX_ATTEMPTS more rounds re-discovering the same hang."""


def _soffice_binary() -> str | None:
    """Locate the LibreOffice CLI binary. SOFFICE_PATH overrides (a non-standard install
    location or a name other than soffice/libreoffice); otherwise checks PATH for both
    common command names. Returns None if neither is found -- the caller degrades to
    'conversion unavailable' rather than raising, matching this package's degrade-gracefully
    discipline for every other optional dependency."""
    override = os.environ.get("SOFFICE_PATH")
    if override and shutil.which(override):
        return override
    for name in _SOFFICE_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def convert_legacy_office(src: str, dest_dir: Path, timeout_s: int = 120) -> Path | None:
    """Convert a legacy Office file to its modern OOXML equivalent via LibreOffice headless.

    Returns the Path of the converted file, or None if conversion failed outright (an
    ordinary, possibly-transient failure, or LibreOffice simply isn't installed -- the
    caller may retry, or fall back to the original file). Raises ConversionTimeout if it
    hung past `timeout_s` and the soffice process had to be killed (a lost cause -- the
    caller should NOT retry; it will hang again for the same duration).
    """
    ext = Path(src).suffix.lower()
    if ext not in _EXT_MAP:
        return None

    binary = _soffice_binary()
    if binary is None:
        logging.warning(
            "no LibreOffice binary (soffice/libreoffice) found on PATH (set SOFFICE_PATH to "
            "override); cannot convert %s -- install libreoffice-writer/-calc/-impress", src)
        return None

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (Path(src).stem + _EXT_MAP[ext])
    src_abs = os.path.abspath(src)

    # Each conversion gets its OWN LibreOffice user profile, in a throwaway temp dir. Without
    # this, concurrent `soffice --headless` invocations (WORKER_CONCURRENCY > 1 doing several
    # conversions at once on one worker) collide on the default shared profile lock -- a
    # well-documented LibreOffice headless pain point -- and hang or fail unpredictably.
    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        cmd = [
            binary, "--headless", "--norestore", "--nolockcheck", "--nodefault",
            "--nofirststartwizard", f"-env:UserInstallation=file://{Path(profile_dir).as_posix()}",
            "--convert-to", _TARGET_FORMAT[ext], "--outdir", str(dest_dir), src_abs,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            # subprocess.run() already killed the child and waited for it before raising --
            # no separate watchdog/force-kill dance needed, unlike the old COM-based approach.
            raise ConversionTimeout(
                f"soffice exceeded {timeout_s}s converting {Path(src).name}") from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)

    if result.returncode != 0:
        logging.warning("conversion failed for %s (soffice exit %d): %s", src, result.returncode,
                        (result.stderr or result.stdout or "").strip()[:500])
        return None

    return dest if dest.exists() else None
