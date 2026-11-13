#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)"
BIN="$HOME/.local/bin"
SYSTEMD="$HOME/.config/systemd/user"
APPS="$HOME/.local/share/applications"

mkdir -p "$BIN" "$SYSTEMD" "$APPS"

ln -sf "$ROOT/bin/diputetacam" "$BIN/diputetacam"
ln -sf "$ROOT/bin/diputetacamd" "$BIN/diputetacamd"
ln -sf "$ROOT/bin/diputetacam-gui" "$BIN/diputetacam-gui"

cp "$ROOT/contrib/systemd/diputetacamd.service" "$SYSTEMD/diputetacamd.service"
cp "$ROOT/contrib/diputetacam.desktop" "$APPS/diputetacam.desktop"

systemctl --user daemon-reload

cat <<'EOF'
Instalado en ~/.local/bin y ~/.config/systemd/user.

Para arrancar el daemon ahora:
  systemctl --user enable --now diputetacamd

Comprobar:
  diputetacam doctor
  diputetacam status

Si el módulo v4l2loopback no está cargado, ejecuta primero las órdenes
que imprime `diputetacam doctor`.
EOF
