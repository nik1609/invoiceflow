#!/usr/bin/env bash
# Put free automatic HTTPS in front of the app on the Oracle VM. Run on your Mac after deploy/oracle.sh worked:
#   bash deploy/https.sh ubuntu@<public-ip> path/to/ssh-key.key
# Needs TCP 443 open in the Oracle security list (same place you opened 80).
# The link becomes https://<ip-with-dashes>.sslip.io, a free name that points at the IP. Caddy gets the certificate itself.
set -euo pipefail

HOST="${1:?Usage: bash deploy/https.sh ubuntu@<public-ip> [ssh-key]}"
KEY="${2:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
IP="${HOST##*@}"
DOMAIN="${IP//./-}.sslip.io"

ssh "${SSH_OPTS[@]}" "$HOST" DOMAIN="$DOMAIN" bash -s <<'REMOTE'
set -euo pipefail
sudo iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save >/dev/null 2>&1 || true
sudo docker network create web >/dev/null 2>&1 || true
sudo docker rm -f invoiceflow caddy >/dev/null 2>&1 || true
sudo docker run -d --name invoiceflow --restart unless-stopped --network web -v invoiceflow_data:/data --env-file ~/invoiceflow.env invoiceflow
sudo docker run -d --name caddy --restart unless-stopped --network web -p 80:80 -p 443:443 \
  -v caddy_data:/data caddy:2 caddy reverse-proxy --from "$DOMAIN" --to invoiceflow:7860
sleep 15
curl -fsS "https://$DOMAIN/api/health" && echo
REMOTE

echo "Live at https://$DOMAIN"
