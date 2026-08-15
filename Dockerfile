FROM node:22-alpine AS frontend-build

WORKDIR /build

RUN corepack enable && corepack prepare pnpm@11.19.0 --activate

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./frontend/
RUN cd frontend && pnpm install --frozen-lockfile

COPY frontend ./frontend
RUN cd frontend && pnpm build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ASSET_HUB_DATA_DIR=/var/data \
    PYTHONPATH=/app/src

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend-build \
    /build/src/asset_compensation/web/static/dist \
    ./src/asset_compensation/web/static/dist

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       fonts-dejavu-core \
       libharfbuzz-subset0 \
       libpango-1.0-0 \
       libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir ".[deploy,email,pdf]" \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /bin/bash app \
    && mkdir -p /var/data \
    && chown -R app:app /app /var/data

USER app

EXPOSE 10000

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads ${GUNICORN_THREADS:-4} --timeout ${GUNICORN_TIMEOUT:-120} --access-logfile - --error-logfile - 'asset_compensation.web.app:create_app()'"]
