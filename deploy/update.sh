#!/usr/bin/env bash
set -euo pipefail
cd /var/www/stock16-7
test -z "$(git status --porcelain --untracked-files=no)" || { echo 'Tracked local edits exist; review before update'; exit 1; }
sudo -u stock16 .venv/bin/python -m stock16 backup
git fetch origin main
git merge --ff-only origin/main
.venv/bin/pip install -r requirements.txt
sudo -u stock16 .venv/bin/python -m stock16 init
sudo install -m 644 deploy/*.service deploy/*.timer /etc/systemd/system/
sudo install -m 644 deploy/stock16.logrotate /etc/logrotate.d/stock16
sudo systemctl daemon-reload
sudo systemctl enable stock16-collector-worker.service
sudo systemctl enable --now stock16-etf.timer stock16-daily.timer stock16-maintenance.timer stock16-disk.timer
sudo systemctl restart stock16-collector-worker.service webapp-stock16-7.service
echo 'Updated application; DB, raw data and .env preserved.'
