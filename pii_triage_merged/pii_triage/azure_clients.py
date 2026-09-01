"""Optional Azure components: Document Intelligence (OCR) and an LLM classifier.

Both are OFF unless enabled (cfg.use_ocr / cfg.use_llm) AND configured. Endpoints
come from the environment or Key Vault and credentials from DefaultAzureCredential
-- nothing is hardcoded. SDKs are imported lazily, so the package runs without
them installed; if a client can't be built, the factory returns None and the
tool simply skips that enrichment.

Retry, rate-limit handling, and context-window management are delegated to
scaling_lib.ai (AzureOpenAIClient, DocumentIntelligenceClient). Key Vault
integration supplements plain env vars when AZURE_KEY_VAULT_URL is set.
"""
from __future__ import annotations

import json
import logging
import os

_log = logging.getLogger(__name__)
_prompt_log = logging.getLogger(__name__ + ".prompts")


def _log_prompt(call_name: str, system: str, user: str, cfg) -> None:
    """Log the system prompt and user message size at DEBUG level.

    The user text is NEVER logged (it is file content and may contain PII).
    Enable via cfg.log_prompts=True (CLI: --log-prompts) or LOG_LLM_PROMPTS=true
    env var (for worker fleets that don't go through the CLI).
    """
    if not (getattr(cfg, "log_prompts", False)
            or os.environ.get("LOG_LLM_PROMPTS", "").lower() in ("1", "true", "yes")):
        return
    _prompt_log.info(
        "[llm:%s] user_chars=%d system_chars=%d\n%s",
        call_name, len(user), len(system), system,
    )

# ---- env / Key Vault helpers ------------------------------------------------

_KV_CACHE: dict = {}


def _kv_secret(name: str) -> str | None:
    """Read a secret from Key Vault if AZURE_KEY_VAULT_URL is set; else None.
    Cached per process. Never raises -- returns None on any failure."""
    url = os.environ.get("AZURE_KEY_VAULT_URL")
    if not url:
        return None
    if name in _KV_CACHE:
        return _KV_CACHE[name]
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        client = SecretClient(vault_url=url, credential=DefaultAzureCredential())
        val = client.get_secret(name).value
    except Exception as exc:
        _log.warning("Key Vault secret '%s' from %s could not be read: %s", name, url, exc, exc_info=True)
        val = None
    _KV_CACHE[name] = val
    return val


def _cfg_value(env_name: str, kv_name: str) -> str | None:
    return os.environ.get(env_name) or _kv_secret(kv_name)


# ---- OCR (Azure Document Intelligence) --------------------------------------

_OCR_CLIENT = None


def _ocr_client():
    global _OCR_CLIENT
    if _OCR_CLIENT is not None:
        return _OCR_CLIENT
    endpoint = (_cfg_value("AZURE_DOCUMENTINTELLIGENCE_ENDPOINT",
                           "AZURE-DOCUMENTINTELLIGENCE-ENDPOINT")
                or os.environ.get("AZURE_DI_ENDPOINT"))
    if not endpoint:
        return None
    from scaling_lib.ai import DocumentIntelligenceClient
    _OCR_CLIENT = DocumentIntelligenceClient(endpoint=endpoint)
    return _OCR_CLIENT


def ocr_file(path: str, cfg):
    """OCR a file with Document Intelligence (prebuilt-layout, so tables come back
    as tables). Returns (text, meta) shaped like an extractor's output, with table
    row counts so a scanned form is tagged programmatic and row-counted.

    Retry and rate-limit handling are managed by scaling_lib.ai.DocumentIntelligenceClient.
    The library automatically records a 'di' checkpoint for each call when running
    inside a scaling-lib worker task.
    """
    client = _ocr_client()
    if client is None:
        raise RuntimeError("Document Intelligence endpoint not configured")
    model = getattr(cfg, "doc_intel_model", "prebuilt-layout")
    ocr_max_pages = int(getattr(cfg, "ocr_max_pages", 0) or 0)
    # Page-cap via DI's native page selector (e.g. "1-15") -- bounds the job on huge scans.
    pages_arg = f"1-{ocr_max_pages}" if (ocr_max_pages and path.lower().endswith(".pdf")) else None

    kwargs = {"pages": pages_arg} if pages_arg else {}
    result = client.analyze(path, model_id=model, **kwargs)

    text = (getattr(result, "content", None) or "")[: cfg.max_scan_chars]
    tables = list(getattr(result, "tables", None) or [])
    pages = list(getattr(result, "pages", None) or [])
    table_rows = sum(int(getattr(t, "row_count", 0) or 0) for t in tables)
    meta = {
        "text_extractable": "text",
        "ocr": True,
        "is_structured": bool(tables),
        "page_or_sheet_count": len(pages),
        # treat each table row beyond the header as a potential entity row
        "structured_entity_rows": max(0, table_rows - len(tables)) if tables else 0,
    }
    return text, meta


