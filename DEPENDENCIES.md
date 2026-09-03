# Dependency, License & Service Register

Every external dependency, library, license, and paid/metered service tied to **pii_triage** —
plus the account-ownership and renewal register.

> **Scope.** Dependencies, versions, licenses, and which services are wired in are **determined
> from the code** and maintained here. Org-specific fields (account owners, subscription IDs,
> secret/token expiry, renewal dates) are **not in the repository** — those cells are left blank
> for an owner to complete.
>
> **Licenses** below are the well-known upstream licenses for each package; treat this as a working
> register, not a legal SBOM. Regenerate the authoritative list from the actually-installed
> versions with:
> ```bash
> pip install pip-licenses && pip-licenses --format=markdown --with-urls --with-license-file
> ```
> Last verified from installed metadata where available (openpyxl=MIT, python-dotenv=BSD-3-Clause
> confirmed locally); the rest are upstream-declared licenses to confirm at pin time.

---

## 1. Python libraries

Source of truth: `pii_triage_merged/requirements.txt`, `requirements-local.txt`. The core package
runs on the **standard library alone**; every library below widens format coverage or enables an
opt-in feature, and missing ones degrade gracefully.

### 1.1 Declared — format parsers & core (`requirements.txt`)

| Package | Constraint | License | Purpose | In worker image? |
|---|---|---|---|---|
| `pypdf` | ≥4.0 | BSD-3-Clause | PDF text + image-only detection | ✅ |
| `python-docx` | ≥1.1 | MIT | `.docx` / `.docm` | ✅ |
| `python-pptx` | ≥0.6 | MIT | `.pptx` / `.pptm` | ✅ |
| `openpyxl` | ≥3.1 | MIT *(confirmed)* | `.xlsx` / `.xlsm` | ✅ |
| `extract-msg` | ≥0.48 | **GPL-3.0-or-later** ⚠️ | `.msg` (Outlook) | ✅ |
| `xlrd` | ≥2.0 | BSD-3-Clause | legacy `.xls` | ✅ |
| `striprtf` | ≥0.0.26 | BSD-3-Clause | `.rtf` | ✅ |
| `PyYAML` | ≥6.0 | MIT | YAML rule packs | ✅ |
| `Pillow` | ≥10.0 | HPND / MIT-CMU | **required** by pypdf for embedded-image OCR | ✅ |
| `pytest` | ≥8.0 | MIT | test runner (dev) | ✅ (in image via requirements) |
| `reportlab` | ≥4.0 | BSD-3-Clause | test-only; builds PDFs for OCR-routing tests | ✅ (in image via requirements) |

### 1.2 System packages — legacy Office conversion (Dockerfile, not pip)

Legacy `.doc`/`.xls`/`.ppt` conversion runs inline on every worker via LibreOffice headless (no
more Windows-only COM automation, no separate Windows VM/queue leg — see `conversion.py` and
`ARCHITECTURE.md` §7).

| Package (apt) | License | Purpose | Where |
|---|---|---|---|
| `libreoffice-writer` / `-calc` / `-impress` | MPL-2.0 | `.doc`/`.xls`/`.ppt` → OOXML conversion | worker image |
| `antiword` | GPL-2.0 | lightweight `.doc` text-extraction fallback (`extractors.x_doc`) | worker image |

### 1.3 Private / internal (`requirements-local.txt`)

| Package | Source | License | Purpose | Account owner |
|---|---|---|---|---|
| `scaling-lib[dev]` | `git+https://github.com/ldmglobal-com/scaling-lib@dev` | **Proprietary / internal** (confirm) | Worker framework: queue polling, retries, DLQ, status table, AI clients, ACR CLI | GitHub org **`ldmglobal-com`** |

Pulled at Docker build time via a `GITHUB_TOKEN` build arg (see §5 renewal register).

### 1.4 Azure enrichment SDKs — optional, `--ocr` / `--llm` only

Listed as commented optionals in `requirements.txt`; installed transitively by `scaling-lib` and/or
imported directly by `azure_clients.py` / `collect_outputs.py`.

