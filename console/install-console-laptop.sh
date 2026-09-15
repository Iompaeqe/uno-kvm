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
#   - copies kvm_console.py to APP_DIR
#   - tty1 autologin -> startx -> kvm_console.py --fullscreen; quitting the app (Q) just
#     restarts it. Ctrl+Alt+F2 gives a normal login shell.
#   - boots straight in: no GRUB menu wait, no kernel log wall
#   - closing the lid SUSPENDS the machine; opening it resumes back into the console.
#     The console re-acquires the capture stick by itself after a resume.
#
# This box is a console, not a server: it is meant to be shut or powered off when unused.
# Set LID_ACTION=poweroff below if suspend/resume proves unreliable on the hardware.
#
# Afterwards: plug the CH340 and the HDMI capture stick into the laptop, open the lid,
# and the target's screen is there. Pause captures/releases the keyboard + trackpad.

set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/kvm_console.py" ] || { echo "kvm_console.py not found next to this script"; exit 1; }

# Deliberately not "kvm": Debian ships a system group of that name for /dev/kvm access,
# and adduser refuses to create a user whose matching group name is already taken.
USER_NAME=kvmconsole
APP_DIR=/opt/uno-kvm
LID_ACTION=suspend        # suspend | poweroff

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# No xserver-xorg-video-intel on purpose: the legacy Intel driver causes more breakage than
# it fixes. The modesetting driver inside xserver-xorg-core handles modern and old Intel GPUs.
apt-get install -y -qq --no-install-recommends \
  python3 python3-pygame python3-opencv python3-serial python3-numpy \
  xserver-xorg-core xserver-xorg-input-libinput xserver-xorg-video-fbdev \
  xinit x11-xserver-utils fonts-dejavu-core >/dev/null

echo "== user $USER_NAME"
id "$USER_NAME" &>/dev/null || adduser --disabled-password --gecos "KVM console" "$USER_NAME" \
  || { echo "could not create user $USER_NAME"; exit 1; }
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
cat > "$HOME_DIR/.xinitrc" <<EOF
#!/bin/sh
# No blanking: the lid switch handles power saving, and a screen that blanks while you are
# watching the target's installer is just irritating.
xset s off -dpms
# Keys go to the target, so the laptop's own layout is irrelevant: the app sends physical
# key positions and the target's layout decides what they type.
exec python3 $APP_DIR/kvm_console.py --fullscreen --video auto
EOF
cat > "$HOME_DIR/.bash_profile" <<EOF
# tty1 only. Start the console ONLY when its hardware is actually attached, so a boot
# with nothing plugged in leaves an ordinary Debian shell instead of a dead viewer.
# The same check runs again after the console exits, so unplugging and quitting drops
# you back to the shell rather than looping on absent hardware.
# Ctrl+Alt+F2 is always a normal login prompt.
if [ -z "\${DISPLAY:-}" ] && [ "\$(tty)" = "/dev/tty1" ]; then
  if python3 $APP_DIR/kvm_console.py --check-devices; then
    while true; do
      start=\$(date +%s)
      startx -- -nocursor >/dev/null 2>&1
      python3 $APP_DIR/kvm_console.py --check-devices >/dev/null 2>&1 || break
      [ \$(( \$(date +%s) - start )) -lt 5 ] && sleep 10 || sleep 2
    done
  fi
  echo
  echo "KVM hardware not attached. Connect the USB-serial adapter and the HDMI capture"
  echo "stick, then run:  startx"
  echo
fi
EOF
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/.xinitrc" "$HOME_DIR/.bash_profile"
chmod 755 "$HOME_DIR/.xinitrc"
# Allow startx from a console login (Debian's default is already "console"; be explicit).
if [ -f /etc/X11/Xwrapper.config ]; then
  sed -i 's/^allowed_users=.*/allowed_users=console/' /etc/X11/Xwrapper.config
else
  echo "allowed_users=console" > /etc/X11/Xwrapper.config
fi

echo "== lid closes the machine down ($LID_ACTION), power button powers off"
install -d /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/kvm-console.conf <<EOF
[Login]
HandleLidSwitch=$LID_ACTION
HandleLidSwitchExternalPower=$LID_ACTION
HandleLidSwitchDocked=$LID_ACTION
HandlePowerKey=poweroff
IdleAction=ignore
EOF

echo "== quiet, immediate boot"
if [ -f /etc/default/grub ]; then
  sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub
  grep -q '^GRUB_TIMEOUT=' /etc/default/grub || echo 'GRUB_TIMEOUT=0' >> /etc/default/grub
  sed -i 's/^GRUB_CMDLINE_LINUX_DEFAULT=.*/GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=3 systemd.show_status=false"/' /etc/default/grub
  grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub || \
    echo 'GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=3 systemd.show_status=false"' >> /etc/default/grub
  update-grub >/dev/null 2>&1 || true
fi

systemctl restart systemd-logind 2>/dev/null || true
systemctl daemon-reload

echo
echo "Done. Reboot to start the console."
echo "Devices seen right now:"
ls -1 /dev/serial/by-id/ 2>/dev/null | sed 's/^/  serial: /' || echo "  serial: none"
ls -1 /dev/v4l/by-id/ 2>/dev/null | sed 's/^/  video:  /' || echo "  video:  none"
