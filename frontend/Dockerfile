# syntax=docker/dockerfile:1.7
ARG NODE_IMAGE=node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1
FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM ${NODE_IMAGE} AS runtime
ARG RELEASE_ID
RUN test -n "${RELEASE_ID}"
LABEL org.opencontainers.image.title="Memes DB frontend" \
      org.opencontainers.image.revision="${RELEASE_ID}"
WORKDIR /app
COPY --from=build --chown=node:node /app/dist ./dist
COPY --chown=node:node server.mjs ./
USER node
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["node", "server.mjs"]
