# Multi-stage, slim, non-root, read-only rootfs. §11.8.
#
# The decision and management workloads share this image and differ only by
# DJANGO_SETTINGS_MODULE: `server.settings` or `server.settings_management`.
# One image means one thing to scan, sign and audit — and it means a decision
# worker and a management worker cannot drift into being built differently.
#
# An earlier version of this comment named a COMPLYLAYER_ROLE variable that was
# never implemented, which is the kind of thing a reader only discovers when
# their deployment does not do what the comment said.

FROM python:3.12-slim-trixie AS build

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
# The wheel's long description. `pyproject.toml` points `readme` at this file,
# so the build needs it — copied here rather than beside pyproject.toml so that
# editing the README does not invalidate the dependency layer above.
COPY README.md ./
RUN uv sync --frozen --no-dev


# Trixie (Debian 13), not bookworm. The first release dry run failed its image
# scan on 34 HIGH/CRITICAL findings in the bookworm package set, every one of
# them with an empty Fixed Version — `affected`, `fix_deferred` or
# `will_not_fix`, so no amount of rebuilding on bookworm would have cleared
# them. Moving the floor is the only thing that actually removes a CVE rather
# than agreeing to overlook it.
#
# Both stages move together. A build stage on one Debian and a runtime on
# another compiles wheels against one glibc and runs them against a different
# one, which fails at import time and looks like a packaging bug.
FROM python:3.12-slim-trixie AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=server.settings

# Apply whatever security updates Debian has shipped since this base image was
# published. The first trixie dry run failed the release scan on `bsdutils
# 1:2.41-5`, where `2.41.5-0+deb13u1` had been available for a while — the
# patch existed, the base image simply predated it, and base images are rebuilt
# on somebody else's cadence.
#
# The cost is honest: this makes the build non-reproducible, because the same
# Dockerfile produces different packages on different days. That is the right
# trade for a container that makes compliance decisions — the alternative is
# reproducibly shipping a known-vulnerable package — but it is a trade, and the
# image digest is what pins a release rather than this file.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

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