| Package | Constraint | License | Purpose |
|---|---|---|---|
| `azure-ai-documentintelligence` | ≥1.0 | MIT | OCR (Document Intelligence `prebuilt-layout`) |
| `openai` | ≥1.40 | Apache-2.0 *(verify)* | Azure OpenAI client (`--llm`) |
| `azure-identity` | ≥1.17 | MIT *(confirmed installed 1.25.x)* | `DefaultAzureCredential` / `AzureCliCredential` |
| `azure-keyvault-secrets` | ≥4.8 | MIT | endpoints from Key Vault (only if `AZURE_KEY_VAULT_URL` set) |

### 1.5 Imported but **not declared** in any requirements file 

These are pulled in indirectly today; flagged so a pin/lockfile change doesn't silently break them.

| Package | License | Imported by | Status |
|---|---|---|---|
| `python-dotenv` | BSD-3-Clause *(confirmed)* | `worker.py`, `enqueue.py`, `collect_outputs.py` | Comes transitively via `scaling-lib`; not pinned here |
| `textual` | MIT | `worker_status.py` (optional live TUI) | Undeclared; the `--once` path degrades without it |
| `spacy` + `en_core_web_sm` | MIT (both) | `detection.py` when `--ner` / `USE_NER` | Optional; install manually (`pip install spacy && python -m spacy download en_core_web_sm`) |

---

## 2. System packages & external tools

| Tool | License | Where / how | Purpose |
|---|---|---|---|
| Python 3.12 (`python:3.12-slim` base image) | PSF License Agreement | Docker base image | Runtime |
| Debian slim userland (base image) | Mixed (mostly permissive + GPL) | Docker base image | OS libs |
| `git` | GPL-2.0 | installed in Docker builder stage | to `pip install` scaling-lib from GitHub |
| `antiword` | GPL-2.0 | apt / PATH (optional) | legacy binary `.doc` → text (Linux) |
| `catdoc` | GPL-2.0 | apt / PATH (optional, alternative to antiword) | legacy binary `.doc` → text |
| Azure CLI (`az`) | MIT | operator / ops VM | login, ACR, Container App queries |
| Docker Engine / CLI | Apache-2.0 | build host (or ACR cloud build) | build the worker image |
| `scaling-lib` CLI (`scaling-lib acr-*`, `status`) | see §1.3 | operator / ops VM | build, deploy, status |

---

## 3. Paid / metered cloud services

All are **Azure**. OCR and LLM are opt-in (`--ocr` / `--llm`); the rules-only pipeline uses none of
the AI services. Pricing is pay-as-you-go / consumption unless a reservation is noted. Owner/billing
details are in §6.

| Service | Wired via | Billing model | Gated by | Notes |
|---|---|---|---|---|
| Azure Storage — **Queues** | `AZURE_STORAGE_QUEUE_URL` / conn string | per operation + storage | always (scaled) | work distribution (main, windows, dead-letter) |
| Azure Storage — **Table** | `AZURE_STORAGE_TABLE_URL` | per operation + storage | always (scaled) | task status, tokens, checkpoints |
| Azure Storage — **File Shares** | `INPUT_MOUNT` / `OUTPUT_MOUNT` | provisioned/used GB | always (scaled) | corpus in, `result.json` out |
| Azure **Container Apps** | `AZURE_CONTAINER_APP` | vCPU-s + GiB-s (scale-to-zero) | always (scaled) | the worker fleet |
| Azure **Container Registry (ACR)** | `ACR_REGISTRY` | **monthly tier** (Basic/Std/Premium) + storage | always (scaled) | worker image registry; fixed monthly component |
| Azure **OpenAI Service** | `AZURE_OPENAI_ENDPOINT` + deployment | per 1K tokens (or PTU if reserved) | `--llm` / `USE_LLM` | responsiveness + BDE count + stage-2 grading |
| Azure **AI Document Intelligence** | `AZURE_DI_ENDPOINT` | per page (`prebuilt-layout`) | `--ocr` / `USE_OCR` | full-file + embedded-image OCR |
| Azure **Key Vault** | `AZURE_KEY_VAULT_URL` | per operation | optional | endpoint/secret storage |
| Azure **Application Insights / Monitor** | `APPLICATIONINSIGHTS_CONNECTION_STRING` | per GB ingested + retention | optional | worker telemetry/logs |
| Azure **support plan** (if any) | account-level | monthly/annual | N/A | note if a paid tier exists |

