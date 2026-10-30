#!/usr/bin/env bash
# Helm lint + unit tests for the observability-stack chart.
# Requires: helm, helm-unittest plugin (helm plugin install https://github.com/helm-unittest/helm-unittest.git)
# Usage: ./test/helm-test.sh
set -euo pipefail

CHART="charts/observability-stack"

echo "==> compose/chart shared files"
# The chart cannot .Files.Get outside its own directory, so these are copies of the Docker
# Compose files and must stay byte-identical (see charts/observability-stack/SYNC.md).
# Behaviour that differs between the two is switched by environment variables the chart's
# Job sets (e.g. SET_DEFAULT_WORKSPACE, PRINT_CREDENTIALS), never by editing one copy.
shared_files=(
  "docker-compose/opensearch-dashboards/init/init-opensearch-dashboards.py:$CHART/files/init-opensearch-dashboards.py"
  "docker-compose/data-prepper/genai-evaluation-index-template.json:$CHART/files/genai-evaluation-index-template.json"
)
drift=0
for pair in "${shared_files[@]}"; do
  src="${pair%%:*}"
  dst="${pair#*:}"
  if ! diff -q "$src" "$dst" >/dev/null; then
    echo "DRIFT: $dst differs from $src (copy $src over it)"
    drift=1
  fi
done
[ "$drift" -eq 0 ] || exit 1
echo "shared files in sync"

echo "==> helm dependency build"
# Subchart tarballs must be present for unittest suites that target subchart
# templates (e.g. tests/otel_demo_frontend_proxy_test.yaml against the
# opentelemetry-demo subchart's component.yaml).
helm dependency build "$CHART"

echo ""
echo "==> helm lint"
helm lint "$CHART"

echo ""
echo "==> helm unittest"
helm unittest "$CHART"
