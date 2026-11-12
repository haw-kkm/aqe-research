#!/usr/bin/env bash
# Incremental pull of Oracle's completed data into this Google VM's data_root (run after the VM starts).
#   bash deploy/google/sync_from_oracle.sh aqe@<oracle-host> [--execute]
# DRY-RUN unless --execute. Absent files are copied; same content is skipped; grown partitions are updated (mirror
# mode, see below); anything else is kept under /srv/aqe/data/_transfer/<id>/conflicts/. Oracle keeps collecting while
# this VM is off; only completed files (not modified in the last 15 min, no *.open) are pulled.
set -euo pipefail
HOST="${1:?usage: sync_from_oracle.sh user@oracle-host [--execute]}"
EXEC="${2:-}"
AQE_HOME="${AQE_HOME:-/opt/aqe}"
export PYTHONPATH="$AQE_HOME/current/src"
DATA="${AQE_DATA_ROOT:-/srv/aqe/data}"
# mirror: partitions that grew on Oracle (monthly REST bars, hourly flow) replace the copy received last time, if this
# VM did not change it locally; the previous version is kept under _transfer/<id>/replaced/. Anything else -> conflict.
ARGS=(--source "ssh://$HOST/srv/aqe/data" --dest "$DATA" --kind data --mode mirror --remote-python /opt/aqe/bin/aqe-python)
[ "$EXEC" = "--execute" ] && ARGS+=(--execute)
"$AQE_HOME/current/.venv/bin/python" -m aqe.ops transfer "${ARGS[@]}"
