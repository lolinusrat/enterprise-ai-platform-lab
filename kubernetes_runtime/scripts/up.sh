#!/usr/bin/env bash
# Create (or reuse) the kind cluster and deploy everything. Safe to re-run.
#   GROQ_API_KEY  optional; enables the groq backend
#   API_TOKEN     optional; generated if unset
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=agent-runtime
VERSION=${VERSION:-0.2.0}
[ -f .env ] && set -a && . ./.env && set +a

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config kind-config.yaml --wait 120s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "==> build and load image agent-api:$VERSION"
docker build -q --build-arg APP_VERSION="$VERSION" -t "agent-api:$VERSION" .
kind load docker-image "agent-api:$VERSION" --name "$CLUSTER"

echo "==> metrics-server"
kubectl apply -k k8s/addons >/dev/null

echo "==> namespace + secret"
kubectl apply -f k8s/base/namespace.yaml >/dev/null
TOKEN=${API_TOKEN:-$(kubectl -n agent get secret agent-secrets -o jsonpath='{.data.api-token}' 2>/dev/null | base64 -d || true)}
TOKEN=${TOKEN:-$(openssl rand -hex 24)}
args=(--from-literal=api-token="$TOKEN")
[ -n "${GROQ_API_KEY:-}" ] && args+=(--from-literal=groq-api-key="$GROQ_API_KEY")
# create --dry-run | apply: idempotent and keeps the value out of shell history and the repo
kubectl -n agent create secret generic agent-secrets "${args[@]}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

echo "==> workload + observability"
kubectl apply -k k8s/overlays/local
kubectl -n kube-system rollout status deploy/metrics-server --timeout=120s
kubectl -n observability rollout status deploy/otel-collector --timeout=120s
kubectl -n observability rollout status deploy/jaeger --timeout=120s
kubectl -n agent rollout status deploy/agent-api --timeout=180s

cat <<MSG

Agent API   http://localhost:18080/v1/info
Jaeger UI   http://localhost:16687
Metrics     http://localhost:8889/metrics   (collector, aggregated from all pods)
Demo UI     uv run --group ui uvicorn ui.server:app --port 8790   ->  http://localhost:8790
MSG
