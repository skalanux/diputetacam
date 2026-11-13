#!/usr/bin/env bash
set -euo pipefail

if ! command -v pacman >/dev/null 2>&1; then
  echo "Este script es para Arch/Omarchy (pacman)." >&2
  exit 1
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Ejecútalo con sudo: sudo $0" >&2
  exit 1
fi

pacman -S --needed --noconfirm v4l2loopback-dkms v4l2loopback-utils v4l-utils

cat >/etc/modprobe.d/99-diputetacam.conf <<'EOF'
options v4l2loopback devices=1 video_nr=10 card_label="Virtual Camera" exclusive_caps=1 max_buffers=4
EOF

echo v4l2loopback >/etc/modules-load.d/diputetacam.conf

# Load the new options. If the module is in use, unload may fail; in that
# case reboot or close the applications using the virtual camera.
modprobe -r v4l2loopback 2>/dev/null || true
modprobe v4l2loopback

echo
v4l2-ctl --list-devices || true
echo
echo "Listo. Deberías ver /dev/video10 como 'Virtual Camera'."
echo "Comprueba con: diputetacam doctor"
