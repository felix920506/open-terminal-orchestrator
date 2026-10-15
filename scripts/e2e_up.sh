#!/usr/bin/env bash
# Bring up orchestrator + Open WebUI for scripts/e2e_openwebui.py.
#
#   OPENROUTER_API_KEY=sk-or-... scripts/e2e_up.sh        # start
#   scripts/e2e_up.sh down                                 # remove everything (incl. sandboxes)
#
# Open WebUI: http://127.0.0.1:13000  (admin@e2e.local / e2e-password-1 after the e2e script ran)
# Orchestrator: http://127.0.0.1:18090 (connection key e2e-conn-key, admin key e2e-admin-key)
set -euo pipefail
cd "$(dirname "$0")/../deploy"

OWUI_IMAGE=${OWUI_IMAGE:-ghcr.io/open-webui/open-webui:v0.12.2}
SANDBOX_IMAGE=${SANDBOX_IMAGE:-ghcr.io/open-webui/open-terminal:slim}
export ORCH_API_KEY=e2e-conn-key ORCH_ADMIN_API_KEY=e2e-admin-key ORCH_SECRET_KEY=e2e-secret \
  ORCH_DEFAULT_IMAGE=$SANDBOX_IMAGE ORCH_PULL_POLICY=if-not-present \
  OTO_IMAGE=open-terminal-orchestrator:dev OTO_PORT=127.0.0.1:18090 OTO_WEBUI_NETWORK=oto-e2e

if [[ "${1:-up}" == "down" ]]; then
  for id in $(curl -sf -H "Authorization: Bearer $ORCH_ADMIN_API_KEY" http://127.0.0.1:18090/api/v1/terminals/sandboxes \
      | python3 -c 'import json,sys; print(" ".join(b["id"] for b in json.load(sys.stdin)))' 2>/dev/null); do
    curl -sf -X DELETE -H "Authorization: Bearer $ORCH_ADMIN_API_KEY" "http://127.0.0.1:18090/api/v1/terminals/sandboxes/$id" >/dev/null || true
  done
  docker rm -f owui-e2e >/dev/null 2>&1 || true
  docker volume rm owui-e2e-data >/dev/null 2>&1 || true
  docker compose -p ototest down -v
  docker network rm oto-egress >/dev/null 2>&1 || true
  exit 0
fi

: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"
docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1 || docker pull "$SANDBOX_IMAGE"
docker build -q -t open-terminal-orchestrator:dev .. >/dev/null
docker compose -p ototest up -d --no-build

if ! docker container inspect owui-e2e >/dev/null 2>&1; then
  docker run -d --name owui-e2e --network oto-e2e -p 127.0.0.1:13000:8080 \
    -e OPENAI_API_BASE_URL=https://openrouter.ai/api/v1 -e OPENAI_API_KEY="$OPENROUTER_API_KEY" \
    -e ENABLE_OLLAMA_API=false -e WEBUI_SECRET_KEY=e2e-webui-secret-0123456789abcdef \
    -e RAG_EMBEDDING_ENGINE=openai -e ENABLE_VERSION_UPDATE_CHECK=false \
    -e TASK_MODEL_EXTERNAL=openai/gpt-4.1-nano \
    -v owui-e2e-data:/app/backend/data "$OWUI_IMAGE" >/dev/null
fi

printf 'Waiting for Open WebUI'
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:13000/health >/dev/null && { echo " ready"; exit 0; }
  printf '.'; sleep 2
done
echo " timed out"; exit 1
