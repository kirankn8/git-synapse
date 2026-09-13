FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && git --version

WORKDIR /app

# Dependencies first, so a source edit does not invalidate the pip layer.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
COPY web /app/web
RUN pip install --no-deps -e /app

# Test tools baked in so the suite never depends on reaching PyPI.
RUN pip install --no-cache-dir pytest pytest-cov pytest-timeout PyYAML ruff

# Mirrors live on a mounted volume; create the mount point with sane ownership.
RUN mkdir -p /data/mirrors

RUN useradd --create-home --uid 10001 git-synapse \
    && chown -R git-synapse:git-synapse /data /app
USER git-synapse

# Git needs a writable HOME; safe.directory stops a UID mismatch breaking clones.
ENV HOME=/home/git-synapse
RUN git config --global --add safe.directory '*'

ENV PYTHONPATH=/app/src \
    WEB_ROOT=/app/web \
    MIRROR_ROOT=/data/mirrors

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "git_synapse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
