# Single image for every Python service (api, scheduler, mcp, cli). Building one
# image instead of four keeps the layer cache warm and guarantees that all
# services run byte-identical code.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is the core dependency of the ingest pipeline, not a build tool.
# ca-certificates is required for HTTPS clones and the GitHub API.
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

# The test tools, baked in rather than installed on every run. Fetching them
# each time made the suite depend on PyPI being reachable and turned a busy
# machine into an apparent deadlock: pip waiting on the network, with the whole
# run buffered behind it and nothing on screen to say so.
RUN pip install --no-cache-dir pytest pytest-cov pytest-timeout PyYAML

# Mirrors live on a mounted volume; create the mount point with sane ownership.
RUN mkdir -p /data/mirrors

# Unprivileged runtime user. Git refuses to operate on repositories owned by a
# different user, so the mirror tree is handed over explicitly.
RUN useradd --create-home --uid 10001 git-synapse \
    && chown -R git-synapse:git-synapse /data /app
USER git-synapse

# Git needs a writable HOME for its config; the mirrors are also marked safe so
# a UID mismatch on a bind-mounted volume cannot break every clone.
ENV HOME=/home/git-synapse
RUN git config --global --add safe.directory '*'

ENV PYTHONPATH=/app/src \
    WEB_ROOT=/app/web \
    MIRROR_ROOT=/data/mirrors

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "git_synapse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
