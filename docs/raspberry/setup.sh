#!/bin/bash
# Rebuild the CP1025 network print server on a fresh Raspberry Pi OS Lite (Trixie) install.
#
# Run on the Pi as the admin user made by Raspberry Pi Imager, with the printer plugged in
# and switched on:  bash setup.sh
# Every step checks or overwrites its own state, so running it again changes nothing.
# What each step is for, and what was measured: docs/raspberry/README.md.
set -euo pipefail

readonly REPO_URL=${REPO_URL:-https://github.com/iklobato/hp-cp1025-monitor.git}
readonly REPO_REF=${REPO_REF:-main}
readonly REPO_DIR=$HOME/hp-cp1025-monitor
readonly FOO2ZJS_URL=https://github.com/koenkooi/foo2zjs.git
readonly FOO2ZJS_COMMIT=e04290d
readonly FOO2ZJS_DIR=$HOME/foo2zjs
readonly FILTER_DIR=/usr/lib/cups/filter
readonly PPD_PI=/usr/local/share/cp1025/HP_CP1025-pi.ppd
readonly QUEUE=HP_CP1025
readonly DASHBOARD_PORT=8025
# Seconds between dashboard readings; while one holds the USB device, CUPS may not see it.
readonly DASHBOARD_EVERY=10
readonly MONITOR_DIR=$HOME/dashboard
readonly MONITOR_SECRETS=/etc/pi-dashboard
readonly CMDLINE=/boot/firmware/cmdline.txt

step() { printf '\n== %s\n' "$1"; }
fail() {
	printf 'ERROR: %s\n' "$1" >&2
	exit 1
}

((EUID != 0)) || fail "run as the admin user, not root (sudo is used where needed)"
sudo -n true 2>/dev/null || fail "this user needs sudo without a password prompt"

step "packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
	cups cups-filters avahi-daemon build-essential libcups2-dev git time \
	python3-rich libusb-1.0-0 unattended-upgrades \
	python3-pil fonts-dejavu-core fonts-dejavu-extra

step "D-Bus policy for colord"
# colord arrives with CUPS, but D-Bus only reads its policy on reload; until then every job
# waits two 25 s timeouts asking colord for a color profile.
sudo systemctl reload dbus

step "usblp off"
# The kernel usblp driver holds interface 0, which makes GET_PORT_STATUS fail for the status
# dashboard. CUPS prints through libusb and does not need it.
echo "blacklist usblp" | sudo tee /etc/modprobe.d/cp1025-no-usblp.conf >/dev/null
sudo modprobe -r usblp 2>/dev/null || true

step "sources"
if [[ -d "$REPO_DIR/.git" ]]; then
	git -C "$REPO_DIR" fetch -q "$REPO_URL" "$REPO_REF"
	git -C "$REPO_DIR" checkout -q --detach FETCH_HEAD
else
	git clone -q --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"
fi
[[ -d "$FOO2ZJS_DIR/.git" ]] || git clone -q "$FOO2ZJS_URL" "$FOO2ZJS_DIR"
git -C "$FOO2ZJS_DIR" checkout -q "$FOO2ZJS_COMMIT"

step "build driver"
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
cc -O2 -w -o "$build/foo2zjs" "$FOO2ZJS_DIR"/{foo2zjs.c,jbig.c,jbig_ar.c}
cc -O2 -w -o "$FOO2ZJS_DIR/zjsdecode" "$FOO2ZJS_DIR"/{zjsdecode.c,jbig.c,jbig_ar.c}
cc -O2 -Wall -Wextra -o "$build/rastertopksm" "$REPO_DIR/driver/macos/rastertopksm.c" -lcups
sudo install -o root -g root -m 755 \
	"$REPO_DIR/driver/macos/cp1025-foo2zjs" "$build/rastertopksm" "$build/foo2zjs" "$FILTER_DIR/"

step "PPD (A4 only, CMYK as PPD default)"
# A4 only so phones cannot pick Letter. CUPS only builds a preset for monochrome jobs, so
# color jobs get the PPD default: CMYK here, while the queue default stays monochrome.
sudo mkdir -p "$(dirname "$PPD_PI")"
sed -e 's|/Library/Printers/foo2zjs/cp1025-foo2zjs|cp1025-foo2zjs|' \
	-e '/Letter/d' \
	-e 's/^\*DefaultColorModel: Gray/*DefaultColorModel: CMYK/' \
	"$REPO_DIR/driver/macos/HP_CP1025-foo2zjs.ppd" | sudo tee "$PPD_PI" >/dev/null
cupstestppd -W translations "$PPD_PI" >/dev/null || fail "cupstestppd rejected $PPD_PI"

step "HDMI monitor carousel (tty1) and its settings page on port 8026"
# The monitor does not need the printer. Checked from the repo first, so a broken copy is
# never installed.
python3 "$REPO_DIR/pi-dashboard/screen.py" selftest "$build/monitor-selftest" >/dev/null ||
	fail "pi-dashboard selftest failed"
mkdir -p "$MONITOR_DIR"
cp -r "$REPO_DIR/pi-dashboard/." "$MONITOR_DIR/"
# The settings page owns carrossel.toml after the first run; only a missing one is seeded.
[[ -f "$MONITOR_DIR/carrossel.toml" ]] ||
	cp "$MONITOR_DIR/carrossel.example.toml" "$MONITOR_DIR/carrossel.toml"
# The news summaries read OPENROUTER_API_KEY from here; the key itself is never in the repo.
sudo install -d -o root -g root -m 755 "$MONITOR_SECRETS"
# Without consoleblank=0 the kernel blanks the monitor after 10 minutes with no key press.
# The kernel reads it only at boot.
reboot_for_monitor=no
if ! grep -qw consoleblank=0 "$CMDLINE"; then
	sudo sed -i '1s/$/ consoleblank=0/' "$CMDLINE"
	reboot_for_monitor=yes
fi
sed -e "s|/home/iklo|$HOME|g" -e "s|^User=iklo$|User=$USER|" \
	"$REPO_DIR/pi-dashboard/dashboard.service" |
	sudo tee /etc/systemd/system/dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q dashboard.service
sudo systemctl restart dashboard.service

step "queue $QUEUE"
# Pause the dashboard so it cannot hold the USB device while CUPS looks for the printer.
sudo systemctl stop cp1025-dashboard.service 2>/dev/null || true
# A USB scan sometimes misses the printer (cause not found; 1 miss in 5 to 10 tries), so retry.
uri=""
for _ in 1 2 3 4 5; do
	uri=$(sudo lpinfo -v 2>/dev/null | awk '/usb:\/\/HP\/LaserJet%20CP%201025/ {print $2; exit}')
	[[ -n "$uri" ]] && break
	sleep 3
done
# A switched-off printer only leaves the queue undone: the steps below still run (they restart
# the dashboard stopped above), and the script fails at the end.
queue_missing=no
if [[ -n "$uri" ]]; then
	sudo lpadmin -p "$QUEUE" -D "HP CP1025" -L Casa -E -v "$uri" -P "$PPD_PI" \
		-o printer-is-shared=true -o print-color-mode-default=monochrome \
		-o printer-error-policy=retry-job
	sudo lpadmin -d "$QUEUE"
else
	queue_missing=yes
	printf 'WARNING: printer not found on USB: queue %s not changed\n' "$QUEUE" >&2
fi
sudo cupsctl --share-printers PreserveJobFiles=No WebInterface=yes

step "security updates"
sudo tee /etc/apt/apt.conf.d/20auto-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
sudo tee /etc/apt/apt.conf.d/52cp1025-unattended >/dev/null <<'EOF'
// Security updates only, reboot at 04:30 when a patch needs it.
#clear Unattended-Upgrade::Origins-Pattern;
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename},label=Debian-Security";
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
};
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:30";
EOF

