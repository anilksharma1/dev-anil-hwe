"""Output schema, entity bucketing, complexity bucketing, and lane routing."""
from __future__ import annotations

from dataclasses import dataclass, fields

DEFAULT_BUCKET_EDGES = [10, 20, 50, 100]   # -> 0-10, 10-20, 20-50, 50-100, 100+
PAGE_BANDS = [(4, "1-4 pages"), (10, "5-10 pages"), (50, "11-50 pages")]

# Validated, unambiguous individual identifiers. Their presence makes a file
# clearly responsive (no LLM needed). A payment-card number is deliberately NOT
# here: bare card-shaped numbers are often order/transaction IDs, so a card-bearing
# file goes to the LLM to judge individual-vs-business rather than auto-flagging.
STRONG_KEYS = ("SSN",)
# A structured file that reads as zero entities but is at least this many bytes is treated
# as a failed extraction (not an empty sheet) and routed to review rather than cleared.
# Observed silent-clear failures on the 200k were all >10KB; tune against the size
# distribution of est=0 structured files if review volume needs trimming.
STRUCTURED_ZERO_MIN_BYTES = 6000


def classify_ambiguity(counts: dict, labels, is_structured: bool,
                       structured_rows: int = 0, structured_total_rows: int = 0,
                       strong_keys=STRONG_KEYS) -> str:
    """Decide whether the LLM should be consulted for a searchable file.

    clear_responsive     -> a strong individual identifier (SSN) is present; no LLM
    clear_non_responsive -> nothing found; no LLM
    ambiguous            -> signals present but no strong identifier; the LLM judges
                            person-vs-business. This now includes spreadsheets/tables
                            that carry PII labels or identifier-bearing rows but no
                            SSN -- previously those were auto-decided by row count.

    structured_rows is the count the identifier RULES recognized as person-bearing;
    structured_total_rows is the raw row count of the sheet. A big table where the
    rules recognized FEW/zero rows is exactly the under-read roster the tool used to
    clear silently -- so any structured file with rows to read is sent to the LLM,
    which reads the actual rows rather than trusting the rule count. Gated to
    structured files, so this does not widen LLM volume on unstructured PII.
    """
    if any(counts.get(k, 0) > 0 for k in strong_keys):
        return "clear_responsive"
    # NAME + a monetary amount is NOT auto-flagged. Benchmarking against gold showed
    # this combo is dominated by business/transaction data -- invoices, receipts,
    # orders, statements, vendor/B2B -- where a name sits next to a dollar figure, and
    # it was by far the largest over-call source. It now goes to the LLM, which keeps
    # genuine individual financial records (a person's own pay/benefits/account) and
    # clears the business ones. The recall-first fallback (value_signal, see
    # choose_lane) still flags it when the LLM is unavailable, so a name+money file is
    # never silently cleared. ("Name" is a meaningful label, so it falls through to the
    # ambiguous branch below and the LLM is consulted.)
    # A money amount on its own is only a weak business signal, so it does not by
    # itself send a file to the LLM (that would flood the AI on a payroll population).
    meaningful = [lab for lab in labels if lab != "Money/Amount"]
    if meaningful:
        return "ambiguous"
    # Structured files: send to the LLM whenever there are rows to read -- either the
    # rules recognized person-bearing rows, OR the sheet simply has rows (an under-read
    # roster). Reading the rows is how the LLM catches rosters the rules under-counted.
    if is_structured and (structured_rows > 0 or structured_total_rows > 0):
        return "ambiguous"
    return "clear_non_responsive"


