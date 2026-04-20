#!/usr/bin/env bash
# Toggle Valkey OOM scenario for debugging
# Usage: ./scripts/valkey-oom.sh on|off|status

set -euo pipefail

CONTAINER="valkey-cart"
CLI="docker exec $CONTAINER valkey-cli"

info() { $CLI INFO memory 2>/dev/null | grep -E "used_memory_human|maxmemory_human"; }

case "${1:-status}" in
  on)
    USED=$($CLI INFO memory | grep "used_memory:" | cut -d: -f2 | tr -d '\r')
    LIMIT=$(( USED - 30000 ))
    $CLI CONFIG SET maxmemory "$LIMIT" > /dev/null
    $CLI CONFIG SET maxmemory-policy noeviction > /dev/null
    echo "🔴 OOM enabled — maxmemory set below current usage"
    info
    echo ""
    echo "Verify: docker exec $CONTAINER valkey-cli SET test fail"
    ;;
  off)
    $CLI CONFIG SET maxmemory 0 > /dev/null
    echo "🟢 OOM disabled — no memory limit"
    info
    ;;
  status)
    MAX=$($CLI CONFIG GET maxmemory | tail -1 | tr -d '\r')
    if [ "$MAX" = "0" ]; then
      echo "🟢 Normal — no memory limit"
    else
      echo "🔴 OOM active — maxmemory=$MAX"
    fi
    info
    ;;
  *)
    echo "Usage: $0 on|off|status"
    exit 1
    ;;
esac
