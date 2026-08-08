#!/usr/bin/env sh
set -eu

mkdir -p /app/data /app/output /app/tmp

: "${GPT_REGISTER_HOST:=0.0.0.0}"
: "${GPT_REGISTER_PORT:=8000}"
: "${GPT_REGISTER_START_GO_WORKER:=1}"
: "${GO_EMAIL_PROTOCOL_MAX_ACTIVE:=1}"
: "${GO_GRAPH_MAX_CONCURRENT:=4}"
: "${GO_EMAIL_PROTOCOL_MODE:=live}"
: "${GO_EMAIL_PROTOCOL_TRANSPORT:=tls}"
: "${GO_EMAIL_PROTOCOL_PURE_GO:=1}"
export GPT_REGISTER_HOST GPT_REGISTER_PORT GPT_REGISTER_START_GO_WORKER
export GO_EMAIL_PROTOCOL_MAX_ACTIVE GO_GRAPH_MAX_CONCURRENT GO_EMAIL_PROTOCOL_MODE GO_EMAIL_PROTOCOL_TRANSPORT GO_EMAIL_PROTOCOL_PURE_GO

worker_pid=""

stop_children() {
  if [ -n "$worker_pid" ] && kill -0 "$worker_pid" 2>/dev/null; then
    kill "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  fi
}

trap 'stop_children; exit 143' TERM INT

if [ "$GPT_REGISTER_START_GO_WORKER" = "1" ]; then
  echo "[docker] starting email-protocol-worker on 127.0.0.1:18765"
  email-protocol-worker \
    -addr 127.0.0.1:18765 \
    -db /app/data/go-email-protocol-ledger.db \
    -key /app/data/go-email-protocol.key \
    -business-db /app/data/gpt_register.db \
    -work-root /app/data/go-email-protocol-jobs \
    -max-active "$GO_EMAIL_PROTOCOL_MAX_ACTIVE" \
    -graph-max-concurrent "$GO_GRAPH_MAX_CONCURRENT" \
    -pure-go \
    -protocol-mode "$GO_EMAIL_PROTOCOL_MODE" \
    -transport "$GO_EMAIL_PROTOCOL_TRANSPORT" \
    -skip-sdk-drift &
  worker_pid="$!"
fi

echo "[docker] starting FastAPI on ${GPT_REGISTER_HOST}:${GPT_REGISTER_PORT}"
uvicorn main:app --host "$GPT_REGISTER_HOST" --port "$GPT_REGISTER_PORT" &
app_pid="$!"

wait "$app_pid"
status="$?"
stop_children
exit "$status"