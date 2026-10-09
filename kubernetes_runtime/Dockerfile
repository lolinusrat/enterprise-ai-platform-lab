# Two stages: resolve and install pinned deps with uv, then copy only the venv and code
# into a slim runtime image that runs as a non-root user.
FROM ghcr.io/astral-sh/uv:0.11.19-python3.12-trixie-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY agent_api ./agent_api

FROM python:3.12.15-slim
ARG APP_VERSION=dev
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 APP_VERSION=${APP_VERSION}
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin agent
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/agent_api /app/agent_api
USER 10001
EXPOSE 8000
# --timeout-graceful-shutdown bounds how long in-flight requests get after SIGTERM;
# it stays below terminationGracePeriodSeconds minus the preStop sleep.
CMD ["uvicorn", "agent_api.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "15"]