def get_ocr_fn(cfg):
    """Return an ocr_fn(path, cfg) if OCR is enabled and importable, else None."""
    if not getattr(cfg, "use_ocr", False):
        return None
    try:
        import scaling_lib.ai  # noqa: F401
    except Exception:
        return None
    return ocr_file


# ---- LLM classifier (Azure OpenAI) ------------------------------------------

_LLM_CLIENT = None

_SYSTEM_PROMPT = (
    "You are a privacy-review assistant applying a breach-notification document-review protocol to decide whether a SINGLE document is RESPONSIVE: it must actually CONTAIN notifiable personal information (PI) belonging to an identifiable (or unknown) individual data subject.\n"
    "\n"
    "This decision drives legal breach-notification obligations. A responsive document that is wrongly cleared is a MISSED NOTIFICATION -- the costliest error here. So when, after reading the document, you are genuinely unsure whether a real individual's PI is present, answer responsive=true.\n"
    "\n"
    "PROCEDURE (in order):\n"
    "1. Read the ENTIRE text provided. Any keyword/search hits you were given are a NON-EXHAUSTIVE starting point, not the full picture: do not limit yourself to them, and never treat \"only a name was hinted\" as proof that nothing else is present. Scan the whole text for every PI category below.\n"
    "2. Find the ACTUAL personal VALUES present -- a real number/date/address/credential tied to a person -- not blank field labels, column headers, placeholders, or the mere name of a data type (\"SSN:\", \"Date of Birth\", \"Account No.\" with nothing after it). A mention with no value is not a value.\n"
    "3. Decide whom the data is about and which jurisdiction the subject is in:\n"
    "   - If you CANNOT tell whether the data subject is US or non-US, treat them as NON-US.\n"
    "   - NON-US (and the unknown-jurisdiction default): RESPONSIVE if at least one real PI value of an individual is present. A name is NOT required.\n"
    "   - US: RESPONSIVE only if a named individual appears WITH at least one other PI value, OR an email/username appears WITH a password or security answer. With no name, US data is responsive only if an SSN, Tax ID, or other government ID is present.\n"
    "4. If multiple subjects appear, the document is responsive if ANY one of them meets the bar.\n"
    "\n"
    "WHAT COUNTS AS A REAL PI VALUE (categories A-K; apply the thresholds):\n"
    "A. Government ID -- SSN, passport, driver's license, TIN, national ID, alien-registration, military ID, tribal ID, any other government ID, or an image/copy of one. Counts only as: an image/copy, a full unredacted number, or a partial number with 5+ digits. Fewer than 5 digits, redacted -> does not count on its own.\n"
    "B. Birth info -- a FULL date of birth (month, day AND year) or a birth certificate. A partial DOB does not count.\n"
    "C. Contact info -- a person's home/mobile phone, personal email, or home address.\n"
    "D. Financial -- a financial account number (full card/bank/loan number), account balance, card expiry, card security code, financial login credentials (username/email WITH password or security answer), security Q&A, or PIN.\n"
    "E. Access credentials (non-financial) -- a username/email WITH a password or security answer; a security answer; a PIN.\n"
    "F. Health -- medical history, condition, diagnosis/treatment, or health-insurance info (policy/subscriber/claims).\n"
    "G. Biometric -- fingerprint, voiceprint, genetic/DNA, retina/iris.\n"
    "H. Family -- mother's maiden name, marriage certificate.\n"
    "I. Demographic -- race/ethnicity, sexual orientation, religion, criminal offenses, trade-union membership.\n"
    "J. Student -- student ID number.\n"
    "K. Work-related -- an individual's employee ID, salary/compensation, performance evaluation, disciplinary record, worker's-comp claim, or employment-application info.\n"
    "\n"
    "CREDENTIAL RULE (strict): a username/email is a credential ONLY when accompanied by a password or security answer. An email address alone is not. A temporary/one-time login code (OTP) is NOT a password.\n"
    "\n"
    "MONEY -- BUSINESS vs INDIVIDUAL (read carefully; a dollar amount alone decides nothing):\n"
    "- A person's name next to a dollar amount is NOT automatically responsive. Decide whose money it is.\n"
    "- BUSINESS / TRANSACTION money is NOT responsive: a name next to an amount in an invoice, receipt, purchase order, quote/estimate, account or billing statement, price list, expense report, shipping/order confirmation, or vendor/B2B correspondence is the business's financial data, not the individual's notifiable PI. Company/order/invoice/transaction numbers and corporate bank/account numbers are likewise not responsive.\n"
    "- INDIVIDUAL financial PI IS responsive: a person's OWN salary, wages, bonus, benefits, payroll, compensation, or their personal bank/card account number or balance (categories D and K). Payroll registers, benefit statements, and compensation records are responsive.\n"
    "- OVERRIDE -- other PI beats business framing: the business/transaction exclusion only clears a document whose ONLY notifiable signal is the dollar amount. If the document also contains an individual's own real category A-K value -- a payment card number, personal bank/financial account or balance, health information, full date of birth, government ID, credentials, biometric, etc. -- it is RESPONSIVE even on an invoice, order, receipt, or statement. (Corporate/business account numbers are not an individual's value and do not trigger this.)\n"
    "- The test is whether the amount is that individual's own pay/account/benefit versus a charge, order, or invoice involving them as a customer or counterparty.\n"
    "\n"
    "DO NOT CLEAR THESE (each of these was wrongly cleared before):\n"
    "- A document with no visible name -- recheck jurisdiction first; under the unknown->non-US default a single PI value is responsive without a name.\n"
    "- A document that reads like ordinary internal/HR/business correspondence -- an individual's Work-Related info (K) is itself notifiable; do not dismiss employee IDs, salaries, evaluations, or discipline as \"business data.\"\n"
    "- A structured/tabular file (spreadsheet/CSV) -- evaluate its contents; rosters of individuals carrying contact, financial, government-ID, or work data are responsive and may hold many subjects. Read it as a MULTI-SUBJECT ROSTER when rows repeat a per-person pattern down the sheet (e.g. a name, DOB, ID, address, email, phone, or employee/member/patient/customer field recurring row after row): each such row is a distinct data subject even if only a few carry a full set of values or the header is unusual/missing. When it is such a roster, set person_count to the number of individual rows (not the number of columns or the number of rows you sampled), so a large roster is not under-counted. A tabular file whose rows are transactions, inventory, ledger lines, system/event logs, or other non-person records is NOT a roster -- clear it under the business/transaction rules above.\n"
    "- A NAME together with a contact element (home address, personal email, or personal phone) that belongs to a data subject -- presumptively RESPONSIVE. (Exception below for a bare sender signature.)\n"
    "\n"
    "STILL NOT RESPONSIVE (answer false -- these protect against over-flagging):\n"
    "- A mere MENTION of a PI type with no real value: blank forms/templates, privacy notices, policies, field labels, data dictionaries, or lookup tables that merely list \"SSN\", \"passport\", \"account number\", etc.\n"
    "- A SIGNATURE / LETTERHEAD block only: if the only contact info is the sender's own sign-off (their name with a work email, office phone, and/or business address) and there is NO other PI anywhere in the document, it is a business signature -- not responsive. But if contact details are presented ABOUT a person in the body (a third party's home address, personal cell, or personal email being shared, listed, or collected), that IS responsive.\n"
    "- Business/organizational data and business/transaction money as defined in the MONEY section above.\n"
    "- Internal operational logs or system records not tied to a specific individual's real PI value.\n"
    "- Generic references like \"we accept credit cards\" or \"bring your passport.\"\n"
    "- A person's NAME with no other PI value -- a name alone is not responsive.\n"
    "\n"
    "TIE-BREAK: the over-flag cases above are real, but do NOT let them pull you into clearing a document that does contain an individual's real PI value. Resolve genuine uncertainty about whether real individual PI is present in favor of responsive=true. If the provided text is empty or unreadable so that you cannot assess it, answer responsive=true so a human reviews it.\n"
    "\n"
    "person_count = the number of DISTINCT individuals whose real PI appears (your best estimate; for a large roster, estimate the number of subject rows).\n"
    "\n"
    "Return ONLY a JSON object: {\"responsive\": true/false, \"names\": [\"...\"], \"person_count\": <int>, \"reasoning\": \"<one sentence naming the specific PI value(s) and the jurisdiction/money rule applied, or why none>\"}. No prose outside the JSON."
)


