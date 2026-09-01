"""pii_triage — read-only, parallel, crash-safe PII triage scanner.

Phase 1 of the document-review toolchain: walk a corpus once and produce a
routing inventory (one row per file) describing what each file is, whether its
text is extractable, an estimate of how many data subjects it contains, which
PII *types* were detected (never the values), and which processing lane it
should go to.

Optional enrichment (off by default, see README): Azure Document Intelligence
OCR turns non-searchable files into text, and an Azure OpenAI model is consulted
ONLY on ambiguous files to make the responsiveness call. Both require Azure and
the relevant SDKs; without them the tool degrades to the rules-only path.

Design guarantees (see README): read-only over the corpus, no code execution,
no PII in any output (labels and counts only), bounded per-file work, fault
isolation, and crash-safe resume. The rules pass is deterministic and makes no
network calls; network is used only when OCR/LLM are explicitly enabled, and the
LLM runs at temperature 0 to keep its judgments as stable as a model allows.
"""

__version__ = "3.0.0"
__all__ = ["__version__"]