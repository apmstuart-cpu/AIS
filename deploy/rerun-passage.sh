#!/usr/bin/env bash
# Rerun ALIBI's Rubicón -> English Harbour passage model with a fresh forecast, then publish:
# passage odds on her measured polar (her logged speed by wind and angle) and 5% below it, the six route options, the 10-day wind maps, the interactive
# wind data and (via the report) both PDFs. Starts in the background; follow it with
#   journalctl -u yachtspeed-passage -f
# Usage: sudo ./deploy/rerun-passage.sh [max_twa_deg]     (default 150: broad reach, no dead running)
set -euo pipefail
MAXTWA="${1:-150}"
HIST=/var/lib/yachtspeed/report/climate_alibi_of_london_polar_history.json
[ -f "$HIST" ] || { echo "no $HIST: run polar-history first"; exit 1; }
systemctl reset-failed yachtspeed-passage 2>/dev/null || true
systemd-run --unit=yachtspeed-passage /bin/bash -c "
set -x
Y='runuser -u yachtspeed -- env YACHTSPEED_CONFIG=/etc/yachtspeed/config.toml /opt/yachtspeed/.venv/bin/yachtspeed'
C='--yacht ALIBI\ OF\ LONDON --from rubicon --to english-harbour --depart 2026-11-20T10:00+00:00 --by 2026-12-06T23:59-04:00 --years 1997,2006-2025 --motor-below 10 --motor-speed 8.5 --fuel-l 3500 --burn-lph 15 --genset-lph 1.25 --max-twa $MAXTWA --measured-polar $HIST'
eval \$Y climate-route \$C --name alibi_antigua
eval \$Y climate-route \$C --polar-factor 0.95 --name alibi_antigua_80
eval \$Y strategies \$C --name alibi_strategies
eval \$Y forecast-maps --from rubicon --depart 2026-11-20T10:00+00:00 --days 10 --options A,B,C,D,E,F --name alibi_forecast
eval \$Y wind-export --from rubicon --to english-harbour --depart 2026-11-20T10:00+00:00 --days 16
systemctl start yachtspeed-report
"
echo "started: follow with  journalctl -u yachtspeed-passage -f"
