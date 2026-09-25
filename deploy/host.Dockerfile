# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM node:22-slim AS build
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@10.34.5 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml .npmrc tsconfig.json ./
RUN pnpm install --frozen-lockfile
COPY src/ src/
RUN pnpm exec tsc && pnpm prune --prod --ignore-scripts

FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/nanoclaw
COPY --from=build /app/dist/ ./dist/
COPY --from=build /app/node_modules/ ./node_modules/
COPY package.json ./
COPY container/CLAUDE.md ./container/CLAUDE.md
COPY container/agent-runner/src/mcp-tools/*.instructions.md ./container/agent-runner/src/mcp-tools/
COPY container/skills/ ./container/skills/
COPY deploy/healthcheck.mjs ./deploy/healthcheck.mjs
COPY deploy/onecli-egress.mjs ./deploy/onecli-egress.mjs
COPY deploy/release-manifest.mjs ./deploy/release-manifest.mjs
COPY deploy/check-release.mjs ./deploy/check-release.mjs
COPY deploy/bootstrap.mjs ./deploy/bootstrap.mjs
RUN mkdir -p data groups store templates && chown -R node:node data groups store templates
ARG SOURCE_REVISION
ARG SOURCE_TREE
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
LABEL org.olaclaw.source.tree="${SOURCE_TREE}"
ENV NANOCLAW_SOURCE_REVISION="${SOURCE_REVISION}"
ENV NANOCLAW_SOURCE_TREE="${SOURCE_TREE}"
USER node
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 CMD node deploy/healthcheck.mjs
CMD ["/bin/sh", "-ec", "node deploy/check-release.mjs && exec node dist/index.js"]
