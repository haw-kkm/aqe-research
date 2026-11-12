#!/usr/bin/env bash
# Kept for the Oracle + Google layout: same as sync_from_collector.sh with the Oracle host as the collector.
#   bash deploy/google/sync_from_oracle.sh aqe@<oracle-host> [--execute] [--accept-new-hostkey]
# Google-only operation (google-collector VM): use deploy/google/sync_from_collector.sh aqe@<collector-vm> instead.
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/sync_from_collector.sh" "$@"
