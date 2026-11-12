#!/usr/bin/env bash
# One-time host preparation (Ubuntu 22.04/24.04: Oracle ARM64 or Google Compute Engine x86-64). Run as a sudo-capable admin:
#   sudo bash deploy/linux/bootstrap_host.sh <role>
#   role: oracle-collector | oracle-shadow | google-collector | google-cpu-train | google-gpu-train
# Idempotent. Creates user `aqe`, /opt/aqe (code, venvs), /srv/aqe/{data,artifacts}; installs git, chrony, python3.12-venv,
# logrotate rules and a journald cap. Never deletes anything and creates NO cloud resources.
# - `aqe` gets a real login shell but no password: transfers run `ssh aqe@host /opt/aqe/bin/aqe-python -m aqe.ops ...`,
#   which a nologin shell refuses. Login is key-only via /home/aqe/.ssh/authorized_keys (created empty, never overwritten).
# - /etc/aqe/aqe.env is created ONCE from aqe.env.example with this role's AQE_ROLE / collector config / disk floor and is
#   never overwritten; an existing file with another AQE_ROLE is reported, not changed.
# - google-collector: mount the data persistent disk at /srv/aqe BEFORE running this (docs/CLOUD_DEPLOYMENT.md §15).
# - google-*-train: an SSH key for pulling from the collector is generated if absent and its public key printed.
set -euo pipefail
ROLE="${1:?usage: bootstrap_host.sh <role>}"
case "$ROLE" in
  oracle-collector|oracle-shadow) COLLECTOR_CFG=configs/cloud/oracle_collector.yaml; MIN_FREE_GB=20; WANT_ARCH=aarch64 ;;
  google-collector) COLLECTOR_CFG=configs/cloud/google_collector.yaml; MIN_FREE_GB=10; WANT_ARCH=x86_64 ;;
  google-cpu-train|google-gpu-train) COLLECTOR_CFG=configs/cloud/google_collector.yaml; MIN_FREE_GB=10; WANT_ARCH=x86_64 ;;
  *) echo "unknown role $ROLE" >&2; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
[ "$(uname -m)" = "$WANT_ARCH" ] || echo "WARNING: role $ROLE expects $WANT_ARCH, this host is $(uname -m) (install_release.sh refuses a requirements file for another CPU)" >&2

apt-get update -y
apt-get install -y git chrony logrotate ca-certificates curl openssh-client
if ! command -v python3.12 >/dev/null; then
  apt-get install -y python3.12 python3.12-venv || { echo "python3.12 not in this Ubuntu's repos: use Ubuntu 24.04 or install it explicitly" >&2; exit 3; }
else
  apt-get install -y python3.12-venv
fi
systemctl enable --now chrony   # Google images point chrony at metadata.google.internal, Oracle at 169.254.169.254
if ! id aqe >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/aqe --shell /bin/bash aqe
elif [ "$(getent passwd aqe | cut -d: -f7)" != "/bin/bash" ]; then
  usermod --shell /bin/bash aqe   # an earlier version used nologin, which makes `ssh aqe@host <command>` fail
  echo "aqe login shell set to /bin/bash (SSH key only; no password is set)"
fi
install -d -o aqe -g aqe -m 0700 /home/aqe/.ssh
[ -f /home/aqe/.ssh/authorized_keys ] || install -o aqe -g aqe -m 0600 /dev/null /home/aqe/.ssh/authorized_keys
install -d -o aqe -g aqe -m 0750 /opt/aqe /opt/aqe/releases /opt/aqe/venvs /srv/aqe /srv/aqe/data /srv/aqe/artifacts
install -d -o root -g aqe -m 0750 /etc/aqe
if [ ! -f /etc/aqe/aqe.env ]; then
  sed -e "s|^AQE_ROLE=.*|AQE_ROLE=$ROLE|" -e "s|^AQE_COLLECTOR_CONFIG=.*|AQE_COLLECTOR_CONFIG=$COLLECTOR_CFG|" \
      -e "s|^AQE_MIN_FREE_GB=.*|AQE_MIN_FREE_GB=$MIN_FREE_GB|" "$HERE/aqe.env.example" > /etc/aqe/aqe.env.new
  chown root:aqe /etc/aqe/aqe.env.new && chmod 0640 /etc/aqe/aqe.env.new
  mv -n /etc/aqe/aqe.env.new /etc/aqe/aqe.env
  echo "created /etc/aqe/aqe.env for $ROLE — review it"
else
  CURRENT_ROLE="$(sed -n 's/^AQE_ROLE=//p' /etc/aqe/aqe.env | tail -n 1)"
  if [ "$CURRENT_ROLE" = "$ROLE" ]; then
    echo "/etc/aqe/aqe.env exists (AQE_ROLE=$ROLE) — left unchanged"
  else
    echo "WARNING: /etc/aqe/aqe.env has AQE_ROLE=${CURRENT_ROLE:-<missing>}, not $ROLE. Left unchanged: set AQE_ROLE," \
         "AQE_COLLECTOR_CONFIG and AQE_MIN_FREE_GB yourself (install_release.sh and preflight refuse a mismatch)" >&2
  fi
fi
install -o root -g root -m 0644 "$HERE/logrotate/aqe" /etc/logrotate.d/aqe
install -d /etc/systemd/journald.conf.d
install -o root -g root -m 0644 "$HERE/journald/aqe.conf" /etc/systemd/journald.conf.d/aqe.conf
systemctl restart systemd-journald
case "$ROLE" in
  google-collector)
    mountpoint -q /srv/aqe || echo "WARNING: /srv/aqe is not a separate mount. Attach a persistent data disk, mount it at /srv/aqe" \
                                   "(fstab with nofail), then re-run this script; preflight warns as well" >&2 ;;
esac
case "$ROLE" in
  oracle-collector|google-collector)
    MEM_KB="$(sed -n 's/^MemTotal: *\([0-9]*\) kB/\1/p' /proc/meminfo)"; SWAP_KB="$(sed -n 's/^SwapTotal: *\([0-9]*\) kB/\1/p' /proc/meminfo)"
    if [ "${MEM_KB:-0}" -lt 1572864 ] && [ "${SWAP_KB:-0}" -lt 1048576 ]; then
      echo "NOTE: RAM < 1.5 GiB and no swap. Recommended before installing: a 2 GiB swap file" \
           "(fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile, plus an fstab line)" >&2
    fi ;;
  google-cpu-train|google-gpu-train)
    if [ ! -f /home/aqe/.ssh/id_ed25519 ]; then
      sudo -u aqe ssh-keygen -q -t ed25519 -N '' -C "aqe-pull@$(hostname)" -f /home/aqe/.ssh/id_ed25519
    fi
    echo "pull key of this training VM — add ONE line to the collector's /home/aqe/.ssh/authorized_keys:"
    echo "  from=\"<THIS_VM_INTERNAL_IP>\",restrict $(cat /home/aqe/.ssh/id_ed25519.pub)" ;;
esac
echo "time sync: $(timedatectl show -p NTPSynchronized --value)  (must be yes before collecting)"
echo "next: clone the repo to /opt/aqe/repo as user aqe, then deploy/linux/install_release.sh <commit> $ROLE"
