#!/usr/bin/env bash
# Clear all runs on the Oracle VM and go back to the starting data (do this before recording the demo).
#   bash deploy/reset.sh ubuntu@<public-ip> path/to/ssh-key.key
set -euo pipefail
HOST="${1:?Usage: bash deploy/reset.sh ubuntu@<public-ip> [ssh-key]}"
KEY="${2:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
ssh "${SSH_OPTS[@]}" "$HOST" "sudo docker exec invoiceflow python -c \"import urllib.request as u; print(u.urlopen(u.Request('http://localhost:7860/api/admin/reset', method='POST')).read().decode())\""