@dataclass
class FileRecord:
    rel_path: str
    file_name: str
    ext: str
    size_bytes: int
    status: str = "ok"
    searchable: bool = False
    programmatic: bool = False
    text_extractable: str = ""
    is_structured: bool = False
    page_or_sheet_count: int = 0
    attachment_count: int = 0
    estimated_entities: int = 0
    estimate_truncated: bool = False
    bde_person_count: int = 0          # entity count from the SEPARATE BDE-only LLM call (0 if not run); never feeds NR
    bde_confirmed: bool = False        # the BDE counter READ >= threshold people -> route to BDE review, never clear
    entity_bucket: str = ""
    entities_found: str = ""           # "Name | SSN | Address" -- labels, never values
    value_signal: bool = False         # rules found actual personal data (not a bare mention)
    pi_categories: str = ""            # protocol PI-Type categories present
    is_bde: bool = False
    complexity_bucket: str = ""        # for non-searchable files
    ambiguity: str = ""                # clear_responsive | clear_non_responsive | structured | ambiguous
    llm_consulted: bool = False        # did the LLM make the responsiveness call on this file
    llm_responsive: str = ""           # "yes" | "no" | "" -- the LLM's decision, when consulted
    llm_tokens: int = 0                # total Azure OpenAI tokens spent judging this file (0 if AI not used)
    suggested_lane: str = ""
    detail: str = ""
    # ---- END OF THE 27 LEGACY COLUMNS -------------------------------------- #
    # Everything above is byte-for-byte the 2.10.2 schema, in the same order, with the
    # same meaning, and is written ONLY by stage 1. report.build_table1,
    # benchmark.run_benchmark and score_bde.py all read by column name from this block,
    # so it must not be reordered or renamed. Everything below is appended.

    # ---- OCR / Document Intelligence accounting (3.0.0) -------------------- #
    # Every field here exists because the corresponding number was previously
    # unobservable. text_extractable records OCR *failures* (apply_ocr overwrites
    # "image_only" -> "text" on success) and the embedded-image path -- the largest
    # cost centre by an order of magnitude -- reported nothing at all when its images
    # yielded no text. Sum di_calls over the CSV to get the billable-call total.
    ocr_attempted: bool = False        # a full-file DI call was made (billable even if it failed)
    ocr: bool = False                  # ...and it returned text (rescued image_only -> text)
    ocr_pages: int = 0                 # pages DI reported for the full-file call
    img_ocr_qualifying: int = 0        # embedded content images x_pdf found
    img_ocr_calls: int = 0             # of those, DI calls actually attempted (excludes <1KB skips)
    img_ocr_ok: int = 0                # ...that returned text
    img_decode_failed: int = 0         # images pypdf could not decode -- almost always missing Pillow
    di_calls: int = 0                  # ocr_attempted + img_ocr_calls: this file's billable DI calls
    elapsed_s: float = 0.0             # wall-clock for this file, all stages

    # ---- Derived stage-1 answers (3.0.0) ---------------------------------- #
    # Both are functions of the legacy columns. They exist so a reader does not have to
    # know that "likely_non_responsive" is the NR lane, or which stage owns is_bde.
    nr_stage1: bool = False            # suggested_lane == "likely_non_responsive" -- the NR-removal decision
    bde_stage1: bool = False           # alias of is_bde -- the BDE-by-stage-1 flag

    # ---- Stage 2 (3.0.0) -------------------------------------------------- #
    # Written ONLY by _stage2(), and only for files stage 1 did not clear. Blank on a
    # stage-1 NR file, with the reason recorded. No field here feeds any stage-1 column.
    s2_ran: bool = False
    s2_skip_reason: str = ""           # "" | stage1_nr | not_searchable | stage2_disabled | no_text
    s2_llm_consulted: bool = False
    s2_llm_responsiveness: str = ""    # clear_yes | likely_yes | borderline | likely_no | clear_no
    s2_llm_responsive: str = ""        # "yes" | "no" -- the graded level collapsed
    s2_llm_tokens: int = 0
    s2_is_bde: bool = False            # BDE at the stage-2 threshold
    s2_lane: str = ""                  # stage 2's own lane
    s2_nr: bool = False                # s2_lane == "likely_non_responsive" -- the R/NR-by-stage-2 decision
    s2_detail: str = ""

    # ---- Rollup ----------------------------------------------------------- #
    llm_tokens_total: int = 0          # llm_tokens + s2_llm_tokens -- the figure for the tracker


FIELDNAMES = [f.name for f in fields(FileRecord)]

# The 27 columns that existed in 2.10.2, pinned so a reorder or rename fails a test
# rather than silently breaking report.py / benchmark.py / score_bde.py.
LEGACY_FIELDNAMES = [
    "rel_path", "file_name", "ext", "size_bytes", "status", "searchable",
    "programmatic", "text_extractable", "is_structured", "page_or_sheet_count",
    "attachment_count", "estimated_entities", "estimate_truncated",
    "bde_person_count", "bde_confirmed", "entity_bucket", "entities_found",
    "value_signal", "pi_categories", "is_bde", "complexity_bucket", "ambiguity",
    "llm_consulted", "llm_responsive", "llm_tokens", "suggested_lane", "detail",
]

# Fields stage 2 is permitted to write. Enforced by test_stage2_cannot_write_stage1_fields.
STAGE2_FIELDNAMES = [
    "s2_ran", "s2_skip_reason", "s2_llm_consulted", "s2_llm_responsiveness",
    "s2_llm_responsive", "s2_llm_tokens", "s2_is_bde", "s2_lane", "s2_nr", "s2_detail",
]

NR_LANE = "likely_non_responsive"


def bucket_of(n: int, edges=DEFAULT_BUCKET_EDGES) -> str:
    prev = 0
    for edge in edges:
        if n <= edge:
            return f"{prev}-{edge}"
        prev = edge
    return f"{prev}+"


def complexity_bucket(pages: int) -> str:
    for limit, label in PAGE_BANDS:
        if pages <= limit:
            return label
    return "51+ pages"


