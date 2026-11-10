#!/usr/bin/env bash
# Full M2-M5 pipeline on real data (sequential). Logs to E:/aqe-v2/data/logs/pipeline_<ts>.log
set -u
cd "$(dirname "$0")/.."
AQE=.venv/Scripts/aqe.exe
CFG=configs/experiment.yaml
for step in build-dataset train-lgbm train-tcn "build-oos --models lgbm,tcn" train-meta calibrate evaluate; do
  echo "=== $(date -u +%FT%TZ) aqe $step"
  $AQE $step --config $CFG
  rc=$?
  echo "=== exit $rc"
  if [ $rc -ne 0 ]; then echo "STOPPED at $step"; exit $rc; fi
done
echo "=== PIPELINE DONE $(date -u +%FT%TZ)"