step "status dashboard on port $DASHBOARD_PORT"
# Read-only page for the home LAN. The lp group may open the printer's USB device node.
sudo tee /etc/systemd/system/cp1025-dashboard.service >/dev/null <<EOF
[Unit]
Description=HP CP1025 status dashboard (web, read-only)
After=network-online.target cups.service
Wants=network-online.target

[Service]
User=$USER
SupplementaryGroups=lp
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 printer_summary.py --web --host 0.0.0.0 --port $DASHBOARD_PORT --every $DASHBOARD_EVERY
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q cp1025-dashboard.service
sudo systemctl restart cp1025-dashboard.service

step "done"
printf 'print queue: ipp://%s.local/printers/%s\n' "$(hostname)" "$QUEUE"
printf 'job list:    http://%s.local:631/jobs?which_jobs=all\n' "$(hostname)"
printf 'dashboard:   http://%s.local:%s\n' "$(hostname)" "$DASHBOARD_PORT"
printf 'monitor:     http://%s.local:8026 (settings, home network only)\n' "$(hostname)"
sudo test -s "$MONITOR_SECRETS/openrouter.env" ||
	printf 'news summaries off: put OPENROUTER_API_KEY=... in %s/openrouter.env (root, mode 600)\n' "$MONITOR_SECRETS"
[[ $reboot_for_monitor == no ]] || printf 'reboot once so the monitor never blanks (consoleblank=0)\n'
[[ $queue_missing == no ]] ||
	fail "printer not found on USB: everything else is set up; switch it on and run again for the queue"
