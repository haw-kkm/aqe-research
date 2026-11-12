#!/usr/bin/env bash
# One-time host preparation (Ubuntu 22.04/24.04, Oracle ARM64 or Google x86-64). Run as a sudo-capable admin:
#   sudo bash deploy/linux/bootstrap_host.sh <role>      role: oracle-collector | oracle-shadow | google-cpu-train | google-gpu-train
# Idempotent. Creates user `aqe`, /opt/aqe (code, venvs), /srv/aqe/{data,artifacts}; installs git, chrony, python3.12-venv.
# Never overwrites an existing /etc/aqe/aqe.env and never deletes anything. Creates NO cloud resources.
set -euo pipefail
ROLE="${1:?usage: bootstrap_host.sh <role>}"
case "$ROLE" in oracle-collector|oracle-shadow|google-cpu-train|google-gpu-train) ;; *) echo "unknown role $ROLE" >&2; exit 2;; esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"

apt-get update -y
apt-get install -y git chrony logrotate ca-certificates curl
if ! command -v python3.12 >/dev/null; then
  apt-get install -y python3.12 python3.12-venv || { echo "python3.12 not in this Ubuntu's repos: use Ubuntu 24.04 or install it explicitly" >&2; exit 3; }
else
  apt-get install -y python3.12-venv
fi
systemctl enable --now chrony
id aqe >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/aqe --shell /usr/sbin/nologin aqe
install -d -o aqe -g aqe -m 0750 /opt/aqe /opt/aqe/releases /opt/aqe/venvs /srv/aqe /srv/aqe/data /srv/aqe/artifacts
install -d -o root -g aqe -m 0750 /etc/aqe
if [ ! -f /etc/aqe/aqe.env ]; then
  install -o root -g aqe -m 0640 "$HERE/aqe.env.example" /etc/aqe/aqe.env
  echo "created /etc/aqe/aqe.env from the example — review it"
else
  echo "/etc/aqe/aqe.env exists — left unchanged"
fi
install -o root -g root -m 0644 "$HERE/logrotate/aqe" /etc/logrotate.d/aqe
install -d /etc/systemd/journald.conf.d
install -o root -g root -m 0644 "$HERE/journald/aqe.conf" /etc/systemd/journald.conf.d/aqe.conf
systemctl restart systemd-journald
echo "time sync: $(timedatectl show -p NTPSynchronized --value)  (must be yes before collecting)"
echo "next: clone the repo to /opt/aqe/repo as user aqe, then deploy/linux/install_release.sh <commit> $ROLE"
