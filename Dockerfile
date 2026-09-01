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
COPY --from=builder /venv /venv
COPY worker.py .
COPY collect_outputs.py .
COPY pii_triage_merged/pii_triage/ pii_triage/
ENV PATH="/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV JOB_TYPE=$JOB_TYPE
CMD ["python", "worker.py"]
