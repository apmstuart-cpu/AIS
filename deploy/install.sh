#!/usr/bin/env bash
# Install or upgrade yachtspeed on Ubuntu (22.04+). Run from a checkout: sudo ./deploy/install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq python3 python3-venv sqlite3 rsync >/dev/null

id yachtspeed >/dev/null 2>&1 || useradd --system --home /var/lib/yachtspeed --shell /usr/sbin/nologin yachtspeed
install -d -o yachtspeed -g yachtspeed -m 750 /var/lib/yachtspeed /var/lib/yachtspeed/backups
install -d -m 755 /etc/yachtspeed /opt/yachtspeed

rsync -a --delete --exclude .git --exclude .venv --exclude '*.db*' "$SRC/" /opt/yachtspeed/
python3 -m venv /opt/yachtspeed/.venv
/opt/yachtspeed/.venv/bin/pip install -q --upgrade pip
/opt/yachtspeed/.venv/bin/pip install -q /opt/yachtspeed

[ -f /etc/yachtspeed/config.toml ] || install -m 644 "$SRC/config.example.toml" /etc/yachtspeed/config.toml
if [ ! -f /etc/yachtspeed/env ]; then
  umask 077; printf 'AISSTREAM_API_KEY=\n' > /etc/yachtspeed/env
fi
chmod 600 /etc/yachtspeed/env; chown root:root /etc/yachtspeed/env

install -m 755 "$SRC/deploy/yachtspeed-wrapper.sh" /usr/local/bin/yachtspeed
install -m 644 "$SRC"/deploy/systemd/yachtspeed-* /etc/systemd/system/

# keep logs across reboots
mkdir -p /var/log/journal && systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald

systemctl daemon-reload
systemctl enable --now yachtspeed-backup.timer yachtspeed-yb.timer
systemctl enable yachtspeed-collector yachtspeed-enricher
systemctl restart yachtspeed-enricher
if grep -q '^AISSTREAM_API_KEY=.\+' /etc/yachtspeed/env && grep -q '^mmsi = "[0-9]\{9\}"' /etc/yachtspeed/config.toml; then
  systemctl restart yachtspeed-collector
  echo "collector started"
else
  echo "collector NOT started: set AISSTREAM_API_KEY in /etc/yachtspeed/env and a yacht mmsi in /etc/yachtspeed/config.toml,"
  echo "then: sudo systemctl restart yachtspeed-collector"
fi
/opt/yachtspeed/.venv/bin/yachtspeed --plain-logs smoke-test >/dev/null && echo "smoke test OK"
echo "installed. check with: yachtspeed status"