def estimate_entities(meta: dict, counts: dict, labels, per_person_keys) -> int:
    if meta.get("is_structured"):
        return int(meta.get("structured_entity_rows", 0))
    per_person_max = max((counts.get(k, 0) for k in per_person_keys), default=0)
    if per_person_max == 0 and labels:
        return 1
    return per_person_max


def roster_entity_estimate(meta: dict, base_estimate: int, threshold: int) -> int:
    """Recover under-read rosters. A structured file whose identifier rules recognized
    FEWER rows than the BDE threshold, yet has at least `threshold` TOTAL rows, is almost
    certainly a roster the rules under-read (name/DOB columns NER missed, an unusual
    layout, or only a handful of rows matched) -- exactly the '51+ entities' spreadsheets
    manual review flags as responsive but the tool was under-counting and clearing. Using
    the total row count makes it reach the LLM (and flag in the rules fallback) rather than
    being silently cleared or routed below the BDE threshold. Non-roster big tables
    (inventory, transaction logs) still get cleared -- by the LLM, which reads the rows and
    sees they aren't people.

    This fires for ANY under-count below the threshold, not only the base_estimate == 0
    case. A 500-row roster where the rules recognized 3 rows (base_estimate 3) was
    previously left at 3 -- under 51 -- and missed; it is now bumped to its true row count.
    Files whose recognized count already meets the threshold are left unchanged (no
    double-count). threshold <= 0 disables this."""
    if (threshold and threshold > 0 and meta.get("is_structured")
            and base_estimate < threshold):
        total = int(meta.get("structured_total_rows", 0) or 0)
        if total >= threshold:
            return total
    return base_estimate


def choose_lane(rec: FileRecord) -> str:
    if rec.status == "container":
        return "container_expand"
    if rec.status != "ok":
        if rec.text_extractable == "needs_conversion":
            return "convert_lane"
        if rec.status == "no_parser":
            return "needs_parser"
        if rec.status == "skipped_too_large":
            return "manual_oversize"
        return "review_error"
    if not rec.searchable:
        # Non-searchable -> the sample-and-extrapolate workflow, by complexity.
        return "nonsearchable_sample"
    # A CONFIRMED BDE: the separate BDE counter READ >= threshold distinct people from a
    # file the row-parser couldn't structure (e.g. a payroll register). This is a real
    # count, not a heuristic, so the file must reach BDE review and is NEVER cleared --
    # even if the NR call judged it non-responsive. This only ever pulls a file OUT of the
    # cleared bucket (promote), so it cannot lower NR recall. (A heuristic row-count bump,
    # by contrast, is NOT bde_confirmed and still flows through the LLM/clear path below,
    # so the LLM can still clear a bumped non-person table -- 2.9.12 behavior preserved.)
    if rec.bde_confirmed:
        return "structured_bde" if rec.is_structured else "bde"
    # Recall-first guard: a STRUCTURED file that read ZERO entities yet has real content
    # (nonzero size) is an extraction failure wearing a spreadsheet extension -- the parser
    # opened it, got no rows, and would otherwise fall through to likely_non_responsive and
    # be SILENTLY CLEARED. On the 200k these were ~600 real rosters (human counts in the
    # hundreds) cleared as "0 entities". We cannot tell an unreadable roster from a genuinely
    # empty sheet at this point, so -- since clearing a roster is the catastrophic error and
    # overflagging is safe -- never clear it; route to review instead. (STRUCTURED_ZERO_MIN_BYTES
    # keeps genuinely trivial/empty files out; observed failures are all >10KB.)
    if rec.is_structured and rec.estimated_entities == 0 and rec.size_bytes > STRUCTURED_ZERO_MIN_BYTES:
        return "structured_unreadable"
    # The LLM's call wins ONLY on ambiguous files (weak signals, no strong ID). Files
    # with a strong identifier (SSN) are clear_responsive and never reach the LLM, so
    # the AI can never clear them -- it only trims the genuinely ambiguous over-calls.
    #
    # When the LLM is off OR its call failed/timed out, we fall back to the rules. That
    # fallback flags on an actual personal VALUE (or identifier-bearing structured rows),
    # NOT on a bare topic mention or a name on its own -- those are the protocol's
    # explicit "not responsive" cases and were the main over-calls. It still never
    # undercalls: any real value, a name paired with another signal, or structured
    # identifier rows all flag (see detection.value_signal).
    if rec.llm_consulted:
        responsive = rec.llm_responsive == "yes"
    else:
        responsive = rec.value_signal or (rec.is_structured and rec.estimated_entities > 0)
    if not responsive:
        return "likely_non_responsive"
    if rec.is_bde and rec.is_structured:
        return "structured_bde"
    if rec.is_bde:
        return "bde"
    return "standard"