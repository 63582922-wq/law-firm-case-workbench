ARG NODE_IMAGE=node:22.23.1-bookworm-slim

FROM ${NODE_IMAGE} AS dependencies
WORKDIR /workspace/web
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile

FROM ${NODE_IMAGE} AS build
WORKDIR /workspace/web
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=dependencies /workspace/web/node_modules ./node_modules
COPY . ./
# The UI intentionally reads the shared, versioned legal-source catalogs from
# the repository root.  Compose supplies this as a separate build context so
# no desktop build output or private case data enters the frontend image.
COPY --from=knowledge . /workspace/knowledge/
# The dependency stage has already resolved the pinned package manager and
# installed the lockfile.  Invoking Corepack again in this clean stage can
# cause an unnecessary network download of pnpm, turning a deterministic
# image build into a transient registry dependency.  Next is installed in the
# copied dependency tree, so execute that exact pinned binary directly.
RUN ./node_modules/.bin/next build

FROM ${NODE_IMAGE} AS runtime
# Next's standalone bundle preserves the `web/` workspace directory because
# output-file tracing is rooted at `/workspace`. Keep that layout intact and
# start the generated server from the directory it was built for.
WORKDIR /app/web
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

COPY --from=build --chown=node:node /workspace/web/.next/standalone /app
COPY --from=build --chown=node:node /workspace/web/.next/static /app/web/.next/static
COPY --from=build --chown=node:node /workspace/web/public /app/web/public

USER node
EXPOSE 3000
CMD ["node", "server.js"]
