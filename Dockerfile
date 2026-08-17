# Multi-stage, slim, non-root, read-only rootfs. §11.8.
#
# The decision and management workloads share this image and differ only by
# COMPLYLAYER_ROLE, which selects a settings module. One image means one thing
# to scan, sign and audit — and it means a decision worker and a management
# worker cannot drift into being built differently.

FROM python:3.12-slim-bookworm AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies before source, so a code change does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY complylayer/ ./complylayer/
COPY server/ ./server/
COPY manage.py ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=server.settings

# Non-root, and the account has no shell. A container that cannot log in is one
# fewer thing to reason about when somebody finds a way to run a command in it.
RUN groupadd --system --gid 1001 complylayer \
 && useradd --system --uid 1001 --gid complylayer --shell /usr/sbin/nologin complylayer

WORKDIR /app
COPY --from=build --chown=complylayer:complylayer /app /app

USER complylayer

EXPOSE 8000

# Readiness is the cache being warm, not the process being up (§11.1). A worker
# that answers before it has compiled its rule set puts a latency spike on every
# deploy.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"

# Read-only rootfs is set by the orchestrator (`readOnlyRootFilesystem: true`),
# not here — this only makes sure nothing in the image needs to write outside
# the durable audit queue's mount.
ENTRYPOINT ["python", "-m", "gunicorn", "server.asgi:application", \
            "--worker-class", "uvicorn.workers.UvicornWorker", \
            "--bind", "0.0.0.0:8000", \
            "--workers", "4", \
            "--graceful-timeout", "30", \
            "--access-logfile", "-"]
