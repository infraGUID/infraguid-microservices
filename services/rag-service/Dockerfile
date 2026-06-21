# syntax=docker/dockerfile:1

# ─────────────────────────── Builder stage ───────────────────────────
# Installs the shared library + service dependencies into an isolated
# virtualenv. Build tooling stays in this stage and never ships to runtime.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Isolated venv + current build tooling (clears outdated pip/setuptools CVEs).
RUN python -m venv "$VIRTUAL_ENV" \
    && pip install --upgrade pip setuptools wheel

# Shared library first for better layer caching.
COPY shared /shared
RUN pip install /shared

COPY services/rag-service/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# ─────────────────────────── Runtime stage ───────────────────────────
# Minimal image: only the prebuilt venv + application code, run as a
# non-root user.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged system user/group (no shell, no home).
RUN groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid appgroup --no-create-home appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY services/rag-service/app ./app

USER appuser

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
