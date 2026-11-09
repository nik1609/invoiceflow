#!/usr/bin/env bash
# Push new code to the Oracle VM after deploy/oracle.sh and deploy/https.sh have been run once. Keeps HTTPS in place.
#   bash deploy/update.sh ubuntu@<public-ip> path/to/ssh-key.key
set -euo pipefail
HOST="${1:?Usage: bash deploy/update.sh ubuntu@<public-ip> [ssh-key]}"
KEY="${2:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude .git --exclude .venv --exclude data --exclude tests --exclude demo --exclude .env \
  --exclude __pycache__ --exclude .pytest_cache ./ "$HOST:invoiceflow/"
ssh "${SSH_OPTS[@]}" "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd ~/invoiceflow
sudo docker build -t invoiceflow .
sudo docker rm -f invoiceflow >/dev/null 2>&1 || true
sudo docker run -d --name invoiceflow --restart unless-stopped --network web -v invoiceflow_data:/data --env-file ~/invoiceflow.env invoiceflow
sleep 8
sudo docker exec invoiceflow python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:7860/api/health').read().decode())"
REMOTE
echo "Updated."
