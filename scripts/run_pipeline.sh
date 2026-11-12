#!/usr/bin/env bash
# Full M2-M5 pipeline on real data (sequential), Windows (Git Bash) or Linux. On Google VMs prefer
# deploy/google/train_pipeline.sh (stage resume, locked-evaluate gate, persistent logs).
set -u
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -x .venv/Scripts/aqe.exe ]; then AQE=.venv/Scripts/aqe.exe; else AQE=".venv/bin/python -m aqe.cli.main"; fi  # Windows | Linux
CFG="${AQE_EXPERIMENT_CONFIG:-configs/experiment.yaml}"
for step in build-dataset train-lgbm train-tcn "build-oos --models lgbm,tcn" train-meta calibrate evaluate; do
  echo "=== $(date -u +%FT%TZ) aqe $step"
  $AQE $step --config $CFG
  rc=$?
  echo "=== exit $rc"
  if [ $rc -ne 0 ]; then echo "STOPPED at $step"; exit $rc; fi
done
echo "=== PIPELINE DONE $(date -u +%FT%TZ)"
