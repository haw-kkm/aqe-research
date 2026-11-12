#!/usr/bin/env bash
# Google on-demand training run (CPU or GPU VM). Stage-level resume; never deploys anything.
#   bash deploy/google/train_pipeline.sh --role google-cpu-train [--config configs/cloud/experiment_google.yaml]
#        [--run-id ID] [--allow-locked-evaluate] [--shutdown-after] [--sync-from aqe@<collector-host>]
# --sync-from: first pull the collector's completed data (deploy/google/sync_from_collector.sh --execute, mirror mode;
#   Google collector VM or Oracle). Training starts only if that transfer is complete AND left no conflicts
#   (conflicts = this VM holds another version of some partition: review / transfer-merge, then re-run).
# Stages: build-dataset, train-lgbm, train-tcn, build-oos (lgbm,tcn), train-meta, calibrate [, evaluate].
# - Resume: re-run with the same --run-id; a stage is skipped only if its marker has the same code commit and config
#   hash; once a stage runs, every later stage runs too (no stale downstream artifacts).
# - `evaluate` touches the LOCKED test segment (its trial counter increases): only with --allow-locked-evaluate.
# - Intra-stage checkpoints do NOT exist (TCN keeps its best epoch in memory): an interrupted stage restarts from its
#   beginning. Spot/preemptible VMs are therefore not recommended until epoch checkpoints are implemented and tested.
# - Training does not promote models: build a release (python -m aqe.ops release-build) and verify it on the host that
#   will load it (Oracle, or in Google-only operation a VM with the full environment — the collector VM has no ML libraries).
set -euo pipefail
ROLE=""; CFG=""; RUN_ID=""; ALLOW_EVAL=0; SHUTDOWN=0; SYNC_FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --config) CFG="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --allow-locked-evaluate) ALLOW_EVAL=1; shift ;;
    --shutdown-after) SHUTDOWN=1; shift ;;
    --sync-from) SYNC_FROM="$2"; shift 2 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
done
case "$ROLE" in
  google-cpu-train) CFG="${CFG:-configs/cloud/experiment_google.yaml}"; BASE=configs/cloud/google_cpu_train.yaml ;;
  google-gpu-train) CFG="${CFG:-configs/cloud/experiment_google_gpu.yaml}"; BASE=configs/cloud/google_gpu_train.yaml ;;
  *) echo "--role google-cpu-train|google-gpu-train required" >&2; exit 2 ;;
esac
AQE_HOME="${AQE_HOME:-/opt/aqe}"
REL="${AQE_RELEASE:-$AQE_HOME/current}"
PY="$REL/.venv/bin/python"
export PYTHONPATH="$REL/src"
DATA="${AQE_DATA_ROOT:-/srv/aqe/data}"
cd "$REL"
COMMIT="$(git -C "$REL" rev-parse HEAD)"
CFG_HASH="$(cat "$CFG" "$BASE" | sha256sum | cut -c1-16)"
RUN_ID="${RUN_ID:-train-$(date -u +%Y%m%dT%H%M%SZ)}"
STATE="$DATA/pipeline/$RUN_ID"
LOG="$DATA/logs/pipeline/$RUN_ID.log"
mkdir -p "$STATE" "$(dirname "$LOG")"
if [ "$SHUTDOWN" = 1 ]; then
  trap 'echo "=== shutting the VM down in 2 minutes (billing for compute stops when it is STOPPED)" | tee -a "$LOG"; sudo shutdown -h +2 "AQE training finished"' EXIT
fi
if [ -n "$SYNC_FROM" ]; then
  SYNC_OUT="$STATE/sync-$(date -u +%Y%m%dT%H%M%SZ).json"
  echo "=== $(date -u +%FT%TZ) sync from $SYNC_FROM (mirror, --execute) -> $SYNC_OUT" | tee -a "$LOG"
  set +e
  bash "$REL/deploy/google/sync_from_collector.sh" "$SYNC_FROM" --execute > "$SYNC_OUT" 2>> "$LOG"
  SYNC_RC=$?
  set -e
  SYNC_STATE="$("$PY" -c 'import json, sys
r = json.load(open(sys.argv[1], encoding="utf-8"))
print("incomplete" if not r.get("complete") else ("conflicts" if r.get("conflicts") else "ok"))' "$SYNC_OUT" 2>> "$LOG" || echo unreadable)"
  echo "=== sync rc=$SYNC_RC state=$SYNC_STATE" | tee -a "$LOG"
  if [ "$SYNC_RC" -ne 0 ] || [ "$SYNC_STATE" != ok ]; then
    echo "STOPPED before training: sync is $SYNC_STATE (rc=$SYNC_RC), see $SYNC_OUT. Training never starts on a partly synced store." | tee -a "$LOG"
    exit 6
  fi
fi
"$PY" -m aqe.ops preflight --config "$BASE" --role "$ROLE" --offline | tee -a "$LOG"
printf '{"run_id":"%s","commit":"%s","config":"%s","config_hash":"%s","started_at":"%s","allow_locked_evaluate":%s}\n' \
  "$RUN_ID" "$COMMIT" "$CFG" "$CFG_HASH" "$(date -u +%FT%TZ)" "$ALLOW_EVAL" >> "$STATE/run.jsonl"
STAGES=("build-dataset" "train-lgbm" "train-tcn" "build-oos --models lgbm,tcn" "train-meta" "calibrate")
[ "$ALLOW_EVAL" = 1 ] && STAGES+=("evaluate")
FORCE=0
for stage in "${STAGES[@]}"; do
  name="${stage%% *}"
  marker="$STATE/$name.done"
  if [ "$FORCE" = 0 ] && [ -f "$marker" ] && grep -q "\"commit\":\"$COMMIT\"" "$marker" && grep -q "\"config_hash\":\"$CFG_HASH\"" "$marker"; then
    echo "=== skip $name (done for this commit/config)" | tee -a "$LOG"; continue
  fi
  FORCE=1
  echo "=== $(date -u +%FT%TZ) aqe $stage" | tee -a "$LOG"
  set +e
  # shellcheck disable=SC2086
  "$PY" -m aqe.cli.main $stage --config "$CFG" >> "$LOG" 2>&1
  rc=$?
  set -e
  echo "=== exit $rc" | tee -a "$LOG"
  [ "$rc" -eq 0 ] || { echo "STOPPED at $name (rc=$rc); fix and re-run with --run-id $RUN_ID" | tee -a "$LOG"; exit "$rc"; }
  printf '{"stage":"%s","commit":"%s","config_hash":"%s","finished_at":"%s"}\n' "$name" "$COMMIT" "$CFG_HASH" "$(date -u +%FT%TZ)" > "$marker"
done
echo "=== PIPELINE DONE $(date -u +%FT%TZ). Models are NOT deployed. Next (optional, explicit):" | tee -a "$LOG"
echo "    $PY -m aqe.ops release-build --artifact-root \${AQE_ARTIFACT_ROOT:-/srv/aqe/artifacts} --experiment <experiment_id> --out /srv/aqe/releases" | tee -a "$LOG"
