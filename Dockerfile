ARG JOB_TYPE

FROM python:3.12-slim AS builder
ARG GITHUB_TOKEN
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY pii_triage_merged/requirements.txt .
RUN python -m venv /venv && \
    /venv/bin/pip install \
    -r requirements.txt \
    git+https://${GITHUB_TOKEN}@github.com/ldmglobal-com/scaling-lib.git@dev

FROM python:3.12-slim
WORKDIR /app
# LibreOffice headless converts legacy .doc/.xls/.ppt -> OOXML inline on this same Linux
# worker (see pii_triage/conversion.py) -- this is what eliminates the separate Windows
# VM/queue leg entirely: writer/calc/impress cover the three formats without the full
# office suite (Draw, Base, Math). antiword is a lightweight, near-free .doc text-extraction
# fallback (pii_triage/extractors.py's x_doc) for the rare case LibreOffice conversion itself
# fails on a given file.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress antiword \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /venv /venv
COPY worker.py .
COPY collect_outputs.py .
COPY pii_triage_merged/pii_triage/ pii_triage/
ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV JOB_TYPE=$JOB_TYPE
CMD ["python", "worker.py"]
