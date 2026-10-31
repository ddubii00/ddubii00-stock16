#!/usr/bin/env bash
set -euo pipefail
cd /var/www/stock16-7
test "$(uname -s)" = Linux
command -v python3 >/dev/null
command -v ss >/dev/null
if ! id stock16 >/dev/null 2>&1; then sudo useradd --system --home-dir /var/www/stock16-7 --shell /usr/sbin/nologin stock16; fi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Existing DB and .env are never overwritten by this script.
if [ ! -f .env ]; then
    .venv/bin/python -m stock16 setup
    chosen_port=3016
    while ss -H -ltn "sport = :$chosen_port" | read -r _; do
        chosen_port=$((chosen_port+1))
        if [ "$chosen_port" -gt 3099 ]; then echo 'No free candidate port'; exit 1; fi
    done
    .venv/bin/python - "$chosen_port" <<'PY'
from pathlib import Path
import sys
p=Path('.env'); s=p.read_text(); s=s.replace('STOCK16_PORT=3016','STOCK16_PORT='+sys.argv[1]); p.write_text(s)
PY
    echo "Selected port $chosen_port; update nginx proxy_pass to this port."
fi
mkdir -p data/logs
sudo chown -R stock16:stock16 data
sudo chown root:stock16 .env
sudo chmod 640 .env
.venv/bin/python -m stock16 init
sudo chown -R stock16:stock16 data
sudo install -m 644 deploy/*.service deploy/*.timer /etc/systemd/system/
sudo install -m 644 deploy/stock16.logrotate /etc/logrotate.d/stock16
sudo systemctl daemon-reload
sudo systemctl enable --now webapp-stock16-7.service stock16-etf.timer stock16-daily.timer stock16-maintenance.timer stock16-disk.timer
echo 'Install complete. Add nginx-stock16.conf to the existing HTTPS host, then nginx -t and reload.'
