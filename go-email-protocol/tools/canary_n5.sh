#!/usr/bin/env bash
# Sequential n=5 pure-go live canary. Each run mints a fresh bestgo sticky session.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
GODIR="$ROOT/go-email-protocol"
OUT="$ROOT/output/pure_go_register_canary/n5"
BIN="$GODIR/bin/pure-go-register.exe"
SUMMARY="$OUT/summary.tsv"
LOG="$OUT/canary_n5_master.log"

# shellcheck disable=SC1091
set -a
# env.db is KEY=value lines; source carefully
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
    *=*) export "$line" ;;
  esac
done < "$ROOT/env.db"
set +a

mkdir -p "$OUT"
: > "$SUMMARY"
echo -e "run\texit\ttask\temail\ttoken_len\taccount_id\tfail_stage\tseconds" >> "$SUMMARY"
{
  echo "START $(date -Iseconds)"
  echo "binary=$BIN"
  echo "mailbox=outlook_token proxy=bestgo seed mint transport=tlsclient"
} | tee "$LOG"

ok=0
fail=0
for i in 1 2 3 4 5; do
  echo "===== RUN $i/5 $(date -Iseconds) =====" | tee -a "$LOG"
  MINT_OUT="$OUT/mint_$i.out"
  RUN_LOG="$OUT/run_$i.log"
  WIRE="$OUT/wire_$i"
  mkdir -p "$WIRE"
  export GPT_REGISTER_WIRE_DIR="$WIRE"

  (cd "$GODIR" && go run ./tools/mint_proxy bestgo,1024 JP) > "$MINT_OUT" 2>&1
  PROXY_URL=$(grep '^URL=' "$MINT_OUT" | sed 's/^URL=//')
  if [ -z "$PROXY_URL" ]; then
    echo "RUN $i mint failed" | tee -a "$LOG"
    echo -e "$i\t2\t\t\t0\t\tmint\t0" >> "$SUMMARY"
    fail=$((fail+1))
    continue
  fi
  echo "proxy_style=$(grep '^STYLE=' "$MINT_OUT")" | tee -a "$LOG"

  t0=$(date +%s)
  set +e
  (cd "$GODIR" && "$BIN" \
    -mailbox-provider outlook_token \
    -proxy "$PROXY_URL" \
    -browser firefox \
    -out "$OUT" \
    -timeout 15m \
    -otp-timeout 360s \
    -email-tries 5) > "$RUN_LOG" 2>&1
  EC=$?
  set -e
  t1=$(date +%s)
  sec=$((t1-t0))

  task=$(grep -m1 '^task=' "$RUN_LOG" | sed 's/^task=//' || true)
  email=$(grep -m1 'email_try=' "$RUN_LOG" | sed -n 's/.* email=\([^ ]*\).*/\1/p' || true)
  token_len=$(grep -m1 'SUCCESS access_token_len=' "$RUN_LOG" | sed -n 's/.*access_token_len=\([0-9]*\).*/\1/p' || true)
  account_id=$(grep -m1 'SUCCESS ' "$RUN_LOG" | sed -n 's/.*account_id=\([^ ]*\).*/\1/p' || true)
  if [ -z "${token_len:-}" ]; then token_len=0; fi
  fail_stage=""
  if [ "$EC" -ne 0 ]; then
    fail_stage=$(grep -E 'FATAL:|step .* → ERR' "$RUN_LOG" | tail -1 | tr '\t' ' ' | cut -c1-160 || true)
    fail=$((fail+1))
  else
    ok=$((ok+1))
  fi
  echo -e "$i\t$EC\t$task\t$email\t$token_len\t$account_id\t$fail_stage\t$sec" >> "$SUMMARY"
  echo "RUN $i exit=$EC token_len=$token_len sec=$sec" | tee -a "$LOG"
  # brief pause between runs
  sleep 2
done

{
  echo "===== DONE $(date -Iseconds) ok=$ok fail=$fail ====="
  cat "$SUMMARY"
} | tee -a "$LOG"

if [ "$ok" -lt 5 ]; then
  exit 1
fi
exit 0
