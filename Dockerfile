# syntax=docker/dockerfile:1

# Build stage: resolve the locked dependencies into a virtual environment with uv, then install the
# application itself (not editable), so the runtime image needs neither uv nor the source tree.
FROM python:3.14-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

# Runtime stage: slim Python, no build tools, a dedicated unprivileged user.
FROM python:3.14-slim-bookworm AS runtime
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ASPNETCORE_HTTP_PORTS=8080 \
    HEALTHCARE_WORKERS=2
EXPOSE 8080
USER 10001:10001

# "migrate" as the argument applies database migrations and exits; without it the API is served
# by Uvicorn with HEALTHCARE_WORKERS worker processes.
ENTRYPOINT ["python", "-m", "healthcare"]
CMD ["serve"]
