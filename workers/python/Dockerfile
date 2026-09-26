# Disposable coding worker, Python profile (build.md section 11).
#
# V1 implements the Node profile (section 42); this one is complete enough to
# run a Python project's verification commands and to keep the WorkerService
# honest about having more than one target.
#
# The run-time policy lives in the WorkerService, not here. See
# workers/node/Dockerfile for why an arbitrary uid has to work.
FROM python:3.12-slim

# git is installed for tooling that reads repository metadata; a worker may not
# run Git itself (domain.commands, section 10).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 worker \
    && pip install --no-cache-dir uv

ENV HOME=/tmp \
    PIP_CACHE_DIR=/tmp/.pip \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CACHE_DIR=/tmp/.uv \
    PYTHONDONTWRITEBYTECODE=1 \
    CI=true

RUN mkdir -p /workspace && chgrp -R 0 /workspace && chmod -R g=u /workspace

USER worker
WORKDIR /workspace

CMD ["sleep", "infinity"]
