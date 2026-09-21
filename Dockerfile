# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the application first (layer caching): copy only what pip needs to
# resolve dependencies before the rest of the source tree.
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY docs ./docs

# Non-root user; the SQLite data directory is a mounted volume owned by it.
RUN useradd --create-home --uid 10001 rustdesk && \
    mkdir -p /app/data && \
    chown -R rustdesk:rustdesk /app
USER rustdesk

# Which commit this image was built from (shown on the Dashboard). Declared this
# late so a new commit does not invalidate the dependency layers above.
ARG GIT_COMMIT=""
ENV GIT_COMMIT=${GIT_COMMIT}
# Also as a file, which the app reads first: a container manager that saves the old
# container's environment would otherwise re-apply an old GIT_COMMIT to a new image.
RUN printf '%s' "${GIT_COMMIT}" > /app/BUILD_COMMIT

ENV RUSTDESK_API_HOST=0.0.0.0 \
    RUSTDESK_API_PORT=21114 \
    DATABASE_URL=sqlite:///./data/rustdesk.db

EXPOSE 21114

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys,os; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('RUSTDESK_API_PORT', '21114') + '/health', timeout=3)" || exit 1

# uvicorn handles SIGTERM/SIGINT gracefully by default; run it as PID 1
# directly (no shell wrapper) so signals from `docker stop` reach it.
# The entrypoint is the CLI and the default command is `serve`, so the same image
# also runs the management commands, e.g. `docker run --rm IMAGE generate-key`.
ENTRYPOINT ["rustdesk-api"]
CMD ["serve"]