**Free / no-incremental-charge APIs used by the code** (no account of their own):

| API | Endpoint | Auth | Used by |
|---|---|---|---|
| Azure Retail Prices API | `prices.azure.com` | none (public) | `collect_outputs.py` compute-cost pricing |
| Azure Resource Manager | `management.azure.com` | managed identity / CLI | `collect_outputs.py` Container App/env introspection |

---

## 4. License compliance notes

- **`extract-msg` is GPL-3.0-or-later** and is installed into the worker Docker image. GPL
  obligations attach on *distribution* of the image. Internal-only use (running it in your own
  Container App) does not trigger distribution, but **do not ship the image outside the org**
  without a license review. If that becomes a requirement, isolate `.msg` handling behind a service
  boundary or replace the parser.
- **`antiword` / `catdoc` are GPL-2.0** external binaries invoked as **separate processes** (not
  linked), so they do not affect the license of pii_triage itself. They are optional and not baked
  into the image by default.
- Everything else in §1 is permissive (MIT / BSD / Apache-2.0 / PSF / HPND) and imposes only
  attribution obligations.
- **`scaling-lib` is a private/internal dependency** — confirm its actual license/terms with its
  owner before any external distribution (§1.3).

---

## 5. Accounts, credentials & renewal register

The items with **real expiry/renewal dates**. None are stored in the repo (`.env` is git-ignored);
track them here by an owner.

| Item | What it's for | Owner | Location / identifier | Renewal / expiry |
|---|---|---|---|---|
| Azure service-principal secret (`AZURE_CLIENT_SECRET`) | `DefaultAzureCredential` for non-managed-identity auth |  | Entra app registration → Certificates & secrets | SP secrets expire — often 1–2 yr |
| GitHub PAT (`GITHUB_TOKEN`) | Docker build installs `scaling-lib` from the private repo |  | GitHub → Developer settings → Tokens | PATs expire |
| Azure OpenAI capacity / PTU reservation (if used) | reserved LLM throughput |  | Azure OpenAI resource | term, if reserved; else N/A pay-go |
| Azure reserved instances / savings plan (if used) | discounted compute |  | Azure Cost Management → Reservations | 1 or 3 yr term |
| Azure support plan (if paid) | support SLA |  | Azure account | monthly/annual |
| TLS certificates / custom domains | — | — | — | **N/A** — the operator UI is loopback-only (127.0.0.1), no public endpoint or cert |

---

## 6. Account ownership register

Who owns each account/resource the project depends on. Known resource **names** (from the prod
`.env`) are pre-filled; complete the owner/billing columns.

| Account / resource | Identifier (known) | Owner | Billing owner |
|---|---|---|---|
| Azure subscription | (subscription ID) |  |  |
| Entra (Azure AD) tenant | (tenant ID) |  | — |
| Resource group | `rg-eus2-idm-internal-prod` |  |  |
| Container App | `ca-worker-eus2-idm-internal-prod` |  |  |
| Container Registry | `ACR_REGISTRY` (see `.env`) |  |  |
| Azure OpenAI resource + deployment | `AZURE_OPENAI_DEPLOYMENT` (e.g. `gpt-5.4-nano`) |  |  |
| Document Intelligence resource | `AZURE_DI_ENDPOINT` (see `.env`) |  |  |
| Storage account (queues/table/files) | from `AZURE_STORAGE_*_URL` |  |  |
| Key Vault (if used) | `AZURE_KEY_VAULT_URL` |  |  |
| App Insights (if used) | connection string |  |  |
| GitHub org (`scaling-lib`) | `ldmglobal-com` |  | — |

---

## 7. How to keep this current

1. **On any dependency change** — update §1 and regenerate the license list with `pip-licenses`
   (command at top). Re-check the §1.5 undeclared list.
2. **On any new Azure service** — add a row to §3 and an owner row to §6.
3. **Quarterly** — review §5 for secrets/tokens approaching expiry (SP secret, GitHub PAT).
4. This register pairs with [`README.md`](README.md#environment-variables) (the full env-var
   reference that names each service's connection variables).
