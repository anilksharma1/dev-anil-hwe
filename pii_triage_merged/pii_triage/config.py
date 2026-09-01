"""Configuration and the Master List of entity definitions.

The Master List is data-driven (definitions only -- no real values, so nothing
leaks across matters). It mirrors the Cognicion CIR Review Protocol's PI types.
Each entity declares:
  key        unique id
  label      display name shown in "Entities Found"
  category   the protocol PI-Type category (for the PI Categories column)
  method     regex | keyword | name | address
  patterns   regex strings for regex/keyword methods
  per_person True for ~one-per-person identifiers used to estimate head count
  luhn       True if regex matches must pass a Luhn checksum (cards)
  casefold   True to de-duplicate matches case-insensitively (emails)
  weak       True for a topic MENTION rather than an actual value (all keyword
             methods, and a bare name); a weak-only file does not flag in the
             rules fallback -- it must reach the LLM or pair with a real value.

External lists load with --rulepack (JSON via stdlib, YAML if PyYAML present).
Partial lists inherit unspecified keys from this default.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict


def load_dotenv(path: str = ".env", override: bool = False) -> int:
    """Minimal, dependency-free .env loader (the VM blocks PyPI, so no python-dotenv).

    Reads KEY=VALUE lines from `path` into os.environ. Blank lines and lines
    starting with '#' are ignored; a leading 'export ' is allowed; surrounding
    single/double quotes around the value are stripped. Real environment
    variables are NOT overwritten unless override=True. Put comments on their own
    line (an inline '#' is treated as part of the value). Returns the number of
    variables set (0 if the file is absent), and never prints any values.
    """
    if not path or not os.path.isfile(path):
        return 0
    count = 0
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line[:7].lower() == "export ":
                line = line[7:].lstrip()
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and (override or key not in os.environ):
                os.environ[key] = val
                count += 1
    return count

# Protocol PI-Type categories (mirrors the coding-layout choice fields).
CAT_CONTACT = "Contact Information"
CAT_GOVID = "Government-Issued Identification"
CAT_BIRTH = "Birth Information"
CAT_FIN = "Financial Account Information"
CAT_ACCESS = "Access Credentials"
CAT_HEALTH = "Health-Related Information"
CAT_BIO = "Biometric Data"
CAT_FAMILY = "Family Information"
CAT_DEMO = "Demographic Information"
CAT_STUDENT = "Student-Related Information"
CAT_WORK = "Work-Related Information"

DEFAULT_RULEPACK: dict = {
    "name": "default",
    "version": "2.3",
    "bde_threshold": 51,
    "bucket_edges": [10, 20, 50, 100],   # 0-10, 10-20, 20-50, 50-100, 100+
    "entities": [
        # --- Data-subject identity ---
        # weak: a name ALONE is not responsive (protocol). It flags only alongside
        # another signal (a name WITH a PI type), or when the LLM confirms it.
        {"key": "NAME", "label": "Name", "category": "", "method": "name",
         "per_person": True, "weak": True},

        # --- Contact Information ---
        {"key": "ADDRESS", "label": "Address", "category": CAT_CONTACT, "method": "address"},
        {"key": "EMAIL", "label": "Email", "category": CAT_CONTACT, "method": "regex",
         "per_person": True, "casefold": True,
         "patterns": [r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"]},
        {"key": "PHONE", "label": "Phone", "category": CAT_CONTACT, "method": "regex",
         "per_person": True,
         "patterns": [r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"]},

        # --- Government-Issued Identification ---
        {"key": "SSN", "label": "SSN", "category": CAT_GOVID, "method": "ssn",
         # method "ssn" matches dashed, spaced, and label-adjacent 9-digit SSNs.
         "per_person": True, "patterns": []},
        {"key": "PASSPORT", "label": "Passport", "category": CAT_GOVID, "method": "labeled_value",
         "label_pattern": r"passport(?:\s*(?:no\.?|number|num|#))?",
         "value_pattern": r"[A-Za-z0-9][A-Za-z0-9-]{4,16}", "min_digits": 4, "window": 30},
        {"key": "DRIVER_LICENSE", "label": "Driver License", "category": CAT_GOVID,
         "method": "labeled_value",
         "label_pattern": r"driver'?s?\s*licen[sc]e(?:\s*(?:no\.?|number|#))?|\bdl\s*(?:no\.?|number|#)",
         "value_pattern": r"[A-Za-z0-9][A-Za-z0-9-]{4,16}", "min_digits": 4, "window": 30},
        {"key": "TIN", "label": "Tax ID", "category": CAT_GOVID, "method": "labeled_value",
         # EIN excluded on purpose (organizational); requires an actual ID value by the label.
         "label_pattern": r"taxpayer\s*id(?:entification)?(?:\s*(?:no\.?|number))?|\btin\b|"
                          r"tax\s*id(?:entification)?(?:\s*(?:no\.?|number))?",
         "value_pattern": r"\d{2}-?\d{7}|\d{9}", "min_digits": 9, "window": 25},
        {"key": "NATIONAL_ID", "label": "National/NI Number", "category": CAT_GOVID,
         "method": "labeled_value",
         "label_pattern": r"national\s*(?:insurance|id)(?:\s*(?:no\.?|number|#))?|\bnino\b|"
                          r"social\s*insurance(?:\s*(?:no\.?|number))?",
         "value_pattern": r"[A-Z]{2}\d{6}[A-D]|[A-Za-z0-9-]{6,12}", "min_digits": 6, "window": 25},
        {"key": "ALIEN_REG", "label": "Alien Registration", "category": CAT_GOVID,
         "method": "keyword", "patterns": [r"\b(?:alien registration|a-?number|uscis)\b"]},
        {"key": "MILITARY_ID", "label": "Military ID", "category": CAT_GOVID,
         "method": "keyword", "patterns": [r"\b(?:military id|\bdod id\b|service number)\b"]},
        {"key": "TRIBAL_ID", "label": "Tribal ID", "category": CAT_GOVID,
         "method": "keyword", "patterns": [r"\btribal id\b"]},

        # --- Birth Information ---
        {"key": "DOB", "label": "Date of Birth", "category": CAT_BIRTH, "method": "labeled_value",
         "label_pattern": r"date\s*of\s*birth|d\.?o\.?b\.?|born(?:\s*on)?|birth\s*date",
         "value_pattern": r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                          r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
         "min_digits": 0, "window": 25},
        {"key": "BIRTH_CERT", "label": "Birth Certificate", "category": CAT_BIRTH,
         "method": "keyword", "patterns": [r"\bbirth certificate\b"]},

        # --- Financial Account Information ---
        {"key": "CARD", "label": "Payment Card", "category": CAT_FIN, "method": "regex",
         "per_person": True, "luhn": True, "patterns": [r"\b(?:\d[ -]?){13,19}\b"]},
        {"key": "BANK_ACCOUNT", "label": "Bank/Account Number", "category": CAT_FIN,
         "method": "labeled_value",
         "label_pattern": r"account\s*(?:no\.?|number|#)|\biban\b|routing\s*(?:no\.?|number)?|"
                          r"sort\s*code|loan\s*(?:no\.?|number)",
         "value_pattern": r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}|\d{6,17}", "min_digits": 6, "window": 30},
        {"key": "CARD_DETAIL", "label": "Card Expiry/CVV", "category": CAT_FIN,
         "method": "keyword",
         "patterns": [r"\b(?:cvv|cvc|security code|expiration date|exp\.? date)\b"]},
        {"key": "CARD_REF", "label": "Card/Transaction", "category": CAT_FIN,
         "method": "keyword",
         # Card-brand and transactional references (e.g. "put it on your AMEX").
         # No actual number, so it is a signal for review, not a strong identifier.
         "patterns": [r"\b(?:amex|american express|master\s?card|credit card|debit card|"
                      r"card ending|card number)\b"]},
        {"key": "FIN_BALANCE", "label": "Account Balance", "category": CAT_FIN,
         "method": "keyword", "patterns": [r"\b(?:account balance|available balance)\b"]},
        # Monetary amount. WEAK: a dollar figure alone is a business signal, not
        # personal -- it never flags on its own. But a money amount together with a
        # person's NAME is that individual's financial information (responsive), and
  # NB: NAME+MONEY is 'ambiguous', not clear_responsive -- routing.classify_ambiguity
    # strips Money/Amount before deciding, so money alone never auto-flags.
        {"key": "MONEY", "label": "Money/Amount", "category": CAT_FIN,
         "method": "money", "weak": True},

        # --- Access Credentials ---
        {"key": "CREDENTIALS", "label": "Credentials", "category": CAT_ACCESS,
         "method": "keyword",
         "patterns": [r"\bpassword\b", r"\b(?:security question|security answer)\b",
                      r"\bpin\s*(?:number|code|:)\b"]},

        # --- Health-Related Information ---
        {"key": "HEALTH", "label": "Health", "category": CAT_HEALTH, "method": "keyword",
         "patterns": [r"\b(?:diagnos\w*|medical history|mental or physical|treatment|prescription|"
                      r"patient|symptom)\b",
                      r"\b(?:insurance policy|policy number|subscriber id|claim number|"
                      r"health insurance)\b"]},

        # --- Biometric Data ---
        {"key": "BIOMETRIC", "label": "Biometric", "category": CAT_BIO, "method": "keyword",
         "patterns": [r"\b(?:fingerprint|voice ?print|genetic print|retina|iris (?:scan|image)|"
                      r"dna profile)\b"]},

        # --- Family Information ---
        {"key": "FAMILY", "label": "Family", "category": CAT_FAMILY, "method": "keyword",
         "patterns": [r"\b(?:mother'?s maiden name|maiden name|marriage certificate)\b"]},

        # --- Demographic Information ---
        {"key": "DEMOGRAPHIC", "label": "Demographic", "category": CAT_DEMO, "method": "keyword",
         "patterns": [r"\b(?:race/?ethnicity|ethnicity|sexual orientation|religion|"
                      r"religious|criminal conviction|trade union)\b"]},

        # --- Student-Related ---
        {"key": "STUDENT_ID", "label": "Student ID", "category": CAT_STUDENT, "method": "keyword",
         "patterns": [r"\bstudent id\b", r"\bstudent identification\b"]},

        # --- Work-Related ---
        {"key": "WORK", "label": "Work-Related", "category": CAT_WORK, "method": "keyword",
         "patterns": [r"\b(?:employee id|employee number|salary|compensation|"
                      r"disciplinary|performance evaluation|workers'? comp|"
                      r"employment application)\b"]},
    ],
}


def _legacy_to_entities(pack: dict) -> dict:
    if "entities" in pack:
        return pack
    ents = []
    luhn = set(pack.get("luhn_types", []))
    for key, pat in pack.get("patterns", {}).items():
        ents.append({"key": key, "label": key.title(), "method": "regex", "patterns": [pat],
                     "per_person": key in pack.get("per_person_types", []),
                     "luhn": key in luhn, "casefold": key == "EMAIL"})
    for key, pat in pack.get("keywords", {}).items():
        ents.append({"key": key, "label": key.title(), "method": "keyword", "patterns": [pat]})
    pack = dict(pack)
    pack["entities"] = ents
    return pack


def load_rulepack(path: str | None) -> dict:
    if not path:
        return dict(DEFAULT_RULEPACK)
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise SystemExit("YAML rule pack supplied but PyYAML not installed") from exc
        pack = yaml.safe_load(raw)
    else:
        pack = json.loads(raw)
    merged = dict(DEFAULT_RULEPACK)
    pack = pack or {}
    # Convert legacy patterns/keywords format BEFORE merging, otherwise the
    # default's `entities` would mask it and the legacy rules would be ignored.
    if "entities" not in pack and ("patterns" in pack or "keywords" in pack):
        pack = _legacy_to_entities(pack)
    merged.update(pack)
    return merged


@dataclass
class Config:
    root: str
    rulepack: dict = field(default_factory=lambda: dict(DEFAULT_RULEPACK))
    bde_threshold: int = 51
    bde_pdf_min_pages: int = 50         # BDE counter fires on PDFs with >= this many pages (roster-sized)
    use_bde_count_llm: bool = True      # separate BDE-only entity-count LLM call (needs use_llm)
    # BDE tier is decided by the LLM person-count on any responsive file whose candidate-entity
    # estimate reaches this floor (a file with fewer PII tokens than this cannot hold this many
    # distinct people). Set to bde_threshold to only count near/over-threshold files; lower =
    # more thorough + more LLM calls. Files below the floor fall back to the estimate.
    bde_count_min_entities: int = 7
    ocr_max_pages: int = 15             # OCR at most this many pages per file (0 = whole doc)
    # ---- Stage 2 (3.0.0) ---------------------------------------------------- #
    use_image_ocr: bool = True          # OCR content images embedded in text PDFs. This is
                                        # ~95% of DI calls on the CNG corpus; set False to
                                        # measure what it actually contributes.
    use_stage2: bool = True             # run Daniel's graded overview on stage-1 survivors
    stage2_on_all: bool = False         # ALSO grade files stage 1 cleared (reproduces an
                                        # ungated full-corpus stage-2 run; costs stage-1-NR
                                        # LLM calls, so off by default)
    s2_bde_threshold: int = 0           # 0 = inherit bde_threshold
    jurisdiction: str = ""              # "" | us | non-us -- read by the stage-2 prompt
    # Pricing, for the run summary. 0 = report units instead of money.
    price_per_1k_in: float = 0.0
    price_per_1k_out: float = 0.0
    price_per_1k_pages: float = 0.0     # Document Intelligence, per 1,000 pages
    use_ner: bool = False               # spaCy NER off by default (locked-down VMs)
    use_ocr: bool = False               # Azure Document Intelligence on non-searchable files
    use_llm: bool = False               # Azure OpenAI on ambiguous files only
    llm_deployment: str = ""            # Azure OpenAI deployment name (e.g. gpt-4.5-nano)
    doc_intel_model: str = "prebuilt-layout"
    protocol_text: str = ""             # matter protocol injected into LLM judgment
    timeout_s: int = 60
    max_bytes: int = 1 << 30
    max_scan_chars: int = 5_000_000
    llm_input_chars: int = 24_000       # chars sampled from a file for the LLM prompt
    max_scan_rows: int = 200_000
    zip_ratio_limit: int = 200
    log_prompts: bool = False   # log system prompts + user char counts at DEBUG; env: LOG_LLM_PROMPTS

    def to_manifest(self) -> dict:
        d = asdict(self)
        d["rulepack"] = {"name": self.rulepack.get("name"),
                         "version": self.rulepack.get("version"),
                         "entities": [e["key"] for e in self.rulepack.get("entities", [])]}
        # Don't dump the whole protocol into the manifest; record only its presence.
        d["protocol_text"] = f"<{len(self.protocol_text)} chars>" if self.protocol_text else ""
        return d