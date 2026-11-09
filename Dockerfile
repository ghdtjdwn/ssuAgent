# Both stages must use the same Python minor version: the builder writes the
# venv under .venv/lib/python3.12 and the runtime interpreter has to find it.
# Stage 1: install dependencies with uv
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
# --no-install-project installs only deps (not the project itself) so ssu_agent/
# directory is not needed yet — preserves Docker layer cache for dependency changes.
RUN uv sync --no-dev --frozen --no-install-project
COPY ssu_agent ./ssu_agent
RUN uv sync --no-dev --frozen

# Stage 2: minimal runtime image
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS runtime
# CI passes the main commit that produced this image; GET /health reports it as
# build_sha so a running container can be matched to its GHCR sha-<commit> tag.
ARG GIT_SHA=unknown
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/ssu_agent /app/ssu_agent
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSUAGENT_BUILD_SHA=${GIT_SHA}
# Fixed non-root UID 1001; the production Compose service runs the container
# with the same uid, so local docker runs and production agree. The venv is
# read-only at runtime.
RUN useradd --uid 1001 --no-create-home --shell /usr/sbin/nologin agent
USER 1001
EXPOSE 8000
CMD ["uvicorn", "ssu_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
