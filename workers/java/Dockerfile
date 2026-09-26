# Disposable coding worker, Java profile (build.md section 11).
#
# V1 implements the Node profile (section 42); this one exists so the
# WorkerService has a second real target and the profile allow-list has
# something to be checked against.
#
# The run-time policy lives in the WorkerService, not here. See
# workers/node/Dockerfile for why an arbitrary uid has to work.
FROM eclipse-temurin:21-jdk-jammy

# git is installed for tooling that reads repository metadata; a worker may not
# run Git itself (domain.commands, section 10).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        maven \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 worker

# Maven and Gradle write a great deal outside the project; both are pointed at
# the container's tmpfs, which is discarded with the worker.
ENV HOME=/tmp \
    MAVEN_OPTS=-Dmaven.repo.local=/tmp/.m2 \
    GRADLE_USER_HOME=/tmp/.gradle \
    CI=true

RUN mkdir -p /workspace && chgrp -R 0 /workspace && chmod -R g=u /workspace

USER worker
WORKDIR /workspace

CMD ["sleep", "infinity"]
