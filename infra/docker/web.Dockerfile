# syntax=docker/dockerfile:1.7
#   docker build -f infra/docker/web.Dockerfile -t jobpulse-web apps/web

ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-trixie-slim AS deps
RUN corepack enable
WORKDIR /app
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile

FROM node:${NODE_VERSION}-trixie-slim AS build
RUN corepack enable
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Build-time placeholders only; real secrets are injected at runtime and never baked in.
RUN API_JWT_SECRET=build-placeholder-not-a-secret-000000000 \
    AUTH_SECRET=build-placeholder-not-a-secret-000000000 \
    pnpm build

FROM node:${NODE_VERSION}-trixie-slim AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
WORKDIR /app
RUN groupadd --system --gid 10001 jobpulse && useradd --system --uid 10001 --gid jobpulse jobpulse
COPY --from=build --chown=jobpulse:jobpulse /app/.next/standalone ./
COPY --from=build --chown=jobpulse:jobpulse /app/.next/static ./.next/static
USER jobpulse
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/dashboard').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
