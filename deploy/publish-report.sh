#!/usr/bin/env bash
# Build the yachtspeed report and publish it to the repo's `server-reports` branch.
# Runs as root from yachtspeed-report.service. The branch is a single commit,
# force-pushed each time, so it never grows. Uses a separate write-enabled deploy key.
set -euo pipefail
OWNER="${1:?owner}"; OWNER_HOME="${2:?owner home}"; REMOTE="${3:-git@github.com:apmstuart-cpu/AIS.git}"
KEY="$OWNER_HOME/.ssh/ais_reports"
WORK="$OWNER_HOME/ais-reports"
OUT=/var/lib/yachtspeed/report
[ -f "$KEY" ] || { echo "no $KEY: create it and add it to GitHub as a write deploy key"; exit 78; }

runuser -u yachtspeed -- env YACHTSPEED_CONFIG=/etc/yachtspeed/config.toml \
  /opt/yachtspeed/.venv/bin/yachtspeed --plain-logs report --out "$OUT"

as_owner() { runuser -u "$OWNER" -- env HOME="$OWNER_HOME" \
  GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" "$@"; }
if [ ! -d "$WORK/.git" ]; then
  as_owner git init -q "$WORK"
  as_owner git -C "$WORK" remote add origin "$REMOTE"
fi
rm -f "$WORK"/route_*.jpg; rm -rf "$WORK/review"
install -o "$OWNER" -m 644 "$OUT/report.json" "$OUT/report.md" "$WORK/"
for f in "$OUT"/route_*.jpg; do [ -e "$f" ] && install -o "$OWNER" -m 644 "$f" "$WORK/"; done
# screenshots OCR could not read, so they can be read by hand (deleted once captured)
if compgen -G "$OUT/review/*.jpg" >/dev/null; then
  install -d -o "$OWNER" "$WORK/review"
  install -o "$OWNER" -m 644 "$OUT"/review/*.jpg "$WORK/review/"
fi
as_owner git -C "$WORK" add -A .
AMEND=""
as_owner git -C "$WORK" rev-parse -q --verify HEAD >/dev/null && AMEND="--amend"
# one commit, replaced each hour, so the branch never grows
as_owner git -C "$WORK" -c user.name="yachtspeed server" -c user.email="yachtspeed@localhost" \
  commit -q $AMEND -m "Server report $(date -u +%Y-%m-%dT%H:%MZ)" --allow-empty
as_owner git -C "$WORK" push -q -f origin HEAD:refs/heads/server-reports
as_owner git -C "$WORK" reflog expire --expire=now --all
as_owner git -C "$WORK" gc -q --prune=now
echo "published report to server-reports"
