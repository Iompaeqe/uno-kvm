#!/usr/bin/env bash
# Turn a bare Debian install into a boot-to-KVM-console appliance.
#
# Target: a laptop with a fresh, minimal Debian 12/13 (netinst, "standard system utilities"
# only, no desktop). Run once as root from this directory:
#
#     sudo bash install-console-laptop.sh
#
# What it does (idempotent, re-run after edits to kvm_console.py):
#   - installs python3-pygame / python3-opencv / python3-serial + a minimal X server
#   - creates user "kvm" (no password login needed: tty1 auto-logs it in)
#   - copies kvm_console.py to /opt/uno-kvm
#   - tty1 autologin -> startx -> kvm_console.py --fullscreen; quitting the app (Q) just
#     restarts it a couple of seconds later. Ctrl+Alt+F2 gives a normal login shell.
#   - lid close does NOT suspend (the console must be usable the moment it is opened);
#     the screen blanks after 10 min instead.
#
# Afterwards: plug the CH340 and the HDMI capture stick into the laptop, open the lid,
# and the target's screen is there. Pause captures/releases the keyboard + trackpad.

set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/kvm_console.py" ] || { echo "kvm_console.py not found next to this script"; exit 1; }

USER_NAME=kvm
APP_DIR=/opt/uno-kvm

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-pygame python3-opencv python3-serial python3-numpy \
  xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-video-fbdev xserver-xorg-video-intel \
  xinit x11-xserver-utils fonts-dejavu-core >/dev/null

echo "== user $USER_NAME"
id "$USER_NAME" &>/dev/null || adduser --disabled-password --gecos "KVM console" "$USER_NAME"
usermod -aG dialout,video,input,render,tty "$USER_NAME" 2>/dev/null || usermod -aG dialout,video,input,tty "$USER_NAME"

echo "== app -> $APP_DIR"
install -d -m 755 "$APP_DIR"
install -m 755 "$HERE/kvm_console.py" "$APP_DIR/kvm_console.py"

echo "== autologin on tty1"
install -d /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $USER_NAME --noclear %I \$TERM
EOF

echo "== X session that is just the console"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
cat > "$HOME_DIR/.xinitrc" <<'EOF'
#!/bin/sh
xset s off
xset dpms 600 600 600
# Keys go to the target, so keep the laptop's own layout out of the way: the app sends
# physical key positions, the target's layout decides what they type.
exec python3 /opt/uno-kvm/kvm_console.py --fullscreen --video auto
EOF
cat > "$HOME_DIR/.bash_profile" <<'EOF'
# tty1 only: run the KVM console under X; restart it when it exits. Ctrl+Alt+F2 = plain shell.
if [ -z "${DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
  while true; do
    startx -- -nocursor >/dev/null 2>&1
    sleep 2
  done
fi
EOF
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.xinitrc" "$HOME_DIR/.bash_profile"
chmod 755 "$HOME_DIR/.xinitrc"
# Allow startx from a console login (Debian default "console" already permits it; be explicit).
if [ -f /etc/X11/Xwrapper.config ]; then
  sed -i 's/^allowed_users=.*/allowed_users=console/' /etc/X11/Xwrapper.config
else
  echo "allowed_users=console" > /etc/X11/Xwrapper.config
fi

echo "== lid: stay awake"
install -d /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/kvm-console.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl restart systemd-logind 2>/dev/null || true
systemctl daemon-reload

echo
echo "Done. Reboot to start the console (or: systemctl restart getty@tty1)."
echo "Devices seen right now:"
ls -1 /dev/serial/by-id/ 2>/dev/null | sed 's/^/  serial: /' || echo "  serial: none"
ls -1 /dev/v4l/by-id/ 2>/dev/null | sed 's/^/  video:  /' || echo "  video:  none"
