# Information Sources & Credentials

What information **pii_triage** draws on to do its job, and what credentials it needs to access
those sources. Two parts:

1. **Sources of information** — every input the tool ingests or consults (reference data, per-matter
   inputs, external inference services, review ground-truth, operational/cost data, configuration).
2. **Credentials & authentication** — the identities, secrets, and token scopes it uses, where each
   comes from, and how it's protected.

> **Scope & safety.** No secret *values* live in this repo or this document — they come from `.env`
> (git-ignored) or Azure Key Vault at runtime. Renewal/expiry and account ownership for these
> credentials are tracked in [`DEPENDENCIES.md`](DEPENDENCIES.md) §5–§6. The tool itself **never
> stores, logs, or outputs a PII value** — it consumes file content but emits only entity *type*
> counts and routing labels.

---

## 1. Sources of information

### 1.1 Reference data the tool is configured with

The detection logic is driven by **entity definitions, never values** — there is no database of
real PII, names, or addresses anywhere in the tool.

| Source | Provides | Where it lives | Notes |
|---|---|---|---|
| **Master List / default rulepack** | The entity definitions (regex/keyword/name/address/ssn/labeled_value/money) and the 11 PI-Type categories | `pii_triage_merged/rulepacks/default.yaml` (embedded in the package) | Loaded by `config.load_rulepack`; the built-in default needs no file to be mounted |
| **Client-protocol rulepack** | Cognicion CIR protocol decomposed to 56 leaf entities | `pii_triage_merged/rulepacks/cognicion-cir.yaml` | Opt-in via `--rulepack` |
| **Custom rulepack** | A per-matter Master List | path in `RULEPACK_PATH` (mounted) or `--rulepack` | Inherits unspecified top-level keys from the default |

### 1.2 Per-matter inputs

| Source | Provides | Where it lives | How consumed |
|---|---|---|---|
| **The corpus** | The documents to triage | `INPUT_MOUNT/<job>/files/` | Read-only; extracted per file, values discarded after counting |
| **Matter protocol document** | Responsiveness judgment context injected into the LLM prompts | `<job>/protocol.{pdf,docx,doc,txt,rtf}` (sibling of `files/`) | Extracted to text, capped at 8,000 chars (`cfg.protocol_text[:8000]`), looked up + cached per job dir |
| **Per-job config** | Optional per-matter BDE threshold | `<job>/pii_job.json` (written by `enqueue.py --bde-threshold`) | Read per job dir by `worker.py::_bde_threshold_for` |

### 1.3 External inference services

These are the only sources that "read" document content off-box, and only when explicitly enabled.

| Source | Provides | Enabled by | Access |
|---|---|---|---|
| **Azure OpenAI** | Responsiveness judgment, BDE person-count, stage-2 graded overview | `--llm` / `USE_LLM` | `AZURE_OPENAI_ENDPOINT` + deployment; AAD token (no key) |
| **Azure AI Document Intelligence** | OCR (`prebuilt-layout`) of image-only files and embedded PDF images | `--ocr` / `USE_OCR` | `AZURE_DI_ENDPOINT`; AAD token (no key) |
| **spaCy `en_core_web_sm`** (optional) | Statistical name (PERSON) recognition | `--ner` / `USE_NER` | Local model; must be installed |

The model returns its reasoning to none of the outputs: stage-2's client omits `reasoning`
entirely, and the stage-1 callers never read it — so no model-surfaced value can reach the CSV.

### 1.4 Review / ground-truth inputs (scoring & Table 2)

Supplied by reviewers, used only for measurement — never by the pipeline's own decisions.

| Source | Provides | Used by |
|---|---|---|
| **Entities export** (e.g. `…CNG_Entities Export.csv`) | Per-file entity counts → responsive (`>0`) / BDE (`>threshold`) ground truth | `tools/score_combined.py --entities` (`--id-col "Control ID"`, `--count-col "Total Entities"`) |
| **Gold sheet** (xlsx/csv) | Reviewer yes/no coding | `python -m pii_triage benchmark` |
| **Coded sample** | `gold_responsive` / `gold_bde` filled in by reviewers on the drawn sample | `python -m pii_triage estimate` → HWE Table 2 |

### 1.5 Operational & cost data

| Source | Provides | Consumed by | Access |
|---|---|---|---|
| **Azure Status Table** | Per-task status, tokens, checkpoints, timings | `collect_outputs.py`, `hwe_scaled_store.py`, `worker_status.py`, UI monitor | `AZURE_STORAGE_TABLE_URL` / connection string |
| **Azure Storage Queues** | Queue depth (approx) | UI monitor, `scaling-lib status` | `AZURE_STORAGE_QUEUE_URL` / connection string |
| **Azure Resource Manager** (`management.azure.com`) | Container App vCPU/memory/workload profile | `collect_outputs.py::_fetch_worker_config` | AAD token, mgmt scope |
| **Azure Retail Prices API** (`prices.azure.com`) | Public per-vCPU/GB/GPU hourly rates | `collect_outputs.py` compute-cost pricing | None (public, unauthenticated) |

