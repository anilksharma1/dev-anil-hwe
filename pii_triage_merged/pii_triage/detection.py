"""Detection layer (Master List driven). Counts, labels, and categories only --
never values. Matched values are held in local sets and discarded on return.

Name detection uses spaCy NER only if explicitly enabled AND available. The
default fallback uses ONLY high-precision structural cues -- a title ("Mr./Dr.
Lastname") or an explicit field label ("Name:", "Patient:") -- and never a list
of specific first names (that approach both misses real names and false-matches
non-names). Free-floating names are deliberately left for the LLM, which fires
on ambiguous files; entity *counts* otherwise lean on strong identifiers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NLP = None
_NLP_TRIED = False


def _get_nlp(enabled: bool):
    global _NLP, _NLP_TRIED
    if not enabled:
        return None
    if not _NLP_TRIED:
        _NLP_TRIED = True
        try:
            import spacy
            _NLP = spacy.load("en_core_web_sm", disable=["lemmatizer", "tagger", "parser"])
        except Exception:
            _NLP = None
    return _NLP


# Names are detected ONLY via reliable structure -- no list of specific first
# names. A title or an explicit field label is strong evidence of a person;
# bare capitalised word pairs (which could be orgs, places, products) are NOT
# guessed here and are left for the LLM on ambiguous files.
_NAME_WORD = r"[A-Z][A-Za-z'\u2019\-]+"
_NAME_RUN = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{0,2}}"
_TITLE = re.compile(rf"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof|Mx)\.?\s+({_NAME_RUN})")
_LABELED = re.compile(
    rf"(?i:\b(?:full name|first name|last name|name|patient|employee|insured|member|"
    rf"customer|client|data subject|applicant|signed))\s*[:\-]\s*({_NAME_RUN})")
_DEAR = re.compile(rf"\bDear\s+({_NAME_RUN})")
_NAME_RXS = (_TITLE, _LABELED, _DEAR)

# A row that STARTS with "Lastname, Firstname" -- the per-person marker in roster /
# payroll-register spreadsheets where each individual's row carries a name but no other
# identifier on that same row (the SSN/pay/deductions sit on following rows). Row-by-row
# identifier detection misses these, so a large register counts as 0 people; counting
# these name-lines recovers the true subject count. Requires a comma + a given name (and
# not a trailing digit) to avoid matching "Total, 1,234" style lines.
_ROSTER_NAME_LINE = re.compile(
    r"^\s*[A-Z][A-Za-z'\u2019\-]{1,}\,\s+[A-Z][A-Za-z.'\u2019\-]+(?:\s+[A-Z][A-Za-z.'\u2019\-]*)*")


def is_roster_name_line(text: str) -> bool:
    """True if a spreadsheet row begins with a 'Lastname, Firstname' personal name --
    the per-subject marker in roster/register layouts. Used to count subjects when the
    per-row identifier rules find none (name-only rows)."""
    if not text:
        return False
    head = text.strip()
    m = _ROSTER_NAME_LINE.match(head)
    if not m:
        return False
    # reject rows that are really labels/totals: the token after the comma must look like
    # a name, not a number.
    after = head[m.end(0):m.end(0) + 1]
    return not after.isdigit()


# Address = a real street line OR a "City, ST 12345" pattern. A bare 5-digit
# number is NOT treated as a ZIP/address (it false-matched ID-number fragments).
_RE_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s){0,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|"
    r"Way|Place|Pl|Terrace|Ter|Circle|Cir|Highway|Hwy)\b\.?", re.I)
_RE_CITY_STATE_ZIP = re.compile(r"\b[A-Z][A-Za-z.'\- ]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b")

# Monetary amounts: a currency symbol/code with a number, a thousands-separated
# number, or a two-decimal value. Deliberately liberal -- a money amount on its
# own is only a WEAK signal (a business invoice/total is not personal), but a
# money amount together with a person's NAME is that individual's financial
# information, which IS responsive under the protocol. This is the payroll /
# benefits / compensation pattern the rules previously cleared as "just a name".
_RE_MONEY = re.compile(
    r"[$£€]\s?\d[\d,]*(?:\.\d+)?"                                            # $1,234.56  £50  €1.000
    r"|\b(?:USD|EUR|GBP|CAD|AUD)\s?\$?\d[\d,]*(?:\.\d+)?"                    # USD 1,234
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|EUR|GBP|CAD|AUD|dollars?|euros?|pounds?)\b"  # 1,234 dollars
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?\b"                                 # 12,345  or  1,234.56
    r"|\b\d+\.\d{2}\b",                                                      # 1234.56
    re.I)


def detect_money(text: str) -> int:
    """Count distinct monetary amounts. Weak on its own; responsive next to a NAME."""
    return len({m.group().strip() for m in _RE_MONEY.finditer(text)})


def luhn_valid(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if d < 0 or d > 9:
            return False
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_iin_ok(digits: str) -> bool:
    """True only if the number starts with a real major-network issuer prefix and
    has a valid length for that network. Random 16-digit order/invoice/transaction
    numbers pass a Luhn checksum about 1 in 10 of the time but almost never carry a
    valid issuer prefix, so this removes those false positives without dropping any
    genuine card (all major networks are covered)."""
    n = len(digits)

    def pre(k: int) -> int:
        return int(digits[:k]) if n >= k else -1

    if digits[:1] == "4" and n in (13, 16, 19):                      # Visa
        return True
    if n == 16 and (51 <= pre(2) <= 55 or 2221 <= pre(4) <= 2720):   # Mastercard
        return True
    if n == 15 and pre(2) in (34, 37):                               # Amex
        return True
    if n in (16, 17, 18, 19) and (                                   # Discover
            pre(4) == 6011 or pre(2) == 65 or 644 <= pre(3) <= 649):
        return True
    if n in (14, 15, 16, 17, 18, 19) and (                          # Diners Club
            300 <= pre(3) <= 305 or pre(4) == 3095 or pre(2) in (36, 38, 39)):
        return True
    if n in (16, 17, 18, 19) and 3528 <= pre(4) <= 3589:            # JCB
        return True
    if n in (16, 17, 18, 19) and pre(2) == 62:                       # UnionPay
        return True
    return False


def card_valid(digits: str) -> bool:
    """A payment-card number must pass BOTH the Luhn checksum and an issuer-prefix
    check. Luhn alone flags any number that happens to checksum (e.g. order IDs)."""
    return luhn_valid(digits) and _card_iin_ok(digits)


def detect_names(text: str, use_ner: bool) -> set:
    nlp = _get_nlp(use_ner)
    if nlp is not None:
        doc = nlp(text[:100_000])
        return {ent.text.strip().lower() for ent in doc.ents if ent.label_ == "PERSON"}
    names = set()
    for rx in _NAME_RXS:
        for m in rx.finditer(text):
            names.add(m.group(1).strip().lower())
    return names


def detect_addresses(text: str) -> int:
    hits = {m.group().strip().lower() for m in _RE_ADDRESS.finditer(text)}
    hits |= {m.group().strip().lower() for m in _RE_CITY_STATE_ZIP.finditer(text)}
    return len(hits)


# SSNs appear dashed (123-45-6789), spaced (123 45 6789), or as a bare 9-digit
# run (123456789). Bare runs are everywhere in data (account/order numbers), so
# they are counted ONLY right after an SSN label -- catching real SSNs on forms
# without flagging random numbers.
_SSN_SEP = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")
_SSN_LABEL = re.compile(r"(?:\bssn\b|social\s+security(?:\s+(?:no\.?|number|num|#))?)", re.I)
_SSN_BARE = re.compile(r"\b\d{9}\b")


def _ssn_plausible(value: str) -> bool:
    d = re.sub(r"\D", "", value)
    if len(d) != 9:
        return False
    area, group, serial = d[:3], d[3:5], d[5:]
    if area in ("000", "666") or area >= "900":   # invalid SSA area numbers
        return False
    return group != "00" and serial != "0000"


def detect_ssn(text: str) -> int:
    hits = set()
    for m in _SSN_SEP.finditer(text):
        d = re.sub(r"\D", "", m.group())
        if _ssn_plausible(d):
            hits.add(d)
    if _SSN_LABEL.search(text):
        # The document is SSN-related, so count plausible 9-digit values even when the
        # label and the number get separated -- which happens with form-field values or
        # OCR reading order. (Bare 9-digit runs are NEVER counted without an SSN label.)
        for m in _SSN_BARE.finditer(text):
            if _ssn_plausible(m.group()):
                hits.add(m.group())
    return len(hits)


def detect_labeled_value(text, label_rx, value_rx, window=35, min_digits=0) -> int:
    """Count identifier VALUES that appear just after their label (e.g. a passport
    number after 'Passport No:'). A bare mention of the type with no value nearby
    does NOT count -- that's what was over-flagging blank forms and notices."""
    if label_rx is None or value_rx is None:
        return 0
    hits = set()
    for m in label_rx.finditer(text):
        seg = text[m.end(): m.end() + window]
        for vm in value_rx.finditer(seg):
            v = vm.group()
            if sum(ch.isdigit() for ch in v) >= min_digits:
                hits.add(v.strip().lower())
                break  # first qualifying value per label occurrence
    return len(hits)


