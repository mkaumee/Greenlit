# One image, two services.
#
#   orchestrator.app:app        the tick loop, woken by Cloud Scheduler
#   orchestrator.approvals:app  the money path, called by a producer
#
# They differ by CMD and by service account, not by build. That is deliberate:
# the boundary between "can spend money" and "cannot" is the IAM grant on the
# orders database, and making it a fork of the code as well would invite the two
# to drift while looking identical. See CLAUDE.md, Hard Rule 5.
#
# The default CMD is the tick service, because that is the one on a schedule.
# scripts/deploy.sh overrides it for the approvals service.
#
# Build from the repository root, never from orchestrator/:
#
#   docker build -t agentic-cinema .
#
# `contracts` is a uv workspace member reached by a path dependency, so a build
# context of orchestrator/ cannot see it and the sync fails.

# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
FROM python:3.14-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer. Only the manifests are copied, so
# editing a source file does not re-resolve and re-download the whole tree —
# which is most of the build time.
COPY pyproject.toml uv.lock ./
COPY contracts/pyproject.toml contracts/
COPY orchestrator/pyproject.toml orchestrator/
COPY supplier-sim/pyproject.toml supplier-sim/
RUN uv sync --frozen --no-dev --no-install-workspace --package orchestrator

# Then the code, and install the workspace members themselves.
COPY contracts/ contracts/
COPY orchestrator/ orchestrator/
RUN uv sync --frozen --no-dev --package orchestrator

# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
FROM python:3.14-slim-bookworm

# Non-root. Cloud Run does not require it, and it costs nothing to do anyway.
RUN useradd --create-home --uid 1000 cinema
USER cinema

WORKDIR /app
COPY --from=build --chown=cinema:cinema /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

# Shell form on purpose: Cloud Run injects $PORT and it has to be expanded.
#
# One worker. The loop holds nothing in memory between requests, so several
# would be safe — but they would also mean several ticks running at once
# against the same due queue, which is exactly the overlap the claiming in
# repository.py exists to survive. Concurrency belongs to Cloud Run's instance
# count, where it is visible.
CMD exec uvicorn orchestrator.app:app --host 0.0.0.0 --port "$PORT" --workers 1
