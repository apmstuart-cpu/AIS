#!/bin/sh
# /usr/local/bin/yachtspeed: run the CLI as the service user with the service config.
# The AISStream key is loaded from the root-only env file into the environment,
# never onto the command line.
set -e
if [ "$(id -u)" -ne 0 ]; then exec sudo "$0" "$@"; fi
set -a; . /etc/yachtspeed/env; set +a
export YACHTSPEED_CONFIG=/etc/yachtspeed/config.toml
cd /var/lib/yachtspeed
exec runuser -u yachtspeed -- /opt/yachtspeed/.venv/bin/yachtspeed --plain-logs "$@"
