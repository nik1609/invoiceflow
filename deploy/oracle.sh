#!/usr/bin/env bash
# Deploy InvoiceFlow to an Oracle Cloud Always Free VM (Ubuntu). Run it from the project folder on your Mac:
#   bash deploy/oracle.sh ubuntu@<public-ip> [path/to/ssh-key.pem]
# It copies the code, installs Docker on the VM, builds the image and starts it on port 80 with auto-restart.
# The Groq key is asked for here (hidden), sent over SSH into a root-only file on the VM, and never put in the image.
set -euo pipefail

HOST="${1:?Usage: bash deploy/oracle.sh ubuntu@<public-ip> [ssh-key.pem]}"
KEY="${2:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY")
ssh_() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

read -r -s -p "Groq API key (hidden): " GROQ; echo
printf 'GROQ_API_KEY=%s\n' "$GROQ" | ssh_ 'umask 077; cat > ~/invoiceflow.env'

rsync -az --delete -e "ssh ${SSH_OPTS[*]}" \
  --exclude .git --exclude .venv --exclude data --exclude tests --exclude demo --exclude .env \
  --exclude __pycache__ --exclude .pytest_cache ./ "$HOST:invoiceflow/"

ssh_ bash -s <<'REMOTE'
set -euo pipefail
# Small shapes (1 GB) need swap or the image build gets killed.
if [ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 3000 ] && [ ! -f /swapfile ]; then
  sudo fallocate -l 3G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sudo sh; fi
# Oracle's Ubuntu images block inbound ports in the VM firewall as well as in the cloud console.
sudo iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent >/dev/null 2>&1 || true
sudo netfilter-persistent save >/dev/null 2>&1 || true
cd ~/invoiceflow
sudo docker build -t invoiceflow .
sudo docker rm -f invoiceflow >/dev/null 2>&1 || true
sudo docker run -d --name invoiceflow --restart unless-stopped -v invoiceflow_data:/data -p 80:7860 --env-file ~/invoiceflow.env invoiceflow
sleep 6
curl -fsS localhost/api/health && echo
REMOTE

IP="${HOST##*@}"
echo "Live at http://$IP  (also open TCP port 80 in the Oracle console security list if the page does not load)"
