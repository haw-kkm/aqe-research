#!/usr/bin/env bash
# Point /opt/aqe/current back at an already-installed release (code rollback). Nothing is deleted.
#   sudo -u aqe bash deploy/linux/rollback_release.sh <40-hex commit>
# Model rollback is separate: python -m aqe.ops release-rollback ... --approve <backup>
set -euo pipefail
COMMIT="${1:?usage: rollback_release.sh <commit>}"
AQE_HOME="${AQE_HOME:-/opt/aqe}"
REL="$AQE_HOME/releases/$COMMIT"
[ -x "$REL/.venv/bin/python" ] || { echo "release $COMMIT is not installed (run install_release.sh first)" >&2; exit 3; }
echo "current: $(readlink -f "$AQE_HOME/current")"
ln -sfn "$REL" "$AQE_HOME/current.new" && mv -T "$AQE_HOME/current.new" "$AQE_HOME/current"
printf '{"rollback_to":"%s","at":"%s"}\n' "$COMMIT" "$(date -u +%FT%TZ)" >> "$AQE_HOME/releases/installed.jsonl"
echo "current -> $COMMIT. Restart the role's service explicitly when ready."
