"""CLI: scan, report, sample, estimate, benchmark."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import Config, load_rulepack, load_dotenv
from .extractors import optional_dependency_report, get_extractor
from .runner import discover_files, run
from .benchmark import run_benchmark
from .report import build_table1
from .sampling import draw_sample, estimate


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pii_triage",
        description="Read-only, parallel, crash-safe PII triage scanner (HWE bucketing).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--version", action="version", version=f"pii_triage {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="Scan a folder -> inventory CSV.")
    s.add_argument("root")
    s.add_argument("--out", default="inventory.csv")
    s.add_argument("--rulepack", default=None, help="Master List JSON/YAML (default: built-in).")
    s.add_argument("--workers", type=int, default=max(1, os.cpu_count() or 1))
    s.add_argument("--bde-threshold", type=int, default=None)
    s.add_argument("--ner", action="store_true",
                   help="Use spaCy NER for names IF installed (default: heuristic, no install).")
    s.add_argument("--ocr", action="store_true",
                   help="OCR non-searchable files via Azure Document Intelligence (needs Azure).")
    s.add_argument("--llm", action="store_true",
                   help="Consult Azure OpenAI on AMBIGUOUS files only (needs Azure).")
    s.add_argument("--llm-deployment", default="",
                   help="Azure OpenAI deployment name (else AZURE_OPENAI_DEPLOYMENT env).")
    s.add_argument("--protocol", default=None,
                   help="Matter protocol doc (PDF/DOCX/TXT); injected as LLM judgment context.")
    s.add_argument("--env-file", default=".env",
                   help="Read KEY=VALUE settings (e.g. Azure endpoints) from this file if present.")
    s.add_argument("--timeout", type=int, default=60)
    s.add_argument("--max-bytes", type=int, default=1 << 30)
    s.add_argument("--max-scan-chars", type=int, default=5_000_000)
    s.add_argument("--max-scan-rows", type=int, default=200_000)
    s.add_argument("--chunksize", type=int, default=16)
    s.add_argument("--progress-interval", type=float, default=0.5)
    s.add_argument("--restart", action="store_true")
    # ---- stage 2 (3.0.0) --------------------------------------------------- #
    s.add_argument("--no-stage2", action="store_true",
                   help="Stage 1 only (Anna's NR/BDE decision). Use to reproduce a 2.10.2 "
                        "inventory exactly, or to re-establish the stage-1 baseline.")
    s.add_argument("--stage2-on-all", action="store_true",
                   help="Also grade files stage 1 cleared, instead of gating stage 2 behind "
                        "it. Reproduces an ungated full-corpus stage-2 run and costs extra "
                        "LLM calls on the NR set.")
    s.add_argument("--s2-bde-threshold", type=int, default=None,
                   help="BDE threshold for stage 2 (default: same as --bde-threshold).")
    s.add_argument("--jurisdiction", default="", choices=["", "us", "non-us"],
                   help="Override jurisdiction for all files (default: the LLM infers per file).")
    # ---- OCR cost control (3.0.0) ------------------------------------------ #
    s.add_argument("--ocr-max-pages", type=int, default=None,
                   help="Page cap for full-file PDF OCR. 0 = whole document. Previously a "
                        "hardcoded 15 with no way to change it.")
    s.add_argument("--no-image-ocr", action="store_true",
                   help="Skip OCR of content images embedded in text PDFs. This path is the "
                        "largest OCR cost centre (~95%% of DI calls on the CNG corpus); use "
                        "this to measure what it actually contributes.")
    # ---- pricing, for the run summary ------------------------------------- #
    s.add_argument("--price-per-1k-in", dest="price_in", type=float, default=None,
                   help="Azure OpenAI input price per 1k tokens, for the cost summary.")
    s.add_argument("--price-per-1k-out", dest="price_out", type=float, default=None,
                   help="Azure OpenAI output price per 1k tokens.")
    s.add_argument("--price-per-1k-pages", dest="price_pages", type=float, default=None,
                   help="Document Intelligence price per 1,000 pages.")
    s.add_argument("--log-prompts", action="store_true",
                   help="Log LLM system prompts and user message sizes at DEBUG level "
                        "(also toggled by LOG_LLM_PROMPTS=true env var; never logs file content).")

    r = sub.add_parser("report", help="Build Table 1 (searchable, per-file) from an inventory.")
    r.add_argument("inventory")
    r.add_argument("--out", default="table1_searchable.csv")

    sp = sub.add_parser("sample", help="Draw a per-bucket sample of non-searchable files.")
    sp.add_argument("inventory")
    sp.add_argument("--out", default="nonsearchable_sample.csv")
    sp.add_argument("--rate", type=float, default=0.05, help="Sampling rate (e.g., 0.05 = 5%%).")
    sp.add_argument("--seed", type=int, default=12345)

    e = sub.add_parser("estimate",
                       help="Extrapolate a coded sample -> Table 2 (non-searchable).")
    e.add_argument("inventory")
    e.add_argument("coded_sample", help="The sample CSV with gold_responsive / gold_bde filled.")
    e.add_argument("--out", default="table2_nonsearchable.csv")

    b = sub.add_parser("benchmark", help="Score an inventory against a gold set (.xlsx or .csv).")
    b.add_argument("inventory")
    b.add_argument("gold", help="Your results file (.xlsx or .csv) with a file column and a responsive/NR column.")
    b.add_argument("--id-col", default=None, help="Gold column holding the file name/path (else auto-detect).")
    b.add_argument("--responsive-col", default=None, help="Gold column holding responsive/NR (else auto-detect).")
    b.add_argument("--bde-col", default=None, help="Gold column holding the BDE flag (optional).")
    b.add_argument("--sheet", default=None, help="Worksheet/tab name to read (xlsx with multiple tabs).")
    b.add_argument("--absent-means", choices=["unreviewed", "zero"], default="unreviewed",
                   help="Inventory files NOT in the gold: 'unreviewed' (default) skips them; "
                        "'zero' scores them as non-responsive (assume zero entities).")
    b.add_argument("--bde-threshold", type=int, default=None,
                   help="Re-score the BDE flag at this entity threshold (from estimated_entities) "
                        "instead of the run's own is_bde. Lets a run scanned at 51+ be scored at 7+.")
    return p


def _cmd_scan(a) -> int:
    root = os.path.abspath(a.root)
    if not os.path.isdir(root):
        sys.stderr.write(f"error: not a directory: {root}\n")
        return 2
    # Load settings from a .env first (parent process), so endpoints/credentials
    # are in the environment before the preflight and before workers are spawned
    # (spawned workers inherit the parent's environment). Values are never printed.
    loaded = load_dotenv(a.env_file)
    if loaded:
        sys.stderr.write(f"loaded {loaded} setting(s) from {a.env_file}\n")
    pack = load_rulepack(a.rulepack)
    bde = a.bde_threshold if a.bde_threshold is not None else pack.get("bde_threshold", 51)
    cfg = Config(root=root, rulepack=pack, bde_threshold=bde, use_ner=a.ner,
                 use_ocr=a.ocr, use_llm=a.llm, llm_deployment=a.llm_deployment,
                 timeout_s=a.timeout, max_bytes=a.max_bytes,
                 max_scan_chars=a.max_scan_chars, max_scan_rows=a.max_scan_rows,
                 use_stage2=not a.no_stage2, stage2_on_all=a.stage2_on_all,
                 s2_bde_threshold=(a.s2_bde_threshold or 0),
                 jurisdiction=a.jurisdiction)
    if a.ocr_max_pages is not None:
        cfg.ocr_max_pages = a.ocr_max_pages
    if a.no_image_ocr:
        cfg.use_image_ocr = False
    if a.log_prompts:
        cfg.log_prompts = True
    for _attr, _val in (("price_per_1k_in", a.price_in), ("price_per_1k_out", a.price_out),
                        ("price_per_1k_pages", a.price_pages)):
        if _val is not None:
            setattr(cfg, _attr, _val)

    # Read the matter protocol (any supported doc type) for LLM judgment context.
    if a.protocol:
        from .detection import CompiledRules
        _proto_ext = os.path.splitext(a.protocol)[1].lower()
        pe = get_extractor(_proto_ext)
        if pe is None:
            sys.stderr.write(
                f"warning: --protocol file has unsupported extension '{_proto_ext}'; "
                "proceeding without protocol\n")
        else:
            try:
                cfg.protocol_text = pe(a.protocol, cfg, CompiledRules.from_pack(pack))[0]
            except Exception as exc:
                sys.stderr.write(f"warning: could not read --protocol file ({type(exc).__name__}: {exc}); proceeding without it\n")

    # Preflight: warn (don't silently no-op) if Azure enrichment was requested but
    # the SDK/endpoint isn't available, so the user knows it degraded to rules.
    if a.ocr or a.llm:
        from .azure_clients import get_ocr_fn, get_llm_fn
        if a.ocr and get_ocr_fn(cfg) is None:
            sys.stderr.write("warning: --ocr set but Azure Document Intelligence is "
                             "unavailable/unconfigured; non-searchable files will be sampled, not OCR'd\n")
        if a.llm and get_llm_fn(cfg) is None:
            sys.stderr.write("warning: --llm set but Azure OpenAI is unavailable/"
                             "unconfigured; ambiguous files fall back to rules\n")

    missing = optional_dependency_report()
    if "Pillow" in missing and a.ocr:
        sys.stderr.write(
            "WARNING: Pillow is not installed. pypdf cannot decode embedded images, so OCR of\n"
            "         content images inside text PDFs is SILENTLY DISABLED -- normally the largest\n"
            "         part of the OCR workload. Install it (pip install Pillow) or expect a much\n"
            "         smaller DI bill and less text reaching both stages.\n")
    sys.stderr.write(
        f"pipeline: stage 1 (NR/BDE) "
        f"{'+ stage 2 (graded overview) on ' + ('ALL files' if a.stage2_on_all else 'stage-1 survivors') if not a.no_stage2 else 'ONLY (--no-stage2)'}\n")
    sys.stderr.write(f"pii_triage {__version__}\nscanning: {root}\n"
                     f"Master List: {pack.get('name')} v{pack.get('version')}  "
                     f"BDE threshold: {bde}  NER: {'on' if cfg.use_ner else 'heuristic'}\n"
                     f"  OCR: {'on' if cfg.use_ocr else 'off'}  "
                     f"LLM: {'on (ambiguous only)' if cfg.use_llm else 'off'}"
                     f"{'  protocol: loaded' if cfg.protocol_text else ''}\n")
    missing = optional_dependency_report()
    if missing:
        sys.stderr.write(f"note: optional libs missing -> graceful degrade: {', '.join(missing)}\n")
    sys.stderr.write("discovering files...\n")
    paths = discover_files(root)
    if not paths:
        sys.stderr.write("no files found.\n")
        return 0
    sys.stderr.write(f"found {len(paths):,} files; workers={a.workers}\n")

    summary = run(cfg, paths, a.out, a.workers, a.progress_interval, a.chunksize, a.restart)
    lanes = summary["lane_counts"]
    sys.stderr.write(
        f"\nDONE in {summary['elapsed_s']}s -> {a.out}  (manifest -> {a.out}.manifest.json)\n"
        "  lanes: " + "  ".join(f"{k}={v:,}" for k, v in sorted(lanes.items())) + "\n"
        f"  next: `report {a.out}` (Table 1)  |  `sample {a.out}` then `estimate` (Table 2)\n")
    return 0


def main(argv=None) -> int:
    a = _build_parser().parse_args(argv)
    if a.command == "scan":
        return _cmd_scan(a)
    if a.command == "report":
        n = build_table1(a.inventory, a.out)
        sys.stderr.write(f"Table 1 (searchable): {n} files -> {a.out}\n")
        return 0
    if a.command == "sample":
        n = draw_sample(a.inventory, a.out, a.rate, a.seed)
        sys.stderr.write(f"sample drawn: {n} files -> {a.out}  "
                         f"(reviewers fill gold_responsive / gold_bde, then run `estimate`)\n")
        return 0
    if a.command == "estimate":
        table = estimate(a.inventory, a.coded_sample, a.out)
        sys.stderr.write(f"Table 2 (non-searchable): {len(table)} buckets -> {a.out}\n")
        for row in table:
            sys.stderr.write(f"  bucket {row['Bucket ID']} [{row['Bucket']}]: "
                             f"{row['# of Files']} files, {row['% Responsive']} resp, "
                             f"{row['% BDE']} BDE -> pred {row['# of Responsive (Predicted)']} resp / "
                             f"{row['# of BDEs (Predicted)']} BDE\n")
        return 0
    if a.command == "benchmark":
        run_benchmark(a.inventory, a.gold, id_col=a.id_col,
                      responsive_col=a.responsive_col, bde_col=a.bde_col, sheet=a.sheet,
                      absent_means=a.absent_means, bde_threshold=a.bde_threshold)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
