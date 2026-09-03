"""Collapse the two-Table-row artefact a Windows-leg .doc/.xls/.ppt file produces.

worker.py's Windows leg converts a legacy file then forwards it to the Linux queue under
the same job_id -- which creates TWO status-table rows for one physical input file: the
pre-conversion row (file_name="report.doc") and the post-conversion row forwarded to the
Linux queue (file_name="report.docx"), which carries the real processing outcome, tokens,
and timing. Counting both inflates any "how many files" tally by one row per legacy file.

Shared by hwe_scaled_store.py (the live Monitor dashboard) and collect_outputs.py
(dump_timing's _timing.json snapshot) so the same fix applies everywhere a raw Table/
TaskRecord list gets turned into a file count -- collect_outputs.py's own inventory.csv
never had this bug (it already skips a Windows-leg stub via its forwarded.json check), so
this module only needs to cover the OTHER two "how many files" readouts.
"""
from __future__ import annotations

import os

_LEGACY_EXT_MAP = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}


def collapse_legacy_pairs(items, name_of=lambda item: item.get("file_name", "")):
    """Drop a legacy pre-conversion item when its post-conversion counterpart is ALSO
    present in `items` -- the converted item is that file's true current state (still
    in flight, or completed/failed), so counting both double-counts one physical file.
    A legacy item with NO converted counterpart yet (conversion hasn't forwarded, or
    never will) is kept as-is -- it IS the file's current state.

    `items` can be Table entity dicts (name_of defaults to dict.get) or any other object
    -- pass a `name_of` that extracts its file_name, e.g. `lambda t: t.file_name` for a
    scaling_lib TaskRecord.

    Heuristic, not authoritative: a corpus that happens to contain both a genuinely-
    unrelated "x.doc" and "x.docx" as separate original files would see the ".doc" item
    collapsed away too. Cheap and no-I/O (pure file_name string matching) is the point --
    an authoritative answer needs the on-disk forwarded.json/.orig.json sidecars, which
    collect_outputs.py's inventory.csv build already uses.
    """
    names = {name_of(it) for it in items}
    out = []
    for it in items:
        name = name_of(it)
        stem, ext = os.path.splitext(name)
        converted_ext = _LEGACY_EXT_MAP.get(ext.lower())
        if converted_ext and (stem + converted_ext) in names:
            continue
        out.append(it)
    return out
