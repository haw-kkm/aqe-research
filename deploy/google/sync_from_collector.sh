#!/usr/bin/env bash
# Incremental pull of the collector's completed data into this Google training VM's data_root (run after the VM starts).
#   sudo -u aqe bash deploy/google/sync_from_collector.sh aqe@<collector-host> [--execute] [--accept-new-hostkey]
#   collector-host: the Google collector VM (Google-only operation: its internal IP or internal DNS name
#   <vm>.<zone>.c.<project>.internal, same VPC) or the Oracle host. The collector keeps collecting meanwhile.
# DRY-RUN unless --execute. Mirror mode (python -m aqe.ops transfer --mode mirror):
#   - absent files are copied, same content is skipped
#   - partitions that grew at the collector replace the copy received last time from the SAME data store
#     (status/data_store.json on the collector) if this VM did not change it; the old version is kept in _transfer/<id>/replaced/
#   - anything else (a different store at that address, a locally changed file) is kept in _transfer/<id>/conflicts/
#     -> review, overlapping collector partitions: python -m aqe.ops transfer-merge (versions merged once are skipped later)
# Only completed files are pulled (not modified for 15 min, no *.open); status/ and _transfer/ never travel.
# Needs: this VM's /home/aqe/.ssh/id_ed25519 (bootstrap_host.sh prints the public key) in the collector's
# /home/aqe/.ssh/authorized_keys; --accept-new-hostkey records the collector's host key on first contact (TOFU).
# stdout = the transfer JSON (plan or receipt); notes go to stderr. Exit: 0 plan made / transfer complete, 1 incomplete.
set -euo pipefail
HOST="${1:?usage: sync_from_collector.sh user@collector-host [--execute] [--accept-new-hostkey]}"
shift
EXEC=0
ACCEPT_NEW=0
for arg in "$@"; do
  case "$arg" in
    --execute) EXEC=1 ;;
    --accept-new-hostkey) ACCEPT_NEW=1 ;;
    *) echo "unknown argument $arg" >&2; exit 2 ;;
  esac
done
[[ "$HOST" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$ ]] || { echo "collector must look like user@host, got $HOST" >&2; exit 2; }
AQE_HOME="${AQE_HOME:-/opt/aqe}"
PY="$AQE_HOME/current/.venv/bin/python"
export PYTHONPATH="$AQE_HOME/current/src"
DATA="${AQE_DATA_ROOT:-/srv/aqe/data}"
SOURCE_ROOT="${AQE_SYNC_SOURCE_ROOT:-/srv/aqe/data}"
ARGS=(--source "ssh://$HOST$SOURCE_ROOT" --dest "$DATA" --kind data --mode mirror --remote-python /opt/aqe/bin/aqe-python)
[ "$ACCEPT_NEW" = 1 ] && ARGS+=(--ssh-option StrictHostKeyChecking=accept-new)
[ "$EXEC" = 1 ] && ARGS+=(--execute)
set +e
OUT="$("$PY" -m aqe.ops transfer "${ARGS[@]}")"
RC=$?
set -e
printf '%s\n' "$OUT"
printf '%s' "$OUT" | "$PY" -c '
import json, sys
try:
    r = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
c = r.get("conflicts") or []
tid = r.get("transfer_id")
if c:
    print(f"NOTE: {len(c)} conflict(s) kept, nothing overwritten. Review, then: python -m aqe.ops transfer-merge "
          f"--root {sys.argv[1]} --transfer-id {tid} (DRY-RUN first)", file=sys.stderr)
if r.get("updates") or r.get("updated"):
    print("NOTE: grown partitions are mirrored; previous versions stay under _transfer/<id>/replaced/", file=sys.stderr)
' "$DATA" || true
exit "$RC"
