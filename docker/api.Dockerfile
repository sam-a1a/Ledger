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
COPY data/catalog ./data/catalog
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# The dataset is seeded into a volume, never baked into the image: it is
# ~180 MB and changes on a different cadence from the code.
VOLUME ["/app/data/raw"]

EXPOSE 8000
CMD ["uvicorn", "ledger.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
