#!/bin/sh
set -eu

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET_USER=${ASTRAQUOTE_DESKTOP_USER:-ec2-user}
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
TARGET_UID=$(id -u "$TARGET_USER")
TARGET_BUS="/run/user/$TARGET_UID/bus"

test -n "$TARGET_HOME"
install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" "$TARGET_HOME/.local/bin"
install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" "$TARGET_HOME/.config/autostart"
install -d -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" "$TARGET_HOME/.config/systemd/user"
install -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" \
  "$SOURCE_DIR/desktop/astraquote-session-policy.sh" \
  "$TARGET_HOME/.local/bin/astraquote-session-policy"
install -m 0755 -o "$TARGET_USER" -g "$TARGET_USER" \
  "$SOURCE_DIR/desktop/astraquote-unlock-guard.sh" \
  "$TARGET_HOME/.local/bin/astraquote-unlock-guard"
install -m 0644 -o "$TARGET_USER" -g "$TARGET_USER" \
  "$SOURCE_DIR/desktop/astraquote-session-policy.desktop" \
  "$TARGET_HOME/.config/autostart/astraquote-session-policy.desktop"
install -m 0644 -o "$TARGET_USER" -g "$TARGET_USER" \
  "$SOURCE_DIR/desktop/astraquote-unlock-guard.service" \
  "$TARGET_HOME/.config/systemd/user/astraquote-unlock-guard.service"

if command -v runuser >/dev/null 2>&1; then
  if [ -S "$TARGET_BUS" ]; then
    runuser -u "$TARGET_USER" -- env DISPLAY=:1 \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$TARGET_BUS" \
      "$TARGET_HOME/.local/bin/astraquote-session-policy" || true
  else
    runuser -u "$TARGET_USER" -- env DISPLAY=:1 \
      "$TARGET_HOME/.local/bin/astraquote-session-policy" || true
  fi

  if [ -S "$TARGET_BUS" ]; then
    runuser -u "$TARGET_USER" -- env \
      XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$TARGET_BUS" \
      systemctl --user daemon-reload
    runuser -u "$TARGET_USER" -- env \
      XDG_RUNTIME_DIR="/run/user/$TARGET_UID" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$TARGET_BUS" \
      systemctl --user enable --now astraquote-unlock-guard.service
  fi
fi
