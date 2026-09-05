#!/usr/bin/env bash
# One-shot setup for a freshly imaged Pi. Idempotent -- safe to re-run.
#
#   git clone https://github.com/PatiponR/KVX-Uplink-reader.git
#   cd KVX-Uplink-reader && ./deploy/setup-pi.sh
#
# Assumes Raspberry Pi OS Bookworm or newer (NetworkManager). On Bullseye the
# networking section needs dhcpcd instead -- see deploy/README.md.
set -euo pipefail

REPO_URL="https://github.com/PatiponR/KVX-Uplink-reader.git"
REPO_DIR="/home/${SUDO_USER:-$USER}/KVX-Uplink-reader"
PLC_IP="192.168.1.210"          # the KEYENCE PLC, fixed
PI_ETH_IP="192.168.1.209/30"    # us, on a /30 that contains only us and it
PROD_URL="https://api.sn-metalpart.com"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[93m    warning: %s\033[0m\n' "$1"; }

[ "$(id -u)" -eq 0 ] && { echo "run as your normal user, not root -- it sudos where needed"; exit 1; }

# --- 0. prerequisites ------------------------------------------------------
# A fresh Raspberry Pi OS image doesn't necessarily have these: git to clone,
# sqlite3 to inspect the spool by hand later. Kept to the minimum -- the
# watcher itself is pure stdlib and needs nothing installed.
say "Installing prerequisites"
sudo apt-get update -qq
sudo apt-get install -y -qq git sqlite3

# Imager's "enable SSH" checkbox is easy to forget; make sure either way, or
# the only way back in is a monitor and keyboard.
sudo systemctl enable --now ssh >/dev/null 2>&1 || true

# --- 1. the PLC link -------------------------------------------------------
# eth0 goes on a /30 inside 192.168.1.0/24 holding just the Pi and the PLC.
# Longest-prefix match sends PLC traffic out eth0 while everything else still
# goes over WiFi, which is what lets both live on the same /24. never-default
# is the critical part: without it the Pi tries to reach the internet through
# the PLC and fails in ways that look like anything but a routing problem.
say "Configuring eth0 for the PLC at ${PLC_IP}"
wlan_ip="$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
if [ -n "$wlan_ip" ]; then
    echo "    wlan0 currently has ${wlan_ip}"
    # The one address range that breaks this design: if DHCP puts wlan0 inside
    # our /30 the two interfaces collide and NetworkManager can't bring both up.
    case "$wlan_ip" in
        192.168.1.20[89]|192.168.1.21[01])
            warn "wlan0 is inside 192.168.1.208/30 -- it collides with eth0."
            warn "Renew the DHCP lease or move the /30 before trusting this setup."
            ;;
    esac
else
    warn "wlan0 has no address yet; check WiFi before relying on the REST sink"
fi

sudo nmcli con delete plc >/dev/null 2>&1 || true
sudo nmcli con add type ethernet ifname eth0 con-name plc \
    ipv4.method manual ipv4.addresses "$PI_ETH_IP" \
    ipv4.never-default yes ipv4.dns "" ipv6.method disabled autoconnect yes
sudo nmcli con up plc || warn "couldn't bring eth0 up -- cable plugged in?"

# Stop the Pi answering ARP on wlan0 for an address that lives on eth0, which
# would otherwise confuse the office LAN about who owns 192.168.1.209.
printf 'net.ipv4.conf.all.arp_ignore = 1\nnet.ipv4.conf.all.arp_announce = 2\n' \
    | sudo tee /etc/sysctl.d/99-plc-arp.conf >/dev/null
sudo sysctl --system >/dev/null

# --- 2. WiFi ---------------------------------------------------------------
# Power save is on by default and causes multi-second stalls that make the REST
# sink look broken. This file makes the fix survive reboots; `iw` alone doesn't.
say "Disabling WiFi power save"
printf '[connection]\nwifi.powersave = 2\n' \
    | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf >/dev/null
sudo systemctl restart NetworkManager

# --- 3. the code -----------------------------------------------------------
say "Fetching the watcher"
if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$REPO_DIR"
fi

# --- 4. secrets ------------------------------------------------------------
# Kept outside the repo and mode 600: REST_API_KEY is a secret and must never
# be committed. Existing values are left alone so re-running can't wipe them.
say "Deployment settings"
if [ -f /etc/watch-signals.env ] && sudo grep -q '^REST_API_KEY=.\+' /etc/watch-signals.env; then
    echo "    /etc/watch-signals.env already has a key, leaving it alone"
else
    read -rsp "    REST_API_KEY (input hidden): " api_key; echo
    printf 'REST_BASE_URL=%s\nREST_API_KEY=%s\n' "$PROD_URL" "$api_key" \
        | sudo tee /etc/watch-signals.env >/dev/null
    sudo chmod 600 /etc/watch-signals.env
fi

# --- 5. the service --------------------------------------------------------
say "Installing the systemd unit"
sudo cp "$REPO_DIR/deploy/watch-signals.service" /etc/systemd/system/
sudo systemctl daemon-reload          # required: this creates StateDirectory
sudo systemctl enable --now watch-signals
sudo systemctl restart watch-signals

# --- 6. checks -------------------------------------------------------------
say "Verifying"
ok=0
route_dev="$(ip route get "$PLC_IP" 2>/dev/null | awk '{print $3; exit}')"
[ "$route_dev" = "eth0" ] \
    && echo "    [ok]   ${PLC_IP} routes via eth0" \
    || { echo "    [FAIL] ${PLC_IP} routes via ${route_dev:-nothing}, expected eth0"; ok=1; }

ip route | grep -q '^default.*wlan0' \
    && echo "    [ok]   default route is wlan0" \
    || { echo "    [FAIL] no default route via wlan0"; ok=1; }

# bash's own /dev/tcp rather than nc, which isn't always installed
if timeout 3 bash -c "exec 3<>/dev/tcp/${PLC_IP}/8501" 2>/dev/null; then
    echo "    [ok]   PLC host-link port 8501 is open"
else
    echo "    [FAIL] can't reach ${PLC_IP}:8501 -- cable? PLC powered?"; ok=1
fi

# The Pi has no battery-backed clock, and occurredAt timestamps depend on it.
timedatectl show -p NTPSynchronized --value | grep -q yes \
    && echo "    [ok]   clock is NTP-synced" \
    || echo "    [warn] clock not synced yet; occurredAt is omitted until it is"

systemctl is-active --quiet watch-signals \
    && echo "    [ok]   watch-signals is running" \
    || { echo "    [FAIL] watch-signals is not running"; ok=1; }

say "Done"
echo "    journalctl -u watch-signals -f"
echo "    ssh $(whoami)@192.168.1.209   (from the PLC segment -- never changes)"
[ "$ok" -eq 0 ] || echo "    some checks failed, see above"
exit "$ok"
