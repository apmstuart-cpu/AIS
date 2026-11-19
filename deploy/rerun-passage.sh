#!/usr/bin/env bash
# Rerun ALIBI's passage model to English Harbour with the freshest forecast, then publish: passage odds on
# her measured polar (her logged speed by wind and angle) and 5% below it, the route options (fastest,
# fuel-saving, more sailing, most wind, and the waypoint routes while they are still ahead), the 10-day wind
# maps, the interactive wind data and (via the report) both PDFs.
#
# Before departure it routes from Rubicón at the planned time; once she is under way it routes from her
# latest tracked position (AIS / YB / screenshots); once she has arrived it does nothing.
# Fuel and other settings can be changed at sea in /etc/yachtspeed/passage.env, e.g.
#   FUEL_L=2600        # litres aboard now
#   MAXTWA=150         # deepest true wind angle under sail
# Starts in the background; follow it with:  journalctl -u yachtspeed-passage -f
# Usage: sudo ./deploy/rerun-passage.sh [max_twa_deg]
set -euo pipefail
YACHT="ALIBI OF LONDON"; START=rubicon; DEST=english-harbour
PLANNED=2026-11-20T10:00+00:00; BY=2026-12-06T23:59-04:00
FUEL_L=3500; BURN_LPH=15; GENSET_LPH=1.25; MAXTWA=150
[ -f /etc/yachtspeed/passage.env ] && . /etc/yachtspeed/passage.env
MAXTWA="${1:-$MAXTWA}"
HIST=/var/lib/yachtspeed/report/climate_alibi_of_london_polar_history.json
[ -f "$HIST" ] || { echo "no $HIST: run polar-history first"; exit 1; }
Y0="runuser -u yachtspeed -- env YACHTSPEED_CONFIG=/etc/yachtspeed/config.toml /opt/yachtspeed/.venv/bin/yachtspeed"

eval "$($Y0 passage-start --yacht "$YACHT" --from $START --to $DEST --depart $PLANNED)"
echo "passage rerun: $LABEL (fuel $FUEL_L L)"
[ "$MODE" = arrived ] && { echo "she has arrived: nothing to rerun"; exit 0; }

systemctl reset-failed yachtspeed-passage 2>/dev/null || true
systemd-run --unit=yachtspeed-passage /bin/bash -c "
set -x
Y='$Y0'
C='--yacht ALIBI\ OF\ LONDON --from $FROM --to $DEST --depart $DEPART --by $BY --years 1997,2006-2025 --motor-below 10 --motor-speed 8.5 --fuel-l $FUEL_L --burn-lph $BURN_LPH --genset-lph $GENSET_LPH --max-twa $MAXTWA --measured-polar $HIST'
eval \$Y climate-route \$C --name alibi_antigua
eval \$Y climate-route \$C --polar-factor 0.95 --name alibi_antigua_80
eval \$Y strategies \$C --name alibi_strategies
eval \$Y forecast-maps --from $FROM --depart $DEPART --days 10 --options A,B,C,D,E --name alibi_forecast
eval \$Y wind-export --from $FROM --to $DEST --depart $DEPART --days 16
systemctl start yachtspeed-report
"
echo "started: follow with  journalctl -u yachtspeed-passage -f"
