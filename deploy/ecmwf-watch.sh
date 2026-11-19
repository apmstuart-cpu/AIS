#!/usr/bin/env bash
# Rerun the passage model as soon as a new full-range ECMWF forecast is out, instead of on a clock.
# Checks Open-Meteo's ECMWF status file; when a new 00 or 12 UTC run (the 15-day ones) has been available
# for WAIT_MIN minutes (so AIFS, GFS and ICON have usually caught up), starts deploy/rerun-passage.sh,
# unless a rerun is already going. Stops doing anything after STOP_AFTER (departure).
# Run it every 20 minutes:
#   sudo systemd-run --on-calendar="*:0/20" --unit=ecmwf-watch ~/ais/deploy/ecmwf-watch.sh
# Follow it with:  journalctl -u ecmwf-watch -n 20 --no-pager
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${STATE:-/var/lib/yachtspeed/ecmwf_last_rerun}"
WAIT_MIN="${WAIT_MIN:-30}"
STOP_AFTER="${STOP_AFTER:-2026-11-20T10:00:00Z}"
MAXTWA="${MAXTWA:-150}"

now=$(date -u +%s)
stop=$(date -u -d "$STOP_AFTER" +%s)
if [ "$now" -ge "$stop" ]; then echo "after $STOP_AFTER: nothing to do (stop me with: systemctl stop ecmwf-watch.timer)"; exit 0; fi

meta=$(curl -sS -m 30 https://api.open-meteo.com/data/ecmwf_ifs025/static/meta.json) || { echo "ECMWF status unavailable, will retry"; exit 0; }
read -r init avail dend < <(python3 -c '
import json, sys
m = json.loads(sys.argv[1])
print(m["last_run_initialisation_time"], m["last_run_availability_time"], m["data_end_time"])' "$meta")

init_h=$(date -u -d "@$init" +%H)
range_d=$(( (dend - init) / 86400 ))
label="$(date -u -d "@$init" +'%d %b %H') UTC run (${range_d} days, out $(date -u -d "@$avail" +%H:%M))"
if [ "$init_h" != "00" ] && [ "$init_h" != "12" ] || [ "$range_d" -lt 10 ]; then
  echo "latest ECMWF: $label - short-range run, waiting for the next full one"; exit 0
fi
last=$(cat "$STATE" 2>/dev/null || echo 0)
if [ "$init" -le "$last" ]; then echo "latest ECMWF: $label - already rerun"; exit 0; fi
if [ $(( now - avail )) -lt $(( WAIT_MIN * 60 )) ]; then
  echo "latest ECMWF: $label - new; giving the other models until $(date -u -d "@$(( avail + WAIT_MIN * 60 ))" +%H:%M)"; exit 0
fi
if systemctl is-active --quiet yachtspeed-passage; then echo "new ECMWF $label, but a rerun is still going: next check"; exit 0; fi

echo "new ECMWF $label: starting the passage rerun"
"$HERE/rerun-passage.sh" "$MAXTWA"
echo "$init" > "$STATE"
