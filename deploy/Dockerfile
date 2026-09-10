FROM node:22-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM node:22-bookworm-slim AS node-runtime

FROM node:22-bookworm-slim AS mcp-dependencies

WORKDIR /build/mcp
COPY deploy/astraquote-mcp/package.json deploy/astraquote-mcp/package-lock.json ./
RUN npm ci --omit=dev

FROM python:3.12-slim-bookworm AS runtime

LABEL com.astraquote.image="true"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    PORT=3000 \
    BACKEND_API_URL=http://127.0.0.1:8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gzip tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir . \
    && rm -rf /root/.cache/pip /var/lib/apt/lists/*

COPY --from=frontend-builder /build/frontend/dist /app/frontend/dist
COPY --from=frontend-builder /build/frontend/node_modules /app/frontend/node_modules
COPY --from=frontend-builder /build/frontend/package.json /app/frontend/package.json
COPY --from=mcp-dependencies /build/mcp/node_modules /app/mcp/node_modules
COPY deploy/astraquote-mcp/package.json /app/mcp/package.json
COPY deploy/astraquote-mcp/server.js /app/mcp/server.js
COPY deploy/astraquote-mcp/instructions.zh-CN.md /app/mcp/instructions.zh-CN.md
COPY deploy/astraquote-mcp/lib /app/mcp/lib
COPY policies /app/policies
COPY deploy/astraquote-mcp-oauth/app.py /app/oauth/app.py
COPY deploy/cache/aws_catalog.sqlite3.gz /app/cache-seed/aws_catalog.sqlite3.gz
COPY deploy/start-production.sh /usr/local/bin/start-production

RUN chmod 0755 /usr/local/bin/start-production \
    && mkdir -p /data/aws /data/oauth /data/v2-quotes

EXPOSE 3000 8001
HEALTHCHECK --interval=30s --timeout=8s --start-period=45s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3000/api/backend/api/health >/dev/null \
    && curl -fsS http://127.0.0.1:8001/readyz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/start-production"]
