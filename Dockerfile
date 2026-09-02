ARG JOB_TYPE

FROM python:3.12-slim AS builder
RUN apt-get update && rm -rf /var/lib/apt/lists/*
COPY pii_triage_merged/requirements.txt .
# If a local `scaling_lib/` checkout is present in the build context, copy it and install
COPY scaling_lib/ /scaling_lib/
RUN set -eux; \
    python -m venv /venv; \
    # prefer installing the vendored scaling_lib when present; pip will ignore missing extras
    /venv/bin/pip install -r requirements.txt /scaling_lib[dev]

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
