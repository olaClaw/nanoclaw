# A reviewed context contains only the runner source and selected shared skills.
# Build the existing container/Dockerfile first, from its own seven-file context.
ARG BASE_IMAGE=nanoclaw-agent-base:local
FROM ${BASE_IMAGE}

USER root
COPY --chown=node:node container/agent-runner/src/ /app/src/
COPY --chown=node:node container/skills/ /app/skills/
ARG SOURCE_REVISION
ARG SOURCE_TREE
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
LABEL org.olaclaw.source.tree="${SOURCE_TREE}"
USER node
