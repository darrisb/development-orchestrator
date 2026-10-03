# Disposable coding worker, Node profile (build.md section 11).
#
# Policy enforced at run time by the WorkerService, not here: no Docker socket,
# no host root filesystem, no added capabilities, no new privileges, a read-only
# root filesystem, CPU/memory/PID limits, and no network unless an operator
# turns one on. The run's worktree is the only writable project mount.
#
# The image itself only has to provide the toolchain and be usable by an
# arbitrary uid: the worker runs as the orchestrator's own user so that files it
# writes into the worktree stay host-owned, which means it will not find itself
# in /etc/passwd. Everything below keeps that working.
FROM node:22-bookworm-slim

# git is present because tooling reads repository metadata, not because a worker
# may run Git: `git` is on the never-permitted list (domain.commands), since
# every Git operation belongs to GitService (section 10).
# make/g++/python3 are node-gyp's prerequisites -- "common build utilities".
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-sandbox \
        g++ \
        git \
        make \
        python3 \
    && rm -rf /var/lib/apt/lists/*

# A writable HOME for an unknown uid. The service also points every package
# manager's cache at /tmp, which is a tmpfs discarded with the container.
# Browser tests run inside the worker's existing Docker security boundary.
# Chromium's own Linux sandbox cannot initialize under no-new-privileges, so
# Karma launches this wrapper while Docker isolation remains fully enforced.
RUN printf '#!/bin/sh\nexec /usr/bin/chromium --headless --no-sandbox "$@"\n' \
        > /usr/local/bin/orchestrator-chromium \
    && chmod 0755 /usr/local/bin/orchestrator-chromium

ENV HOME=/tmp \
    NPM_CONFIG_CACHE=/tmp/.npm \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    CHROME_BIN=/usr/local/bin/orchestrator-chromium \
    CI=true

# Group 0 with group-write on the workdir: an arbitrary uid still belongs to
# root's group under Docker, so this is what makes an unknown user able to work
# here at all.
RUN mkdir -p /workspace && chgrp -R 0 /workspace && chmod -R g=u /workspace

# The default when no --user is given. Overridden at run time.
USER node
WORKDIR /workspace

# The worker is created, then commands are executed in it one at a time, then it
# is destroyed. It never runs anything by itself.
CMD ["sleep", "infinity"]
