#!/usr/bin/env bash
# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.
#
# Nimbus VM bootstrap (P6.2). UNRUN: written without a VM (H6). Idempotent: safe
# to re-run. Ubuntu 22.04/24.04 (Oracle A1 arm64 or any x86 VPS). Run as root
# (sudo) from a checkout of the repo ON THE VM:   sudo deploy/bootstrap-vm.sh
#
# It does NOT: create the VM, open cloud firewall rules, write secrets, pull the
# model key, or start the bot. It never reads or prints a secret. It will not
# lock you out: SSH password login is disabled only if an authorized key exists
# for the invoking sudo user, and sshd config is validated before reload.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR=/etc/nimbus
APP_ENV="$ENV_DIR/app.env"
MIGRATE_ENV="$ENV_DIR/migrate.env"
ADMIN_USER="${SUDO_USER:-}"

log() { printf '[bootstrap] %s\n' "$*"; }
need_root() { [ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }; }
need_root

export DEBIAN_FRONTEND=noninteractive

# --- 1. packages ------------------------------------------------------------
log "packages"
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg ufw nftables fail2ban unattended-upgrades \
    chrony age postgresql-client cron

if ! command -v docker >/dev/null 2>&1; then
    log "installing docker from the official apt repo"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    . /etc/os-release
    arch="$(dpkg --print-architecture)"
    echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

# --- 2. time sync -----------------------------------------------------------
# The container has no NTP egress (nftables); it uses the host clock.
log "time sync (chrony)"
systemctl enable --now chrony

# --- 3. unattended security upgrades ----------------------------------------
log "unattended-upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CFG'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CFG
cat > /etc/apt/apt.conf.d/52nimbus-unattended <<'CFG'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
CFG
systemctl enable --now unattended-upgrades

# --- 4. SSH key-only --------------------------------------------------------
log "sshd hardening"
have_key=0
if [ -n "$ADMIN_USER" ]; then
    home="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
    if [ -s "$home/.ssh/authorized_keys" ]; then have_key=1; fi
fi
if [ "$have_key" -eq 1 ]; then
    install -d -m 0755 /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/10-nimbus-hardening.conf <<'CFG'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
MaxAuthTries 3
CFG
    sshd -t
    systemctl reload ssh || systemctl reload sshd
else
    log "WARNING: no authorized_keys for '${ADMIN_USER:-?}'; sshd left unchanged (password auth NOT disabled). Add your key, re-run."
fi
# fail2ban: default sshd jail is enabled by the Ubuntu package; just ensure running.
systemctl enable --now fail2ban

# --- 5. firewall: SSH inbound only -------------------------------------------
# The bot has NO inbound port (Telegram long polling is outbound). Docker
# publishes nothing (docker-compose.yml). ufw governs the host INPUT path.
# Use the port sshd actually listens on (sshd -T prints the effective config), so
# a non-default Port cannot lock you out. Falls back to 22 with a warning.
ssh_port="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')"
if ! [[ "$ssh_port" =~ ^[0-9]+$ ]]; then
    log "WARNING: could not read sshd port; assuming 22"
    ssh_port=22
fi
log "ufw: deny incoming except ${ssh_port}/tcp"
ufw --force default deny incoming
ufw --force default allow outgoing
ufw allow "${ssh_port}/tcp"
ufw --force enable

# --- 6. swap (12 GB RAM shape: a small safety swap, low swappiness) -----------
if ! swapon --show=NAME --noheadings | grep -q .; then
    log "creating 2G swapfile"
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/90-nimbus.conf
sysctl -q -p /etc/sysctl.d/90-nimbus.conf

# --- 7. env files (placement and mode only; YOU fill the values) -------------
log "env files in $ENV_DIR (mode 0600, root-owned, outside the repo)"
install -d -m 0750 -o root -g root "$ENV_DIR"
for f in "$APP_ENV" "$MIGRATE_ENV"; do
    if [ ! -e "$f" ]; then
        : > "$f"
        log "created empty $f; fill it from docs/pivot/ENV_CHECKLIST.md and docs/runbooks/DEPLOY.md"
    fi
    chown root:root "$f"
    chmod 0600 "$f"
done

# --- 8. egress ruleset + boot unit -------------------------------------------
log "install egress ruleset and systemd unit"
install -d -m 0755 /opt/nimbus
# Keep the repo checkout where it is; the unit points at it via a symlink.
ln -sfn "$REPO_DIR" /opt/nimbus/current
install -m 0644 "$REPO_DIR/deploy/nftables/nimbus-egress.nft" /etc/nimbus-egress.nft
nft -c -f /etc/nimbus-egress.nft
install -m 0644 "$REPO_DIR/deploy/nimbus.service" /etc/systemd/system/nimbus.service
systemctl daemon-reload
systemctl enable nimbus.service
log "enabled nimbus.service (NOT started: fill env files, run the migrate step, then 'systemctl start nimbus')"

# --- 9. disk-usage alert hook (installed by the observability task) ----------
log "done. Next: docs/runbooks/DEPLOY.md section 'First deploy'."
