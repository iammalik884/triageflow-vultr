#!/usr/bin/env bash
# One-shot setup for a fresh Vultr Cloud Compute VM (Ubuntu 22.04/24.04, >= 2 GB RAM).
# Usage (as root, from the cloned repo folder):
#   export VULTR_INFERENCE_API_KEY=...        # from Vultr console > Serverless > Inference
#   export VULTR_MODEL=kimi-k2-instruct       # any id from GET /v1/models
#   bash deploy/setup_vultr.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
: "${VULTR_INFERENCE_API_KEY:?set VULTR_INFERENCE_API_KEY first}"
VULTR_MODEL="${VULTR_MODEL:-kimi-k2-instruct}"

echo "==> installing Docker + Python"
apt-get update -y
apt-get install -y docker.io python3-venv python3-pip curl
systemctl enable --now docker

echo "==> building the sandbox image (the only place agent code runs)"
docker build -t triageflow-sandbox:latest "$APP_DIR/sandbox"

echo "==> optional: gVisor runtime for a second isolation layer"
if [ "${USE_GVISOR:-0}" = "1" ]; then
  curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" > /etc/apt/sources.list.d/gvisor.list
  apt-get update -y && apt-get install -y runsc
  runsc install && systemctl restart docker
  RUNTIME=runsc
else
  RUNTIME=""
fi

echo "==> python venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> secrets file (readable by root only; never passed to sandboxes)"
umask 077
cat > /etc/triageflow.env <<EOF
VULTR_INFERENCE_API_KEY=${VULTR_INFERENCE_API_KEY}
VULTR_MODEL=${VULTR_MODEL}
SANDBOX_IMAGE=triageflow-sandbox:latest
SANDBOX_RUNTIME=${RUNTIME}
EOF

echo "==> systemd service on port 80"
sed "s#__APP_DIR__#${APP_DIR}#g" "$APP_DIR/deploy/triageflow.service" > /etc/systemd/system/triageflow.service
systemctl daemon-reload
systemctl enable --now triageflow
command -v ufw >/dev/null && ufw allow 80/tcp || true

sleep 3
curl -s http://127.0.0.1/api/health && echo
echo "==> done. Open http://$(curl -s -4 ifconfig.me)/"
