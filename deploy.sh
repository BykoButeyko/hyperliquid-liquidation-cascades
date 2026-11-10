#!/bin/sh
# Pull the latest main on the VPS and restart the collector.
#   ssh root@<vps> /home/hlliq/hl-liquidations/deploy.sh
set -e
cd /home/hlliq/hl-liquidations
before=$(sudo -u hlliq git rev-parse HEAD)
sudo -u hlliq git pull --ff-only -q
if ! sudo -u hlliq git diff --quiet "$before" HEAD -- requirements.txt; then
  sudo -u hlliq .venv/bin/pip install -q -r requirements.txt
fi
if ! sudo -u hlliq git diff --quiet "$before" HEAD -- deploy/hl-liquidations-collector.service; then
  cp deploy/hl-liquidations-collector.service /etc/systemd/system/
  systemctl daemon-reload
fi
systemctl restart hl-liquidations-collector
echo "deployed $(sudo -u hlliq git log --oneline -1)"
