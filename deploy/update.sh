#!/usr/bin/env bash
set -euo pipefail
cd /var/www/stock16-7
test -z "$(git status --porcelain --untracked-files=no)" || { echo 'Tracked local edits exist; review before update'; exit 1; }
sudo -u stock16 .venv/bin/python -m stock16 backup
git fetch origin main
git merge --ff-only origin/main
.venv/bin/pip install -r requirements.txt
sudo -u stock16 .venv/bin/python -m stock16 init
sudo systemctl restart webapp-stock16-7.service
echo 'Updated application; DB, raw data and .env preserved.'
