# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the environment.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY scripts ./scripts
# The migrations and their config. Without these the container starts, reports
# itself healthy right up to the lifespan, and then fails with "No
# 'script_location' key found" -- which names alembic's configuration rather
# than the missing file, and looks nothing like "the image is incomplete".
COPY alembic.ini ./
COPY migrations ./migrations
COPY data/catalog ./data/catalog
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Deliberately no VOLUME for the data directory. Declaring one at
# /app/data/raw creates an anonymous volume that shadows a bind mount of the
# *parent* /app/data -- so the container sees an empty directory and the seed
# service re-downloads 180 MB that is already on the host, with no error to
# explain why. Compose provides the mount instead.

EXPOSE 8000
CMD ["uvicorn", "ledger.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