def _llm_client(cfg):
    global _LLM_CLIENT
    if _LLM_CLIENT is not None:
        return _LLM_CLIENT
    endpoint = _cfg_value("AZURE_OPENAI_ENDPOINT", "AZURE-OPENAI-ENDPOINT")
    if not endpoint:
        return None
    from scaling_lib.ai import AzureOpenAIClient
    deployment = (getattr(cfg, "llm_deployment", "")
                  or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
                  or os.environ.get("AZURE_OPENAI_DEPLOYMENT_GPT_5_4_NANO")
                  or "gpt-4.5-nano")
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    _LLM_CLIENT = AzureOpenAIClient(
        endpoint=endpoint,
        deployment=deployment,
        api_version=api_version,
    )
    return _LLM_CLIENT


def _sample_for_llm(text: str, limit: int) -> str:
    """Bound the text handed to the LLM so the request never exceeds the model's
    input limit (the scan-wide max_scan_chars is millions of chars -- far too big for
    a prompt, which made every large file's call bounce and silently fall back to a
    rules flag). If the file is larger than the limit, sample across it -- head (column
    headers / first rows), a middle slice, and the tail -- so spreadsheets and long
    tables are represented throughout, not just at the top."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    rest = limit - head
    mid_len = rest // 2
    tail_len = rest - mid_len
    mid_start = max(head, (len(text) - mid_len) // 2)
    return (
        text[:head]
        + "\n...[sampled from middle of file]...\n"
        + text[mid_start:mid_start + mid_len]
        + "\n...[sampled from end of file]...\n"
        + text[len(text) - tail_len:]
    )


def llm_classify(text: str, cfg):
    """Ask the LLM to make the responsiveness call on an ambiguous file.

    Rate limiting, retries, and context-window management are handled by
    scaling_lib.ai.AzureOpenAIClient. The library automatically records an 'aoai'
    checkpoint with tokens_in/tokens_out when running inside a scaling-lib task;
    the token delta is also surfaced in the return dict for the CLI runner's tally.
    """
    client = _llm_client(cfg)
    if client is None:
        raise RuntimeError("Azure OpenAI endpoint not configured")
    system = _SYSTEM_PROMPT
    protocol = getattr(cfg, "protocol_text", "") or ""
    if protocol:
        system += "\n\nMATTER PROTOCOL (apply this):\n" + protocol[:8000]
    user = _sample_for_llm(text or "", int(getattr(cfg, "llm_input_chars", 24_000) or 24_000))

    _log_prompt("classify", system, user, cfg)
    tokens_before = client.tokens_in + client.tokens_out
    try:
        content = client.complete(
            message=user,
            system_prompt=system,
            max_output_tokens=16_000,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        _log.error(
            "Azure OpenAI call failed (llm_classify) — endpoint=%s deployment=%s: %s",
            _cfg_value("AZURE_OPENAI_ENDPOINT", "AZURE-OPENAI-ENDPOINT"),
            getattr(client, "deployment", "?"),
            exc,
            exc_info=True,
        )
        raise
    tokens = (client.tokens_in + client.tokens_out) - tokens_before

    data = json.loads(content)
    try:
        person_count = int(data.get("person_count"))
    except (TypeError, ValueError):
        person_count = 0
    return {
        "responsive": bool(data.get("responsive")),
        "names": list(data.get("names") or []),
        "person_count": person_count,
        "reasoning": str(data.get("reasoning") or ""),
        "tokens": tokens,
    }


def llm_classify_stub(text: str, cfg):
    """Timing stub: sleeps ~3 s to approximate a real Azure OpenAI call, then returns
    a neutral non-responsive verdict. No Azure subscription needed.
    Swap in get_llm_fn below to use it.
    """
    import time
    time.sleep(3)
    return {"responsive": False, "names": [], "person_count": 0,
            "reasoning": "stub", "tokens": 0}


# --- Separate BDE entity-COUNT call --------------------------------------------------
# This is intentionally NOT the responsiveness prompt. It makes no responsive/non-responsive
# judgment at all -- it only counts distinct individuals in text the row-parser failed to
# structure (e.g. payroll registers whose grouped multi-row layout defeats the grid reader).
# It exists so a readable roster that extracted as "0 rows" is not silently under-counted.
_BDE_COUNT_PROMPT = (
    "You count how many distinct DATA SUBJECTS appear in a document, for a breach-notification "
    "review governed by the Cognicion CIR PI Review Protocol. Output is a COUNT ONLY -- you make "
    "no responsive/non-responsive judgment. A 'Data Subject' is one individual person who would "
    "get a profile created under the rules below. Count each individual ONCE no matter how many "
    "fields, rows, pages, or mentions they span (name + SSN + address + phone + salary = 1 "
    "person). This mirrors the reviewers' 'entity' count -- a document with >50 is a '51+ "
    "Entities' file.\n"
    "\n"
    "WHEN AN INDIVIDUAL COUNTS AS A DATA SUBJECT (jurisdiction matters):\n"
    "- NON-US data subject (or jurisdiction UNCLEAR -- default to this): counts if the document "
    "has AT LEAST ONE PII TYPE below for them, EVEN IF NO NAME is present (name captured as "
    "'[Unknown]'). Infer non-US from foreign addresses, foreign ID types, or issuing countries; "
    "if you cannot tell whether the person is US or non-US, treat as non-US.\n"
    "- US data subject: counts ONLY if (1) a NAME is present together with at least one PII TYPE "
    "below, OR (2) an email address together with a password or security question/answer. A US "
    "person with NO name counts ONLY if an SSN, Tax ID, or other Government-issued ID is present.\n"
    "- A bare NAME with no other PII attached is NOT a data subject (does not count).\n"
    "\n"
    "PII TYPES (any ONE qualifies an individual):\n"
    "A. Government-Issued ID -- SSN, passport #, driver's license #, Taxpayer ID (TIN), National "
    "ID / National Insurance #, Alien Registration #, Military ID, Tribal ID, other gov ID, or a "
    "photocopy/image of a gov ID. Include a FULL number, a partial with 5+ digits, or an ID image.\n"
    "B. Birth -- full Date of Birth (month, day, AND year), or a birth certificate copy.\n"
    "C. Contact -- personal phone (home or mobile), personal email, or home/residential address.\n"
    "D. Financial Account -- transactional info, financial account number (full credit/debit card, "
    "full bank account, loan #, other), bank balance, card expiration date, card security code, "
    "financial access credentials (username/email WITH password or security answer -- NOT one-time "
    "codes), security Q&A, or PIN.\n"
    "E. Access Credentials (non-financial) -- username/email WITH password or security answer, a "
    "security answer, or a PIN.\n"
    "F. Health -- medical history, mental/physical condition, treatment/diagnosis, or health "
    "insurance info (policy #, subscriber ID, claims, appeals, etc.).\n"
    "G. Biometric -- fingerprint, voice print, genetic/DNA, retina/iris.\n"
    "H. Family -- mother's maiden name, or marriage certificate copy.\n"
    "I. Demographic -- race/ethnicity, sexual orientation, religion/philosophical belief, criminal "
    "convictions/offenses, or trade union membership.\n"
    "J. Student -- student identification number.\n"
    "K. Work-Related -- employee ID number, salary/compensation, work evaluation, disciplinary "
    "record, workers' compensation claim, or employment application info.\n"
    "\n"
    "\n"
    "ALSO CLASSIFY (critical): set \"is_roster\": true if this document is a PER-PERSON "
    "roster/register where each row or block is a DIFFERENT individual (payroll or HR register, "
    "employee/member/customer list, class roster, etc.). Set \"is_roster\": false if it is about "
    "ONE or a FEW individuals with repeated data (a statement, form, letter, contract). You are "
    "often shown only a PARTIAL SAMPLE of a large file -- if is_roster is true, the TRUE number of "
    "data subjects is roughly the file's total row/record count (given below as CONTEXT), NOT just "
    "the people visible in the sample. Prefer OVER-counting to UNDER-counting: when unsure whether "
    "someone qualifies, or how many people a large roster holds, err HIGH (recall matters more "
    "than precision here).\n"
    "\n"
    "DO NOT COUNT: organizations/companies, column headers, totals/subtotals, or non-person line "
    "items (transactions, inventory, ledger entries, account numbers not tied to an individual). "
    "For a large roster/register, ESTIMATE the number of distinct data subjects (per-person "
    "blocks/rows) -- do not only count the few you read in full, and never report more than are "
    "actually present. If no individual meets the rules above, return 0.\n"
    "\n"
    "Return ONLY JSON: {\"person_count\": <int>, \"is_roster\": <true|false>, \"reasoning\": \"<one sentence: who the data "
    "subjects are, which PII type(s) qualified them, and how you counted>\"}. No prose outside JSON."
)


def llm_count_entities(text: str, cfg):
    """Ask the LLM ONLY to count distinct individuals in unparseable structured text.
    Returns {"person_count": int, "is_roster": bool, "reasoning": str, "tokens": int}.
    Makes NO responsiveness judgment -- the caller uses this solely to raise the BDE
    entity estimate, never to clear."""
    client = _llm_client(cfg)
    if client is None:
        raise RuntimeError("Azure OpenAI endpoint not configured")
    system = _BDE_COUNT_PROMPT
    protocol = getattr(cfg, "protocol_text", "") or ""
    if protocol:
        system += "\n\nMATTER PROTOCOL (use its PII definitions):\n" + protocol[:8000]
    user = _sample_for_llm(text or "", int(getattr(cfg, "llm_input_chars", 24_000) or 24_000))

    _log_prompt("count", system, user, cfg)
    tokens_before = client.tokens_in + client.tokens_out
    try:
        content = client.complete(
            message=user,
            system_prompt=system,
            max_output_tokens=16_000,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        _log.error(
            "Azure OpenAI call failed (llm_count_entities) — endpoint=%s deployment=%s: %s",
            _cfg_value("AZURE_OPENAI_ENDPOINT", "AZURE-OPENAI-ENDPOINT"),
            getattr(client, "deployment", "?"),
            exc,
            exc_info=True,
        )
        raise
    tokens = (client.tokens_in + client.tokens_out) - tokens_before

    data = json.loads(content)
    try:
        person_count = int(data.get("person_count"))
    except (TypeError, ValueError):
        person_count = 0
    return {"person_count": max(0, person_count),
            "is_roster": bool(data.get("is_roster")),
            "reasoning": str(data.get("reasoning") or ""), "tokens": tokens}


def llm_count_entities_stub(text: str, cfg):
    """Timing stub for the counter call. Returns 0 so it never changes behavior offline."""
    import time
    time.sleep(1)
    return {"person_count": 0, "is_roster": False, "reasoning": "stub", "tokens": 0}


def get_bde_count_fn(cfg):
    """Return a bde_count_fn(text, cfg) if the LLM is enabled and importable, else None.
    Separate from get_llm_fn so the BDE counter can be toggled/budgeted independently."""
    if not getattr(cfg, "use_llm", False):
        return None
    if not getattr(cfg, "use_bde_count_llm", True):
        return None
    try:
        import scaling_lib.ai  # noqa: F401
    except Exception:
        return None
    return llm_count_entities
    # return llm_count_entities_stub  # timing stub — no Azure needed


def get_llm_fn(cfg):
    """Return an llm_fn(text, cfg) if the LLM is enabled and importable, else None."""
    if not getattr(cfg, "use_llm", False):
        return None
    try:
        import scaling_lib.ai  # noqa: F401
    except Exception:
        return None
    return llm_classify           # real Azure OpenAI call
    # return llm_classify_stub    # timing stub — no Azure needed


# ===================================================================================== #
# STAGE 2 -- graded responsiveness (the "dataset overview" call)
#
# NEW CODE, deliberately additive. Nothing here touches _SYSTEM_PROMPT, llm_classify or
# apply_llm -- the frozen stage-1 NR surface that tools/check_nr_frozen.py pins.
#
# Stage 2 asks the same underlying question as stage 1 but returns a 5-level grade rather
# than a boolean, and its TIE-BREAK instruction differs: stage 1 rounds genuine uncertainty
# UP to responsive, stage 2 expresses it as `borderline`. Since routing treats borderline as
# non-responsive, stage 2 CLEARS on uncertainty where stage 1 FLAGS. That is exactly why
# stage 1 owns NR removal and stage 2 only describes what survived it -- never the reverse.
#
# Prompt text is carried over verbatim from Daniel's 2.9.9 build. About 87% of it is
# identical to stage 1's prompt; the divergence is confined to the output contract, the
# TIE-BREAK wording, the protocol-mode swap, and the roster person_count instruction.
# ===================================================================================== #

_S2_PROMPT_HEAD = (
    "You are a privacy-review assistant applying a breach-notification document-review protocol to decide whether a SINGLE document is RESPONSIVE: it must actually CONTAIN notifiable personal information (PI) belonging to an identifiable (or unknown) individual data subject.\n"
    "\n"
    "This decision drives legal breach-notification obligations. A responsive document that is wrongly cleared is a MISSED NOTIFICATION -- the costliest error here. So when, after reading the document, you are genuinely unsure whether a real individual's PI is present, answer responsive=true.\n"
    "\n"
    "PROCEDURE (in order):\n"
    "1. Read the ENTIRE text provided. Any keyword/search hits you were given are a NON-EXHAUSTIVE starting point, not the full picture: do not limit yourself to them, and never treat \"only a name was hinted\" as proof that nothing else is present. Scan the whole text for every PI category below.\n"
    "2. Find the ACTUAL personal VALUES present -- a real number/date/address/credential tied to a person -- not blank field labels, column headers, placeholders, or the mere name of a data type (\"SSN:\", \"Date of Birth\", \"Account No.\" with nothing after it). A mention with no value is not a value.\n"
    "3. Decide whom the data is about and which jurisdiction the subject is in:\n"
    "   - If you CANNOT tell whether the data subject is US or non-US, treat them as NON-US.\n"
    "   - NON-US (and the unknown-jurisdiction default): RESPONSIVE if at least one real PI value of an individual is present. A name is NOT required.\n"
    "   - US: RESPONSIVE only if a named individual appears WITH at least one other PI value, OR an email/username appears WITH a password or security answer. With no name, US data is responsive only if an SSN, Tax ID, or other government ID is present.\n"
    "4. If multiple subjects appear, the document is responsive if ANY one of them meets the bar.\n"
    "\n"
)
_S2_PROMPT_PI_CATEGORIES = (
    "WHAT COUNTS AS A REAL PI VALUE (categories A-K; apply the thresholds):\n"
    "A. Government ID -- SSN, passport, driver's license, TIN, national ID, alien-registration, military ID, tribal ID, any other government ID, or an image/copy of one. Counts only as: an image/copy, a full unredacted number, or a partial number with 5+ digits. Fewer than 5 digits, redacted -> does not count on its own.\n"
    "B. Birth info -- a FULL date of birth (month, day AND year) or a birth certificate. A partial DOB does not count.\n"
    "C. Contact info -- a person's home/mobile phone, personal email, or home address.\n"
    "D. Financial -- a financial account number (full card/bank/loan number), account balance, card expiry, card security code, financial login credentials (username/email WITH password or security answer), security Q&A, or PIN.\n"
    "E. Access credentials (non-financial) -- a username/email WITH a password or security answer; a security answer; a PIN.\n"
    "F. Health -- medical history, condition, diagnosis/treatment, or health-insurance info (policy/subscriber/claims).\n"
    "G. Biometric -- fingerprint, voiceprint, genetic/DNA, retina/iris.\n"
    "H. Family -- mother's maiden name, marriage certificate.\n"
    "I. Demographic -- race/ethnicity, sexual orientation, religion, criminal offenses, trade-union membership.\n"
    "J. Student -- student ID number.\n"
    "K. Work-related -- an individual's employee ID, salary/compensation, performance evaluation, disciplinary record, worker's-comp claim, or employment-application info.\n"
    "\n"
)
_S2_PROMPT_PI_FROM_PROTOCOL = (
    "WHAT COUNTS AS A REAL PI VALUE: use ONLY the categories explicitly listed in the MATTER PROTOCOL at the end of this prompt. Do not add types from your own knowledge.\n"
    "\n"
    "DEEPEST-LEAF RULE: for clear_yes or likely_yes you must match the DEEPEST applicable item in the protocol hierarchy. A subcategory that has its own sub-items listed beneath it is NOT a leaf -- you must match one of those sub-items. For example, if the protocol lists 'Financial account number' with children (Full credit or debit card number / Full bank account number / Loan number), matching 'Financial account number' alone is not enough; you must match a child. If you can only reach a non-leaf parent, use borderline.\n"
    "\n"
    "COMBINATION RULES: some leaf items only count when two elements appear TOGETHER in the document (e.g. email + password, partial government ID with enough digits). Apply every such pairing requirement literally: one element alone does not trigger the type.\n"
    "\n"
)
_S2_PROMPT_TAIL = (
    "CREDENTIAL RULE: apply pairing requirements from the protocol (or A-K) literally -- both elements must be present. Specifically: a username or email is credentials ONLY when accompanied by an actual password or security answer in the same document. A one-time login code (OTP) is NOT a password.\n"
    "\n"
    "MONEY -- BUSINESS vs INDIVIDUAL (read carefully; a dollar amount alone decides nothing):\n"
    "- A person's name next to a dollar amount is NOT automatically responsive. Decide whose money it is.\n"
    "- BUSINESS / TRANSACTION money is NOT responsive: a name next to an amount in an invoice, receipt, purchase order, quote/estimate, account or billing statement, price list, expense report, shipping/order confirmation, or vendor/B2B correspondence is the business's financial data, not the individual's notifiable PI. Company/order/invoice/transaction numbers and corporate bank/account numbers are likewise not responsive.\n"
    "- INDIVIDUAL financial PI IS responsive: a person's OWN salary, wages, bonus, benefits, payroll, compensation, or their personal bank/card account number or balance. Payroll registers, benefit statements, and compensation records are responsive.\n"
    "- OVERRIDE -- other PI beats business framing: the business/transaction exclusion only clears a document whose ONLY notifiable signal is the dollar amount. If the document also contains an individual's own real PI value -- a payment card number, personal bank/financial account or balance, health information, full date of birth, government ID, credentials, biometric, etc. -- it is RESPONSIVE even on an invoice, order, receipt, or statement. (Corporate/business account numbers are not an individual's value and do not trigger this.)\n"
    "- The test is whether the amount is that individual's own pay/account/benefit versus a charge, order, or invoice involving them as a customer or counterparty.\n"
    "\n"
    "DO NOT CLEAR THESE (each of these was wrongly cleared before):\n"
    "- A document with no visible name -- recheck jurisdiction first; under the unknown->non-US default a single PI value is responsive without a name.\n"
    "- A document that reads like ordinary internal/HR/business correspondence -- an individual's work-related info (employee ID, salary, evaluation, discipline) is itself notifiable; do not dismiss it as \"business data.\"\n"
    "- A structured/tabular file (spreadsheet/CSV) -- evaluate its contents; rosters of individuals carrying contact, financial, government-ID, or work data are responsive and may hold many subjects.\n"
    "- A NAME together with a contact element (home address, personal email, or personal phone) that belongs to a data subject -- presumptively RESPONSIVE. (Exception below for a bare sender signature.)\n"
    "\n"
    "STILL NOT RESPONSIVE (answer false -- these protect against over-flagging):\n"
    "- A mere MENTION of a PI type with no real value: blank forms/templates, privacy notices, policies, field labels, data dictionaries, or lookup tables that merely list \"SSN\", \"passport\", \"account number\", etc.\n"
    "- A SIGNATURE / LETTERHEAD block only: if the only contact info is the sender's own sign-off (their name with a work email, office phone, and/or business address) and there is NO other PI anywhere in the document, it is a business signature -- not responsive. But if contact details are presented ABOUT a person in the body (a third party's home address, personal cell, or personal email being shared, listed, or collected), that IS responsive.\n"
    "- Business/organizational data and business/transaction money as defined in the MONEY section above.\n"
    "- Internal operational logs or system records not tied to a specific individual's real PI value.\n"
    "- Generic references like \"we accept credit cards\" or \"bring your passport.\"\n"
    "- A person's NAME with no other PI value -- a name alone is not responsive.\n"
    "\n"
    "TIE-BREAK: the over-flag cases above are real, but do NOT let them pull you into clearing a document that does contain an individual's real PI value. Express genuine uncertainty in the responsiveness level rather than rounding up to clear_yes -- use borderline when the protocol is genuinely silent, and likely_no when the data is more plausibly business/operational. If the provided text is empty or unreadable, choose borderline so a human reviews it.\n"
    "\n"
    "person_count = the number of DISTINCT individuals whose real PI appears (your best estimate; for a large roster, estimate the number of subject rows).\n"
    "\n"
    "RESPONSIVENESS LEVEL -- choose exactly one:\n"
    "  clear_yes  -- A specific leaf item from the protocol (or A-K) is demonstrably present: an actual value exists in the document, all pairing requirements are met, no reasonable doubt.\n"
    "  likely_yes -- You can name a specific leaf item (not a section heading or a subcategory with its own children) from the protocol (or A-K), an actual value of that type is present in the document (not just context suggesting one might exist), all pairing requirements are met, and the match requires interpretive judgment. If the leaf item cannot be named, the value is absent, or a pairing requirement is unmet, use borderline.\n"
    "  borderline -- Contains something PI-adjacent but you cannot match it to a specific listed leaf subcategory -- either the protocol is silent on this type, the content fits a group heading but not any item enumerated under it, or there is genuine ambiguity about whether it is the individual's own PI or business/operational data.\n"
    "  likely_no  -- Has PI-adjacent content but under a careful reading of the protocol probably doesn't qualify -- the data is more plausibly about business activity or operations than about the individual as a data subject.\n"
    "  clear_no   -- Nothing meets or approaches the protocol's definition.\n"
    "\n"
    "Return ONLY a JSON object: {\"responsiveness\": \"<level>\", \"names\": [\"...\"], \"person_count\": <int>, \"reasoning\": \"<one sentence; for clear_yes or likely_yes name the exact leaf subcategory from the protocol (or A-K item), confirm all combination requirements for that item are satisfied, and state the value found; for borderline explain which group heading seemed relevant and why no listed leaf item matched (or which combination requirement was unmet); for other levels explain why the document falls short>\"}. No prose outside the JSON."
)


_S2_VALID_LEVELS = ("clear_yes", "likely_yes", "borderline", "likely_no", "clear_no")


def llm_classify_graded(text: str, cfg):
    """Stage-2 responsiveness call. Returns
    {"responsiveness": <level>, "names": [...], "person_count": int, "tokens": int}.

    NOTE: `reasoning` IS requested from the model (asking for it measurably improves
    grading quality) but is DELIBERATELY NOT RETURNED. The prompt instructs the model
    to state the value it found; dropping it here means it cannot reach the inventory
    even by accident.
    """
    client = _llm_client(cfg)
    if client is None:
        raise RuntimeError("Azure OpenAI endpoint not configured")
    protocol = getattr(cfg, "protocol_text", "") or ""
    pi_section = _S2_PROMPT_PI_FROM_PROTOCOL if protocol else _S2_PROMPT_PI_CATEGORIES
    system = _S2_PROMPT_HEAD + pi_section + _S2_PROMPT_TAIL
    jurisdiction = (getattr(cfg, "jurisdiction", "") or "").strip().lower()
    if jurisdiction == "us":
        system += ("\n\nJURISDICTION: All data subjects in this matter are US individuals. "
                   "Apply the US standard throughout; when you cannot determine jurisdiction "
                   "from the document, default to US (not non-US).")
    elif jurisdiction == "non-us":
        system += ("\n\nJURISDICTION: All data subjects in this matter are non-US individuals. "
                   "Apply the non-US standard throughout; when you cannot determine "
                   "jurisdiction from the document, default to non-US.")
    if protocol:
        system += "\n\nMATTER PROTOCOL (apply this):\n" + protocol[:8000]
    user = _sample_for_llm(text or "", int(getattr(cfg, "llm_input_chars", 24_000) or 24_000))

    _log_prompt("graded", system, user, cfg)
    tokens_before = client.tokens_in + client.tokens_out
    try:
        content = client.complete(
            message=user,
            system_prompt=system,
            max_output_tokens=16_000,
            temperature=0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        _log.error(
            "Azure OpenAI call failed (llm_classify_graded) — endpoint=%s deployment=%s: %s",
            _cfg_value("AZURE_OPENAI_ENDPOINT", "AZURE-OPENAI-ENDPOINT"),
            getattr(client, "deployment", "?"),
            exc,
            exc_info=True,
        )
        raise
    tokens = (client.tokens_in + client.tokens_out) - tokens_before

    data = json.loads(content)
    try:
        person_count = int(data.get("person_count"))
    except (TypeError, ValueError):
        person_count = 0
    out = {
        "responsiveness": str(data.get("responsiveness") or ""),
        "names": list(data.get("names") or []),
        "person_count": person_count,
        "tokens": tokens,
    }
    # Older deployments may still answer with the boolean shape. Pass it through so the
    # runner can fall back rather than treating the file as ungraded.
    if not out["responsiveness"] and "responsive" in data:
        out["responsive"] = bool(data.get("responsive"))
    return out


def llm_classify_graded_stub(text: str, cfg):
    """Offline stub: grades on a crude keyword heuristic. For smoke-testing the pipeline
    with no Azure subscription. Never use for real triage."""
    t = (text or "").lower()
    if "ssn" in t or "social security" in t:
        level = "clear_yes"
    elif any(k in t for k in ("dob", "date of birth", "passport", "account number")):
        level = "likely_yes"
    elif any(k in t for k in ("invoice", "purchase order", "inventory")):
        level = "likely_no"
    elif t.strip():
        level = "borderline"
    else:
        level = "clear_no"
    return {"responsiveness": level, "names": [], "person_count": 0, "tokens": 0}


def get_stage2_fn(cfg):
    """Return a stage2_fn(text, cfg) if stage 2 AND the LLM are enabled and importable,
    else None. When None, the runner falls back to the recall-first rules path rather
    than leaving stage 2 blank."""
    if not getattr(cfg, "use_stage2", True):
        return None
    if not getattr(cfg, "use_llm", False):
        return None
    try:
        import scaling_lib.ai  # noqa: F401
    except Exception:
        return None
    return llm_classify_graded
