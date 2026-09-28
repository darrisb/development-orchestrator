FROM python:3.12-slim

# The identity the image will report at /health. Defaults are the developer
# build, which reports "unknown/dev" rather than inventing a SHA: an image that
# does not know what it was built from is one that cannot be verified, and
# check_deployment_freshness.py refuses exactly that.
ARG SOURCE_REVISION=unknown/dev
ARG SOURCE_DIRTY=unknown
ARG BUILD_TIME=unknown

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY apps ./apps
RUN set -eu; \
    case "$SOURCE_DIRTY" in \
        true|1|yes) dirty=True ;; \
        false|0|no) dirty=False ;; \
        *) dirty=None ;; \
    esac; \
    printf 'SOURCE_REVISION = "%s"\nSOURCE_DIRTY = %s\nBUILD_TIME = "%s"\n' \
        "$SOURCE_REVISION" "$dirty" "$BUILD_TIME" \
        > apps/orchestrator/_build_meta.py
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn apps.orchestrator.main:app --host 0.0.0.0 --port 8000"]
