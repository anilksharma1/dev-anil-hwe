#!/usr/bin/env python3
"""Prove the recall-critical NR path is byte-identical to the frozen baseline.

The spec freezes the responsiveness (NR/R) path: detection.py and routing.py in
whole, plus the responsiveness call itself -- apply_llm (enrich.py), llm_classify
(azure_clients.py), and the _SYSTEM_PROMPT constant llm_classify depends on.

detection.py and routing.py are frozen WHOLE (sha256 of the file). But enrich.py
and azure_clients.py each also contain NON-frozen functions we edit in this build
(apply_bde_count, llm_count_entities, _BDE_COUNT_PROMPT), so those two files cannot
be hashed whole. Their frozen pieces are fingerprinted at FUNCTION / CONSTANT scope
via inspect.getsource / the constant's value.

Usage:
    python tools/check_nr_frozen.py capture   # write the golden (run once, pristine)
    python tools/check_nr_frozen.py check      # verify; exit 1 on any drift

Run `check` at the end of every build. A clean run is the proof that no edit
touched the NR path.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PKG = os.path.join(_ROOT, "pii_triage")
_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nr_frozen_golden.json")

# Whole-file frozen modules (relative to the package dir).
_WHOLE_FILE = ["detection.py", "routing.py"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_newlines(data: bytes) -> bytes:
    r"""CRLF / CR -> LF.

    Whole-file hashing is the only part of this check that sees raw bytes, so it was the only
    part that tripped on line endings -- and it tripped on BOTH frozen files at once, which
    reads alarmingly like a real regression. It is not: Python does not care whether a source
    file uses LF or CRLF, so a line-ending difference cannot change NR behaviour.

    This bit on the first real Windows run (the zip ships LF; something on the Windows side
    converted to CRLF), and a FALSE drift alarm on the recall-critical path is genuinely
    dangerous -- it teaches people either to ignore the check or to silence it with `capture`,
    which is exactly how a real regression would get blessed. So we normalize.

    Everything that actually matters is still caught: any added, removed or edited line changes
    the hash. The function-scoped and constant-scoped checks were never affected, because
    inspect.getsource() reads with universal newlines and a str constant has no line endings.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _file_hash(rel: str) -> str:
    with open(os.path.join(_PKG, rel), "rb") as fh:
        return _sha(_normalize_newlines(fh.read()))


def _looks_like_newlines_only(rel: str, golden_hash: str) -> bool:
    """True if the RAW bytes match the golden -- i.e. the golden predates newline
    normalisation and the only difference is line endings."""
    try:
        with open(os.path.join(_PKG, rel), "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    if _sha(raw) == golden_hash:
        return True
    # Or the reverse: golden was captured from CRLF, file is now LF.
    return _sha(raw.replace(b"\n", b"\r\n")) == golden_hash


def _fingerprint() -> dict:
    """Compute the current fingerprint of the whole frozen NR surface."""
    # Import lazily and inside the function so a missing optional dep can't stop us;
    # azure_clients imports Azure SDKs only lazily, so importing the module is safe.
    from pii_triage import enrich, azure_clients

    fp: dict = {"files": {}, "functions": {}, "constants": {}}
    for rel in _WHOLE_FILE:
        fp["files"][rel] = _file_hash(rel)

    # Function-scoped: hash the exact source text of each frozen function.
    frozen_funcs = {
        "enrich.apply_llm": enrich.apply_llm,
        "azure_clients.llm_classify": azure_clients.llm_classify,
    }
    for name, fn in frozen_funcs.items():
        src = inspect.getsource(fn)
        fp["functions"][name] = _sha(src.encode("utf-8"))

    # Constant-scoped: hash the VALUE of the NR system prompt (a module-level str
    # that llm_classify reads; editing it would silently change NR behaviour).
    fp["constants"]["azure_clients._SYSTEM_PROMPT"] = _sha(
        azure_clients._SYSTEM_PROMPT.encode("utf-8"))
    return fp


def capture() -> int:
    fp = _fingerprint()
    with open(_GOLDEN, "w", encoding="utf-8") as fh:
        json.dump(fp, fh, indent=2, sort_keys=True)
    print(f"captured NR-frozen golden -> {_GOLDEN}")
    for section in ("files", "functions", "constants"):
        for k, v in fp[section].items():
            print(f"  {section:9s} {k:38s} {v[:16]}...")
    return 0


def check() -> int:
    if not os.path.isfile(_GOLDEN):
        print("ERROR: no golden file; run `capture` once against pristine code first.")
        return 2
    with open(_GOLDEN, "r", encoding="utf-8") as fh:
        golden = json.load(fh)
    current = _fingerprint()
    drift = []
    for section in ("files", "functions", "constants"):
        g, c = golden.get(section, {}), current.get(section, {})
        for k in sorted(set(g) | set(c)):
            gv, cv = g.get(k), c.get(k)
            if gv != cv and section == "files" and gv and _looks_like_newlines_only(k, gv):
                print(f"  [OK*  ] {section:9s} {k}")
                print(f"           differs from the golden ONLY in line endings (CRLF vs LF).")
                print(f"           Not a code change -- Python is newline-agnostic. Re-run "
                      f"`capture` once to")
                print(f"           refresh the golden to the normalised hash; no code review "
                      f"needed for this.")
                continue
            status = "OK" if gv == cv else "DRIFT"
            if gv != cv:
                drift.append((section, k, gv, cv))
            print(f"  [{status:5s}] {section:9s} {k}")
    if drift:
        print("\nNR FROZEN CHECK FAILED -- the recall-critical path changed:")
        for section, k, gv, cv in drift:
            print(f"  {section}:{k}\n    golden={gv}\n    now   ={cv}")
        return 1
    print("\nNR FROZEN CHECK PASSED -- detection/routing whole-file and "
          "apply_llm/llm_classify/_SYSTEM_PROMPT are byte-identical to baseline.")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "check"
    if cmd == "capture":
        return capture()
    if cmd == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