@dataclass
class EntityDef:
    key: str
    label: str
    category: str
    method: str
    regexes: tuple = ()
    per_person: bool = False
    luhn: bool = False
    casefold: bool = False
    label_rx: object = None
    value_rx: object = None
    window: int = 35
    min_digits: int = 0
    # weak = a topic MENTION, not an actual personal value (keyword hits, and a bare
    # name). Per the review protocol a mere mention ("we accept credit cards") and a
    # name on its own are NOT responsive, so a file whose only signals are weak does
    # not flag in the rules fallback -- it still goes to the LLM, which can confirm.
    weak: bool = False


@dataclass
class CompiledRules:
    entities: tuple
    use_ner: bool = False

    @classmethod
    def from_pack(cls, pack: dict, use_ner: bool = False) -> "CompiledRules":
        ents = []
        for e in pack.get("entities", []):
            ents.append(EntityDef(
                key=e["key"], label=e.get("label", e["key"]), category=e.get("category", ""),
                method=e.get("method", "regex"),
                regexes=tuple(re.compile(p, re.I) if e.get("method") == "keyword"
                              else re.compile(p) for p in e.get("patterns", [])),
                per_person=e.get("per_person", False), luhn=e.get("luhn", False),
                casefold=e.get("casefold", False),
                label_rx=re.compile(e["label_pattern"], re.I) if e.get("label_pattern") else None,
                value_rx=re.compile(e["value_pattern"], re.I) if e.get("value_pattern") else None,
                window=e.get("window", 35), min_digits=e.get("min_digits", 0),
                # keyword methods are mention-only by default; anything else is
                # value-bearing unless a rulepack marks it weak (NAME does).
                weak=e.get("weak", e.get("method", "regex") == "keyword")))
        return cls(entities=tuple(ents), use_ner=use_ner)

    @property
    def per_person_keys(self):
        return tuple(e.key for e in self.entities if e.per_person)

    @property
    def value_keys(self):
        """Keys that carry an actual personal VALUE (not a bare topic mention)."""
        return tuple(e.key for e in self.entities if not e.weak)