### 1.6 Configuration sources

| Source | Provides | Notes |
|---|---|---|
| **`.env` file** | All runtime settings (endpoints, queues, mounts, flags, thresholds) | Loaded by `worker.py`/`enqueue.py`/`collect_outputs.py` and the CLI; git-ignored; the tool prints only *how many* settings loaded, never their values |
| **Environment variables** | Same keys, taking precedence over `.env` | Full reference in [`README.md`](README.md#environment-variables) |
| **Azure Key Vault** (optional) | Endpoints/secrets, when `AZURE_KEY_VAULT_URL` is set | `azure_clients._kv_secret` via `SecretClient` + `DefaultAzureCredential`; per-process cached |

### 1.7 Sources deliberately **not** used

- **No name lists / dictionaries.** Name detection is structural (title, field label, salutation) —
  there is deliberately no list of common first names, which both misses real names and
  false-matches companies/places.
- **No external PII databases, no third-party lookups, no telemetry of document content.**
- **No network at all in the rules pass.** Network is used only under `--ocr` / `--llm`.

---

## 2. Credentials & authentication

No endpoint or credential is hardcoded, and **no API keys are used anywhere** — Azure OpenAI and
Document Intelligence authenticate with AAD tokens obtained through the Azure identity chain.

### 2.1 Azure identity (the primary credential)

| Mode | When | How |
|---|---|---|
| **Managed identity** (via `DefaultAzureCredential`) | Production (workers on Container Apps) | The Container App's assigned identity; no secret to manage |
| **Service principal** (via `DefaultAzureCredential`) | Non-managed hosts | `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` (the secret **is** sensitive) |
| **Azure CLI** (`AzureCliCredential`) | Local dev, when `AZURE_CREDENTIAL_TYPE=cli` | Uses your `az login` session |

`collect_outputs.py:70-75` selects `AzureCliCredential` vs `DefaultAzureCredential` on
`AZURE_CREDENTIAL_TYPE`; `azure_clients.py:52-54` uses `DefaultAzureCredential` for Key Vault.

### 2.2 Storage authentication (queues + table)

| Mode | Variable(s) | Use |
|---|---|---|
| **Connection string** | `AZURE_STORAGE_CONNECTION_STRING` | Local / Azurite (contains an account key — sensitive) |
| **Managed identity** | `AZURE_STORAGE_QUEUE_URL` + `AZURE_STORAGE_TABLE_URL` | Production (token via `DefaultAzureCredential`) |

`hwe_scaled_store.py` reports which mode is active and can probe a real token against
`https://storage.azure.com/.default`; it never prints the connection string.

### 2.3 Other credentials & secrets

| Credential | Purpose | Source | Sensitivity |
|---|---|---|---|
| `AZURE_CLIENT_SECRET` | Service-principal auth (non-MI hosts) | `.env` / secret store | **Secret** — expires; track in `DEPENDENCIES.md` §5 |
| `GITHUB_TOKEN` | Build-time install of the private `scaling-lib` from GitHub | Docker `--build-arg` (not baked into the image) | **Secret** — PAT, expires |
| `AZURE_KEY_VAULT_URL` | Points at the vault that holds endpoints/secrets | `.env` | Not itself secret; gates KV access |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Telemetry ingestion | injected at runtime | Contains an ingestion key — inject, never bake into the image |

### 2.4 Token scopes requested

| Scope | Requested by | For |
|---|---|---|
| `https://management.azure.com/.default` | `collect_outputs.py` | Container App / environment introspection (compute cost) |
| `https://storage.azure.com/.default` | `hwe_scaled_store.py` (probe) | Storage queue/table access under managed identity |
| Key Vault (`SecretClient`) | `azure_clients.py` | Reading endpoint/secret entries when `AZURE_KEY_VAULT_URL` is set |
| Azure OpenAI / Document Intelligence | via `scaling_lib.ai` clients | LLM / OCR calls (AAD token, no key) |

### 2.5 What is **not** stored or hardcoded

- No API keys for Azure OpenAI or Document Intelligence — AAD tokens only.
- No endpoints or credentials hardcoded in source (the one literal is a deployment-*name* fallback,
  `gpt-4.5-nano`, not a secret).
- `.env` is git-ignored; the tool logs only the *count* of settings loaded, never values.
- No PII values in any output, log, `result.json`, `inventory.csv`, or the UI.

---

## 3. Where to go next

- **Renewal dates & account ownership** for these credentials → [`DEPENDENCIES.md`](DEPENDENCIES.md)
  §5–§6.
- **Full environment-variable reference** (every source's connection variable) →
  [`README.md`](README.md#environment-variables).
- **How the sources flow through the pipeline** → [`ARCHITECTURE.md`](ARCHITECTURE.md).
