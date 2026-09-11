FROM node:22.22.0-alpine3.23

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY deploy/astraquote-mcp/package.json deploy/astraquote-mcp/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force
COPY deploy/astraquote-mcp/server.js ./server.js
COPY deploy/astraquote-mcp/instructions.zh-CN.md ./instructions.zh-CN.md
COPY deploy/astraquote-mcp/lib ./lib
COPY policies ./policies

USER node
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8000/healthz >/dev/null || exit 1

CMD ["node", "server.js"]
