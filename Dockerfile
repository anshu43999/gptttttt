# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM golang:1.22-bookworm AS go-builder
WORKDIR /build/go-email-protocol
COPY go-email-protocol/go.mod go-email-protocol/go.sum ./
RUN go mod download
COPY go-email-protocol/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -tags tlsclient -trimpath -ldflags="-s -w" -o /out/email-protocol-worker ./cmd/email-protocol-worker

FROM python:3.13-slim AS app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    GPT_REGISTER_DB_BACKEND=sqlite \
    GPT_REGISTER_SKIP_ENV_DB=1 \
    GPT_REGISTER_HOST=0.0.0.0 \
    GPT_REGISTER_PORT=8000 \
    GPT_REGISTER_START_GO_WORKER=1 \
    GO_EMAIL_PROTOCOL_PURE_GO=1 \
    GO_EMAIL_PROTOCOL_MODE=live \
    GO_EMAIL_PROTOCOL_TRANSPORT=tls \
    GO_EMAIL_PROTOCOL_MAX_ACTIVE=1 \
    GO_GRAPH_MAX_CONCURRENT=4

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium \
    && python -m patchright install chromium

COPY api ./api
COPY application ./application
COPY core ./core
COPY database ./database
COPY domain ./domain
COPY infrastructure ./infrastructure
COPY platforms ./platforms
COPY providers ./providers
COPY registration ./registration
COPY services ./services
COPY main.py full_pipeline.py smstome_tool.py __init__.py ./
COPY config.docker.yaml ./config.yaml
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist
COPY --from=go-builder /out/email-protocol-worker /usr/local/bin/email-protocol-worker
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /app/data /app/output /app/tmp \
    && chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${GPT_REGISTER_PORT}/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]