def detect(text: str, rules: CompiledRules) -> tuple[dict, list, list]:
    """Return (distinct_counts_by_key, labels_found, categories_found)."""
    counts: dict[str, int] = {}
    labels: list[str] = []
    categories: list[str] = []
    for e in rules.entities:
        if e.method == "regex":
            seen = set()
            for rx in e.regexes:
                if e.luhn:
                    for m in rx.finditer(text):
                        digits = re.sub(r"\D", "", m.group())
                        if card_valid(digits):
                            seen.add(digits)
                else:
                    for v in rx.findall(text):
                        seen.add(v.lower() if e.casefold else v)
            n = len(seen)
        elif e.method == "keyword":
            n = 1 if any(rx.search(text) for rx in e.regexes) else 0
        elif e.method == "name":
            n = len(detect_names(text, rules.use_ner))
        elif e.method == "address":
            n = detect_addresses(text)
        elif e.method == "money":
            n = detect_money(text)
        elif e.method == "ssn":
            n = detect_ssn(text)
        elif e.method == "labeled_value":
            n = detect_labeled_value(text, e.label_rx, e.value_rx, e.window, e.min_digits)
        else:
            n = 0
        counts[e.key] = n
        if n:
            labels.append(e.label)
            if e.category and e.category not in categories:
                categories.append(e.category)
    return counts, labels, categories


def value_signal(counts: dict, rules: CompiledRules) -> bool:
    """True when the rules found ACTUAL personal data, not just a topic mention.

    This is the recall-safe responsiveness floor used when the LLM is off or its
    call fails -- it never undercalls a genuinely responsive file, but stops the
    two big over-call classes that a bare ``any label`` test let through:

      * a value-bearing identifier (email, phone, address, SSN, card, or a labeled
        gov-id / financial / DOB value) is responsive on its own; and
      * a NAME together with ANY other signal is responsive (protocol: a name WITH
        a PI type), e.g. "Patient: Jane Doe -- diagnosis ...".

    A name ON ITS OWN, or a lone topic mention ("we accept credit cards", a blank
    form that just lists "SSN"/"passport"), is NOT responsive and does not flag.
    """
    if any(counts.get(k, 0) > 0 for k in rules.value_keys):
        return True
    if counts.get("NAME", 0) > 0:
        return any(c > 0 for k, c in counts.items() if k != "NAME")
    return False


def row_has_identifier(text: str, rules: CompiledRules) -> bool:
    for e in rules.entities:
        if e.method == "ssn" and e.per_person:
            if _SSN_SEP.search(text):   # formatted SSN in a row = identifier
                return True
            continue
        if not e.per_person or e.method != "regex":
            continue
        for rx in e.regexes:
            if e.luhn:
                for m in rx.finditer(text):
                    if card_valid(re.sub(r"\D", "", m.group())):
                        return True
            elif rx.search(text):
                return True
    return False