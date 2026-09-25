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
WORKDIR /app
COPY --from=build /app/dist/ ./dist/
COPY --from=build /app/node_modules/ ./node_modules/
COPY package.json ./
COPY container/CLAUDE.md ./container/CLAUDE.md
COPY container/agent-runner/src/mcp-tools/*.instructions.md ./container/agent-runner/src/mcp-tools/
COPY container/skills/ ./container/skills/
RUN mkdir -p data groups store templates && chown -R node:node data groups store templates
ARG SOURCE_REVISION
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
USER node
CMD ["node", "dist/index.js"]
