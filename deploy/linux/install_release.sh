#!/usr/bin/env bash
# Install ONE verified commit as a release and (only after tests + preflight pass) point /opt/aqe/current at it.
#   sudo -u aqe bash /opt/aqe/repo/deploy/linux/install_release.sh <40-hex commit> <role> [--skip-tests]
#   role: oracle-collector | oracle-shadow | google-collector | google-cpu-train | google-gpu-train
# - refuses branch names / short SHAs: the release is exactly the given commit (docs/CLOUD_DEPLOYMENT.md)
# - checkout: git worktree /opt/aqe/releases/<commit> (previous releases are kept for rollback)
# - venv per platform requirements file, keyed by its hash: /opt/aqe/venvs/core-<hash> (requirements.lock = Windows only);
#   the file must match this host's CPU (linux-arm64-* on aarch64, linux-x86_64-* on x86_64)
# - google-collector defaults to the collector-only environment (no ML libraries); AQE_REQUIREMENTS overrides the file
# - runs the test suite (collector-only environments: the collector/ops subset) and `python -m aqe.ops preflight` on THIS
#   host before switching; AQE_ROLE in /etc/aqe/aqe.env must name the same role
# - does NOT restart services: prints the command; the switch of a running collector is an explicit operator step
set -euo pipefail
COMMIT="${1:?usage: install_release.sh <commit> <role> [--skip-tests]}"
ROLE="${2:?role required}"
SKIP_TESTS="${3:-}"
AQE_HOME="${AQE_HOME:-/opt/aqe}"
REPO="$AQE_HOME/repo"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "commit must be a full 40-hex SHA (not a branch): $COMMIT" >&2; exit 2; }
case "$ROLE" in
  oracle-collector|oracle-shadow) REQ=requirements/linux-arm64-cpu.txt ;;
  google-collector) REQ=requirements/linux-x86_64-collector.txt ;;
  google-cpu-train) REQ=requirements/linux-x86_64-cpu.txt ;;
  google-gpu-train) REQ=requirements/linux-x86_64-cuda.txt ;;
  *) echo "unknown role $ROLE" >&2; exit 2 ;;
esac
case "$ROLE" in
  oracle-collector) CFG=configs/cloud/oracle_collector.yaml ;;
  oracle-shadow) CFG=configs/cloud/oracle_shadow.yaml ;;
  google-collector) CFG=configs/cloud/google_collector.yaml ;;
  google-cpu-train) CFG=configs/cloud/google_cpu_train.yaml ;;
  google-gpu-train) CFG=configs/cloud/google_gpu_train.yaml ;;
esac
# optional override, e.g. AQE_REQUIREMENTS=requirements/linux-arm64-collector.txt (collector-only, no ML libraries)
REQ="${AQE_REQUIREMENTS:-$REQ}"
case "$(uname -m)" in aarch64|arm64) HOST_ARCH=arm64 ;; x86_64|amd64) HOST_ARCH=x86_64 ;; *) HOST_ARCH="$(uname -m)" ;; esac
case "$REQ" in
  requirements/linux-"$HOST_ARCH"-*) ;;
  *) echo "$REQ does not match this host's CPU ($HOST_ARCH): wheels differ per architecture" >&2; exit 2 ;;
esac
case "$REQ" in
  *-collector.txt)
    case "$ROLE" in
      oracle-collector|google-collector) ;;
      *) echo "the collector-only environment supports only the collector roles (oracle-collector, google-collector)" >&2; exit 2 ;;
    esac
    TESTS="tests/test_collector_offline.py tests/test_orderbook.py tests/test_trades.py tests/test_collector_host.py local-collector/tests" ;;
  *) TESTS="" ;;
esac

git -C "$REPO" fetch --tags origin
git -C "$REPO" cat-file -e "${COMMIT}^{commit}" || { echo "commit $COMMIT not in $REPO" >&2; exit 3; }
REL="$AQE_HOME/releases/$COMMIT"
[ -d "$REL" ] || git -C "$REPO" worktree add --detach "$REL" "$COMMIT"
[ "$(git -C "$REL" rev-parse HEAD)" = "$COMMIT" ] || { echo "release dir is not at $COMMIT" >&2; exit 3; }
[ -z "$(git -C "$REL" status --porcelain --untracked-files=all)" ] || { echo "release checkout is not clean" >&2; exit 3; }

REQ_HASH="$(cat "$REL/$REQ" $(grep -q '^-r common.txt' "$REL/$REQ" && echo "$REL/requirements/common.txt") | sha256sum | cut -c1-12)"
VENV="$AQE_HOME/venvs/core-$REQ_HASH"
if [ ! -x "$VENV/bin/python" ]; then
  python3.12 -m venv "$VENV.tmp"
  "$VENV.tmp/bin/python" -m pip install --upgrade pip
  "$VENV.tmp/bin/python" -m pip install --require-virtualenv -r "$REL/$REQ"
  mv "$VENV.tmp" "$VENV"
fi
ln -sfn "$VENV" "$REL/.venv"
export PYTHONPATH="$REL/src"
"$REL/.venv/bin/python" -m aqe.ops platform > "$REL/.release-platform.json"
if [ "$SKIP_TESTS" != "--skip-tests" ]; then
  # shellcheck disable=SC2086
  (cd "$REL" && "$REL/.venv/bin/python" -m pytest -q -p no:cacheprovider -m "not slow" $TESTS) | tee "$REL/.release-tests.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || { echo "tests failed on this host: not switching" >&2; exit 4; }
fi
if [ -f /etc/aqe/aqe.env ]; then set -a; . /etc/aqe/aqe.env; set +a; export PYTHONPATH="$REL/src"; fi
if [ -n "${AQE_ROLE:-}" ] && [ "$AQE_ROLE" != "$ROLE" ]; then
  echo "AQE_ROLE=$AQE_ROLE in /etc/aqe/aqe.env but installing $ROLE: not switching (fix the env file or the role)" >&2; exit 5
fi
case "$ROLE" in
  oracle-collector|google-collector)
    [ -n "${AQE_ROLE:-}" ] || { echo "AQE_ROLE missing from /etc/aqe/aqe.env (the collector unit passes it to preflight): add AQE_ROLE=$ROLE" >&2; exit 5; } ;;
esac
(cd "$REL" && "$REL/.venv/bin/python" -m aqe.ops preflight --config "$CFG" --role "$ROLE") | tee "$REL/.release-preflight.json"
ln -sfn "$REL" "$AQE_HOME/current.new" && mv -T "$AQE_HOME/current.new" "$AQE_HOME/current"
# interpreter wrapper for remote `ssh host /opt/aqe/bin/aqe-python -m aqe.ops ...` (transfers between hosts)
mkdir -p "$AQE_HOME/bin"
printf '#!/bin/sh\nexport PYTHONPATH=%s/current/src\nexec %s/current/.venv/bin/python "$@"\n' "$AQE_HOME" "$AQE_HOME" > "$AQE_HOME/bin/aqe-python.tmp"
chmod 0755 "$AQE_HOME/bin/aqe-python.tmp" && mv -f "$AQE_HOME/bin/aqe-python.tmp" "$AQE_HOME/bin/aqe-python"
printf '{"commit":"%s","role":"%s","installed_at":"%s","venv":"%s","requirements":"%s"}\n' "$COMMIT" "$ROLE" "$(date -u +%FT%TZ)" "$VENV" "$REQ" >> "$AQE_HOME/releases/installed.jsonl"
echo "current -> $COMMIT. Services were NOT restarted. When ready: sudo systemctl restart aqe-collector@BTCUSDT (or the role's unit)"
